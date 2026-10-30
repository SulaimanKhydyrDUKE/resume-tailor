"""MCP server: tailoring plus the browser that can actually attach the PDF.

One process owns both halves on purpose. Splitting them means the tailorer
writes a file the browser server cannot see and the browser cannot attach.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .apply import ApplySession, _SUCCESS_TEXT, _is_empty, _looks_like_application, detect_blocker
from .profile import Profile, ProfileError
from .render import available_styles, render_pdf_async

mcp = MCPServer(name="resume-tailor")

# The project's own output directory — the one the CLI and the loop's workers
# write — whatever directory the MCP process was started in. (It used to be
# the process's cwd, so a Claude Code session opened one folder up read a
# different batch-state.json from the loop.) RESUME_TAILOR_OUTPUT overrides.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(os.environ.get("RESUME_TAILOR_OUTPUT") or PROJECT_ROOT / "output").expanduser()
# The agent driver's own Chrome profile. Worker 0 of the loop keeps the
# default profile (browser-profile) open for hours at a time, and two
# Chromes on one profile directory fight over its lock; the driver gets its
# own, persistent like the others, so a portal sign-in the user does once in
# its visible window holds for every later session. The cookies saved under
# ~/.resume-tailor/logins/ load into it as into every session.
# RESUME_TAILOR_AGENT_PROFILE overrides.
AGENT_PROFILE_DIR = Path(os.environ.get("RESUME_TAILOR_AGENT_PROFILE") or Path.home() / ".resume-tailor" / "browser-profile-agent").expanduser()
_session = ApplySession(headless=os.environ.get("RESUME_TAILOR_HEADLESS", "0") == "1", profile_dir=AGENT_PROFILE_DIR)
_profile: Profile | None = None


def _get_profile() -> Profile:
    global _profile
    if _profile is None:
        _profile = Profile.load()
    return _profile


# --- tailoring ------------------------------------------------------------

@mcp.tool()
async def tailor_resume(job_description: str, style: str = "clean", label: str = "role") -> str:
    """Generate a resume tailored to one job description and render it to PDF.

    Returns JSON with the PDF path, an ATS keyword-coverage report, and any
    requirements the career record does not support. Pass the full text of the
    posting, not a URL.
    """
    from .tailor import tailor

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})

    result = await tailor(
        profile=profile,
        job_description=job_description,
        style=style,
        out_dir=OUT_DIR,
        label=label,
    )
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool()
async def render_resume_pdf(body_html: str, out_name: str, style: str = "clean") -> str:
    """Render resume HTML to a styled, ATS-parseable PDF. Returns the path."""
    path = await render_pdf_async(body_html, OUT_DIR / f"{out_name}.pdf", style=style)
    return str(path)


@mcp.tool()
def list_styles() -> str:
    """List available resume CSS themes."""
    return json.dumps(sorted(available_styles().keys()))


# --- the answer bank ------------------------------------------------------

@mcp.tool()
def answer_form_question(question: str) -> str:
    """Look up one application-form question in the answer bank.

    Returns {"answer": ..., "source": ...} on a hit, or {"answer": null} on a
    miss. A miss means ask the user — never invent the value.
    """
    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    answer, key = profile.lookup(question)
    return json.dumps({"answer": answer, "source": key, "question": question})


@mcp.tool()
def list_known_answers() -> str:
    """Every question the answer bank can answer, as key -> value."""
    try:
        return json.dumps(_get_profile().flat_answers(), indent=2)
    except ProfileError as e:
        return json.dumps({"error": str(e)})


# --- the browser ----------------------------------------------------------

@mcp.tool()
async def browser_open(url: str) -> str:
    """Open a URL in the persistent browser. Logins survive across runs."""
    return await _session.goto(url)


@mcp.tool()
async def browser_read() -> str:
    """Visible text of the current page — use this to read a job posting."""
    return (await _session.read_text())[:60000]


@mcp.tool()
async def describe_form() -> str:
    """Every visible form field with its label, type, options and required flag."""
    return json.dumps(await _session.describe_form(), indent=2)


@mcp.tool()
async def fill_field(selector: str, value: str) -> str:
    """Fill one field. Use a selector from describe_form. Prose (forty words
    or more, or with a line break) is held to the writing floor
    (banned_words): a value that breaks it is refused, not typed — use the
    bank's words, never your own."""
    if len(value.split()) >= 40 or "\n" in value:
        from . import freetext
        try:
            bad = freetext.violations(value, _get_profile())
        except Exception:
            bad = []
        if bad:
            return json.dumps({"filled": False, "selector": selector, "violations": bad,
                               "note": "rewrite without these, or use the bank's words; see banned_words()"})
    await _session.fill(selector, value)
    return f"filled {selector}"


@mcp.tool()
async def attach_file(selector: str, file_path: str) -> str:
    """Attach a file to a file input — the resume PDF, typically.

    This is the step a browser-tool server cannot do for you: browsers reject
    synthetic events on file inputs, so it must run in-process via Playwright.
    """
    await _session.upload(selector, file_path)
    return f"attached {Path(file_path).name} to {selector}"


@mcp.tool()
async def click(selector: str) -> str:
    """Click an element. Use list_buttons to find one."""
    await _session.click(selector)
    return f"clicked {selector}"


@mcp.tool()
async def list_buttons() -> str:
    """Every visible button on the page, with its text."""
    return json.dumps(await _session.buttons(), indent=2)


@mcp.tool()
async def screenshot(name: str = "page") -> str:
    """Full-page screenshot. Returns the path."""
    return str(await _session.screenshot(OUT_DIR / f"{name}.png"))


@mcp.tool()
async def review_before_submit() -> str:
    """Pre-submit check: current values of every field, plus required ones still empty.

    Read this before calling submit. It is the last point at which a wrong
    answer costs nothing to fix.
    """
    fields = await _session.describe_form()
    missing = await _session.unfilled_required()
    shot = await _session.screenshot(OUT_DIR / "pre-submit.png")
    return json.dumps(
        {"fields": fields, "unfilled_required": missing, "screenshot": str(shot),
         "buttons": await _session.buttons()},
        indent=2,
    )


@mcp.tool()
async def submit_application(selector: str, confirm: str = "") -> str:
    """Submit the application by clicking `selector`.

    Requires confirm="yes-submit" so a submit is always a deliberate call and
    never something the model drifts into. Call review_before_submit first.
    """
    if confirm != "yes-submit":
        return json.dumps({
            "submitted": False,
            "reason": "Pass confirm='yes-submit' to actually submit. Call review_before_submit first.",
        })
    missing = await _session.unfilled_required()
    if missing:
        return json.dumps({
            "submitted": False,
            "reason": "Required fields are still empty.",
            "unfilled_required": missing,
        })
    await _session.click(selector)
    try:
        await _session._page.wait_for_timeout(2500)  # the page's own confirmation takes a moment
    except Exception:
        pass
    shot = await _session.screenshot(OUT_DIR / "post-submit.png")
    text = (await _session.read_text())[:4000]
    try:
        await _session._pick_frame()
        fields = await _session.describe_form()
    except Exception:
        fields = []
    form_gone = not fields or not _looks_like_application(fields)
    # The only proof a submit went through is the form gone and a
    # confirmation in its place. A page that still shows the form was not
    # sent — and is never clicked again blind: inbox_confirmation first.
    return json.dumps({"submitted": True, "form_gone": form_gone, "confirmation_seen": bool(_SUCCESS_TEXT.search(text)),
                       "screenshot": str(shot), "page_text": text[:2000],
                       "note": ("went through: the form is gone and the page confirms it" if form_gone and _SUCCESS_TEXT.search(text)
                                else "the form is gone but no confirmation text was read — check the page text, then inbox_confirmation" if form_gone
                                else "the form is still on the page: not sent. Do not click again; read the page for an error, "
                                     "fix it, or record needs_review")})


# --- unattended batch ------------------------------------------------------

@mcp.tool()
async def run_batch_apply(queue_path: str, dry_run: bool = False, headless: bool = True,
                          pause_seconds: float = 15.0, retry_all: bool = False) -> str:
    """Apply to every posting in a queue file, unattended — no per-application approval.

    Uses its own browser session, separate from browser_open/describe_form above.
    A required field the answer bank cannot resolve skips that one posting and
    logs why, rather than guessing; a login wall or a bot/CAPTCHA check does the
    same. Set dry_run=True first to fill and screenshot every form without
    clicking submit. Safe to re-run — entries already recorded are skipped
    unless retry_all=True.

    Returns a JSON summary. The full per-posting report is a CSV at
    <out_dir>/batch-report.csv; screenshots are under <out_dir>/screenshots/.
    """
    from .batch import run_batch

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})

    summary = await run_batch(
        profile=profile, queue_path=queue_path, out_dir=OUT_DIR,
        dry_run=dry_run, headless=headless, pause_seconds=pause_seconds, retry_all=retry_all,
    )
    return json.dumps(summary, indent=2)


@mcp.tool()
def discover_internships(limit: int = 40) -> str:
    """New Summer 2027 software postings from the SimplifyJobs list that match
    the search settings in answers.yaml and haven't been attempted yet.

    Returns JSON: the postings in the order they would be attempted (direct-form
    ATS hosts first, account-gated hosts last) plus a tally of why the rest were
    left out. Read-only — nothing is applied to.
    """
    from .discover import Prefs, refresh, select, sources_from_profile
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    feed, tables = sources_from_profile(profile.answers)
    listings, changed = refresh(OUT_DIR, table_sources=tables, feed=feed)
    entries, excluded = select(listings, Prefs.from_profile(profile), RunState.load(OUT_DIR / "batch-state.json"), limit=limit)
    return json.dumps({
        "listings_total": len(listings), "source_changed": changed,
        "to_apply": [{"company": e.company_hint, "title": e.title, "url": e.url, "apply_url": e.apply_url} for e in entries],
        "left_out": excluded,
    }, indent=2)


@mcp.tool()
async def apply_new_internships(dry_run: bool = False, max_per_run: int = 25, headless: bool = True) -> str:
    """One pass of the unattended loop: fetch the SimplifyJobs list, pick the new
    matching postings, tailor a resume to each and apply. No per-application
    approval. Unanswerable required fields, login walls and CAPTCHAs each skip
    that one posting and are logged. dry_run=True fills and screenshots without
    submitting. Returns the pass summary; batch_report has the per-posting detail.
    """
    from .batch import run_batch
    from .discover import Prefs, refresh, select, sources_from_profile
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    feed, tables = sources_from_profile(profile.answers)
    listings, changed = refresh(OUT_DIR, table_sources=tables, feed=feed)
    entries, excluded = select(listings, Prefs.from_profile(profile), RunState.load(OUT_DIR / "batch-state.json"), limit=max_per_run)
    if not entries:
        return json.dumps({"listings_total": len(listings), "new": 0, "left_out": excluded})
    summary = await run_batch(profile=profile, entries=entries, out_dir=OUT_DIR, dry_run=dry_run, headless=headless)
    summary.update({"listings_total": len(listings), "new": len(entries), "left_out": excluded})
    return json.dumps(summary, indent=2)


# --- the assist lane ------------------------------------------------------

@mcp.tool()
def assist_queue(limit: int = 10, statuses: str = "", include_old: bool = False) -> str:
    """The postings worth the driver's time, best first: filled forms
    awaiting a click, then the postings the loop handed over unopened
    (for_agent: Workday, Oracle, SmartRecruiters, Lever, iCIMS, custom
    sites), then forms held on a field the loop would not guess, then
    sign-in walls, then captchas. Prefer assist_next, which batches by portal. Each row says why it stopped, which
    fields are unresolved, whether it needs the user (a captcha, a login),
    and where the PDF and screenshot are. Companies already applied to and
    postings the list says are gone are left out; so are postings older than
    45 days unless include_old. Read-only. Work one at a time with
    assist_brief, then assist_record.
    """
    from . import assist
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    state = RunState.load(OUT_DIR / "batch-state.json")
    wanted = tuple(s.strip() for s in statuses.split(",") if s.strip()) or assist.ASSIST_STATUSES
    rows = assist.queue(state, OUT_DIR, profile, limit=limit, statuses=wanted, max_age_days=0 if include_old else assist.MAX_AGE_DAYS)
    return json.dumps({"count": len(rows), "postings": rows}, indent=2)


@mcp.tool()
def assist_brief(entry_id: str) -> str:
    """Everything needed to finish one stuck posting: the URL, why the loop
    stopped, the unresolved fields, the PDF to attach, the screenshot it
    left, and every answer it decided with its source — reuse those answers;
    anything else comes from answer_form_question or from the user."""
    from . import assist
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    state = RunState.load(OUT_DIR / "batch-state.json")
    b = assist.brief(state, OUT_DIR, entry_id, profile)
    return json.dumps(b if b else {"error": f"no record {entry_id!r} in {OUT_DIR / 'batch-state.json'}"}, indent=2)


@mcp.tool()
async def assist_record(entry_id: str, status: str, detail: str = "", company: str = "", role: str = "") -> str:
    """Record the driver's outcome for a posting, so the loop never touches
    it again: applied (submitted and proven — also marks the company applied
    to, for the one-application rule), by_hand, skipped (gone, closed, not
    worth it), needs_review (handed back with a note naming the field),
    needs_login (a sign-in the user must do in the driver's window) or
    blocked (a captcha stood). `detail` is a few words on what happened.
    On applied, the browser's cookies for the portal are saved so every
    later session there starts signed in."""
    from . import assist
    from .queue import RunState

    state = RunState.load(OUT_DIR / "batch-state.json")
    try:
        rec = assist.record(state, entry_id, status, detail, company, role)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    saved = 0
    if status == "applied" and _session._ctx is not None:
        try:
            saved = await _session.save_logins()
        except Exception:
            saved = 0
    return json.dumps({"ok": True, "status": rec["status"], "detail": rec["detail"], "company": rec.get("company"),
                       "cookies_saved": saved}, indent=2)


@mcp.tool()
def intake_facts() -> str:
    """Every personal fact the user answered once in `resume-tailor intake`
    (address, zip, permanent address, birthday, college, high school, employer,
    availability, demographics defaults…) plus what the record already held,
    as key → value, and the keys still missing. Call this before driving a
    batch and fill forms from it; never stop to ask the user for a fact that
    is here. A missing one is noted with note_pending, not asked."""
    from . import intake

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    st = intake.status(profile.root)
    return json.dumps({"facts": {r["key"]: r["value"] for r in st["facts"] if r["value"] is not None},
                       "missing": st["missing"]}, ensure_ascii=False)


@mcp.tool()
def note_pending(question: str, company: str = "", role: str = "", url: str = "", entry_id: str = "", tab: str = "") -> str:
    """Write down a question nobody can answer from the facts, the bank or
    the record (an essay in the user's own words, a video, a preference never
    given) instead of stopping the run: leave the form open in its tab, call
    this, record the posting needs_review, and move to the next posting. The
    user sees the list with `resume-tailor pending`."""
    from . import pending

    row = pending.add(OUT_DIR, question, company=company, role=role, url=url, entry_id=entry_id, tab=tab)
    return json.dumps({"ok": True, "n": row["n"], "file": str(OUT_DIR / pending.FILE)})


@mcp.tool()
def assist_learn(question: str, answer: str) -> str:
    """Keep an answer the user gave for a form question the bank missed, in
    answers.yaml → hand_answers. The bank finds it first on every later form
    that asks the same question. Only what the user said, in their words —
    never an answer you reasoned out yourself."""
    from . import assist

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    path = assist.learn(profile.root, question, answer)
    global _profile
    _profile = None  # reread on the next call, so the new answer is live at once
    return json.dumps({"ok": True, "file": str(path), "question": question, "answer": answer})


@mcp.tool()
def assist_next(size: int = 5, portal: str = "", include_old: bool = False) -> str:
    """The driver's next batch: up to `size` postings on one portal (an ATS
    kind such as workday, or a host), best portal first — so a sign-in done
    for the first posting holds for the rest and a quirk learned on one form
    applies to the next. Also lists every portal with its count. Empty
    `postings` means the queue is done: delete .claude/keep-going and stop.
    Work each posting start to finish (browser_open → page_verdict →
    assist_prepare → fill → review_before_submit → submit_application →
    assist_record), then call this again."""
    from . import assist
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    state = RunState.load(OUT_DIR / "batch-state.json")
    batch = assist.next_batch(state, OUT_DIR, profile, size=size, portal=portal,
                              max_age_days=0 if include_old else assist.MAX_AGE_DAYS)
    return json.dumps(batch, indent=2)


@mcp.tool()
async def assist_prepare(entry_id: str, posting_text: str = "") -> str:
    """The résumé and the two judges for one posting, run when the driver
    gets to it (the loop's own fit step, so nothing is spent on a posting
    never reached). Reads the posting from the page the browser is on —
    browser_open the posting first — unless `posting_text` is given. A PDF
    and verdict already on file and still current are reused. Returns the
    PDF path to attach and the verdict; a `status` of fit_rejected, skipped
    or needs_review means the posting is closed (already recorded as the
    driver's) — move on. A re-queued posting is prepared whatever the judges say."""
    from . import assist
    from .batch import posting_text_on_page
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    text = posting_text or ""
    if not text.strip():
        try:
            await _session._pick_frame()
            text = await posting_text_on_page(_session)
        except Exception as e:
            return json.dumps({"error": f"could not read the posting from the open page: {e}"})
    state = RunState.load(OUT_DIR / "batch-state.json")
    res = await assist.prepare(state, OUT_DIR, entry_id, profile, text)
    return json.dumps(res, indent=2, default=str)


@mcp.tool()
async def page_verdict() -> str:
    """What kind of page the browser is on, by the loop's own classifier:
    login_required, bot_check (a captcha: never solve it — record blocked or
    ask the user), posting_gone, already_applied, no_form_found, or form (a
    fillable application). With no form found, one click on an Apply
    control is tried and the page is classified again (`apply_clicked`).
    Call this right after browser_open, before describing the form."""
    try:
        await _session._pick_frame()
        verdict = await detect_blocker(_session)
        clicked = False
        if verdict == "no_form_found":
            clicked = await _session.click_apply_control()
            if clicked:
                await _session._pick_frame()
                verdict = await detect_blocker(_session, after_apply=True)
        fields = await _session.describe_form() if verdict is None else []
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"verdict": verdict or "form", "apply_clicked": clicked,
                       "url": _session._page.url if _session._page is not None else "",
                       "fields": len(fields), "looks_like_application": bool(fields) and _looks_like_application(fields)})


@mcp.tool()
async def portal_account() -> str:
    """On a portal's sign-in wall: the loop's own account step — create the
    account with the applicant's e-mail and the site password, or sign in
    when it exists — but only on hosts the user listed in
    search.create_accounts_on; anywhere else it does nothing and says so
    (record needs_login and move on). Returns how it went: created,
    signed_in, captcha (never solve it: record blocked), verify_email (the
    portal mailed a link: record needs_login), or wall_stayed. Call
    page_verdict again after created / signed_in."""
    from .batch import _accounts_allowed, _create_account

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    url = _session._page.url if _session._page is not None else ""
    if not _accounts_allowed(profile, url, url):
        return json.dumps({"how": "not_allowed", "url": url,
                           "note": "this host is not in search.create_accounts_on — no account is made or signed in; record needs_login"})
    try:
        how = await _create_account(_session, profile)
    except Exception as e:
        return json.dumps({"how": "error", "error": str(e), "url": url})
    if how in ("created", "signed_in"):
        try:
            await _session.save_logins()
        except Exception:
            pass
    return json.dumps({"how": how or "wall_stayed", "url": _session._page.url if _session._page is not None else url})


@mcp.tool()
async def wait_for_you(kind: str = "captcha", minutes: int = 10) -> str:
    """Hold the page for the user to pass a wall in the driver's own window —
    `kind` is "captcha", "sign-in" or "code" (an e-mailed one-time code the
    inbox did not yield, for them to type) — for up to `minutes`, with a desktop
    notification and a banner on the dashboard; returns whether they did.
    Only when the user has said this session that they are at the keyboard;
    otherwise record needs_login / blocked and move on. After a passed
    sign-in the portal's cookies are saved for every later session."""
    from .batch import _wait_for_human

    if kind not in ("captcha", "sign-in", "code"):
        return json.dumps({"error": "kind is 'captcha', 'sign-in' or 'code'"})
    passed = await _wait_for_human(_session, None, kind, None, timeout_s=max(1, int(minutes)) * 60, force=True)
    if passed and kind == "sign-in":
        try:
            await _session.save_logins()
        except Exception:
            pass
    return json.dumps({"passed": passed, "kind": kind, "url": _session._page.url if _session._page is not None else ""})


@mcp.tool()
def banned_words() -> str:
    """The dictionary of words and phrases never written into an application:
    the writing floor (freetext.BANNED_DEFAULT) plus the user's own
    additions (answers.yaml → writing.banned_phrases; `resume-tailor words
    --add`), and the rules beside it. fill_field refuses prose that breaks
    the floor. Read it before typing any sentence that is not the bank's."""
    from . import freetext

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"banned": freetext.banned_phrases(profile),
                       "rules": ["no em dash (—): write the plain sentence, or a comma",
                                 "never volunteer a weakness (what the applicant has not done, lacks or would be learning)",
                                 "numbers, names, dates and durations only as the bank or the record states them",
                                 "never close by saying the applicant wants a summer or the internship: name the work"]}, indent=2)


@mcp.tool()
async def eligibility_check(posting_text: str = "") -> str:
    """The deterministic eligibility read (eligibility.py) on the posting the
    browser is on, or on `posting_text`: verdict bar / ok with the sentence
    that bars, and a tier-4 signal (seed-stage, a tiny team) if any. Free —
    no model. assist_prepare runs it too; call this alone when a posting
    looks doubtful before anything is spent on it."""
    from . import eligibility
    from .batch import posting_text_on_page

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    text = posting_text or ""
    if not text.strip():
        try:
            await _session._pick_frame()
            text = await posting_text_on_page(_session)
        except Exception as e:
            return json.dumps({"error": f"could not read the posting from the open page: {e}"})
    verdict, why = eligibility.scan(text, eligibility.facts_from_profile(profile))
    return json.dumps({"verdict": verdict, "why": why, "tier4_signal": eligibility.tier4_signal(text),
                       "skip_tier4": eligibility.skip_tier4(profile)}, indent=2)


@mcp.tool()
async def inbox_confirmation(company: str, since_minutes: int = 30) -> str:
    """Whether the applicant's inbox holds an acknowledgement of an
    application at `company` received in the last `since_minutes` (one IMAP
    look of about twenty seconds). The check to make before any second
    submit: a page can show an error over a submission that landed."""
    from . import mailbox

    if not mailbox.configured():
        return json.dumps({"configured": False, "confirmed": False, "note": "no inbox configured (RESUME_TAILOR_IMAP_*)"})
    try:
        found = await mailbox.confirmation_async(time.time() - since_minutes * 60, company, timeout_s=20)
    except Exception as e:
        return json.dumps({"configured": True, "confirmed": False, "error": str(e)})
    return json.dumps({"configured": True, "confirmed": bool(found), "subject": found or ""})


@mcp.tool()
async def inbox_code(company: str, since_minutes: int = 10, wait_seconds: int = 120) -> str:
    """The one-time code (or continue link) the applicant's inbox received in
    the last `since_minutes` for `company`, polling for up to `wait_seconds`:
    Greenhouse's security code after Submit, Oracle's verification code
    before the form. Read over IMAP, never guessed; `enter_code` types it."""
    from . import mailbox

    if not mailbox.configured():
        return json.dumps({"configured": False, "code": "", "link": "", "note": "no inbox configured (RESUME_TAILOR_IMAP_*)"})
    try:
        found = await mailbox.fetch_secret_async(time.time() - max(1, since_minutes) * 60, [company, "security code", "verification", "code"],
                                                timeout_s=max(0, min(int(wait_seconds), 300)))
    except Exception as e:
        return json.dumps({"configured": True, "code": "", "link": "", "error": str(e)})
    return json.dumps({"configured": True, "code": (found or {}).get("code") or "", "link": (found or {}).get("link") or ""})


@mcp.tool()
async def enter_code(code: str) -> str:
    """Type a one-time code from `inbox_code` into the code boxes on the open
    page (one character per box where the page splits it) and press the
    control that verifies it; then the page as it stands: whether boxes are
    still asking, the fields left, the first lines of text. A code not read
    from the inbox is never typed."""
    from .batch import _code_fields, _enter_code

    code = (code or "").strip()
    if not code:
        return json.dumps({"error": "no code"})
    boxes = await _code_fields(_session)
    if not boxes:
        return json.dumps({"entered": False, "note": "no code boxes on the page"})
    entered = await _enter_code(_session, code)
    await _session._page.wait_for_timeout(1500)
    try:
        still = bool(await _code_fields(_session))
        fields = len(await _session.describe_form())
        text = (await _session.read_text())[:600]
    except Exception:
        still, fields, text = True, -1, ""
    return json.dumps({"entered": entered, "boxes": len(boxes), "still_asking": still, "fields": fields, "page_text": text,
                       "url": _session._page.url if _session._page is not None else ""})


@mcp.tool()
def batch_report() -> str:
    """The results of the most recent batch run — status and detail per posting."""
    path = OUT_DIR / "batch-report.csv"
    if not path.is_file():
        return json.dumps({"error": f"no report at {path} yet — run run_batch_apply first"})
    return path.read_text(encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
