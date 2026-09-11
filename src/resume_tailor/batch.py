"""Unattended application to a list of postings. No per-application approval —
that is the point of this module. What replaces approval is that anything the
answer bank cannot resolve gets skipped and logged rather than guessed, and
every action is written to a report and a screenshot before the run moves on.

What this will not do: create a site account, solve a CAPTCHA, or fabricate an
answer to a required question the profile does not cover. Each of those stops
one job and logs why; none of them stop the batch.
"""
from __future__ import annotations

import asyncio
import csv
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import ats, freetext, judge, mailbox, planner, qa
from .apply import (ApplySession, _SIGNIN_TEXT, _is_empty, _looks_like_application, _pick_option, _value_parts, _YES_WORDS,
                    closest_option, detect_blocker)
from .planner import Decision
from .profile import Profile, gpa_answer, sponsorship_answer
from .render import extract_pdf_text
from .queue import QueueEntry, RunState, company_key, load_queue
from .tailor import tailor

# Outcomes that are worth re-attempting on a later run without --retry-all —
# transient errors and the two blockers a human can actually clear in between
# runs (logging into a site, noticing a CAPTCHA and doing that one by hand).
RETRYABLE = {"error", "needs_login", "blocked", "ready_not_submitted", "needs_review"}
# Filled and screenshotted, waiting for the user's word before it is sent —
# the companies named in `search.approve_before_submit`. Not retried by a
# pass; the dashboard's Approve button submits it.
AWAITING = "awaiting_approval"
# ready_not_submitted is a dry run's verdict, not an application — it must not
# make the next real pass skip that posting. needs_review is retried because the
# answer bank grows between passes; the resume and its verdicts are cached, so
# a retry costs a page visit, not a tailoring. by_hand (a form the user opened
# and closed) and skipped are deliberately absent: the loop leaves those alone.

_SUBMIT_WORDS = re.compile(r"\b(submit|apply|send)\b", re.I)
# Pickers that search a large index as you type — the opening list is a
# handful of suggestions, not the set of answers.
_SEARCH_PICKER = re.compile(r"\b(location|located|where (are|do) you|city|town|address|region|country|school|university|college|institution|employer|company)\b", re.I)


def _is_search_picker(field: dict) -> bool:
    """A picker whose list is an index searched as you type — by its label
    (a place, a school, an employer) or by the widget itself (Workday's
    selectinput with its "Search" placeholder: Field of Study, Skills)."""
    label = field.get("label") or ""
    if re.search(r"prefer|office|which|choose|select (a|an|your)", label, re.I) and not field.get("search"):
        return False  # "Top location preference": a fixed list of offices, read like any other
    return bool(field.get("combobox")) and (bool(field.get("search")) or bool(_SEARCH_PICKER.search(label)))
# Facts about one entry of a work or language block that only the plan may
# decide, with the entry in view: never the bank ("currently enrolled: Yes"
# once ticked "I currently work here"), never an acknowledgement rule.
_NO_BANK = re.compile(r"currently work here|currently (employed|attend)|i am fluent", re.I)
_CURRENT_ROLE = re.compile(r"currently work here|current(ly)? (role|position|employ)|i still work here|present position", re.I)
_LEAVE_BLANK = re.compile(r"middle (name|initial)|phone extension|\bext(ension)?\.?\b|name suffix|\bsuffix\b|address line ?2|apartment|apt\.?\b|unit number|suite", re.I)
_SELF_ID = re.compile(r"self-?identif|eeo|equal employment|diversity|transgender|sexual orientation|hispanic|latino|ethnicity|"
                      r"\brace\b|veteran|disability|\bgender\b|pronoun", re.I)
_DECLINE_WORDS = ("decline", "prefer not", "do not wish", "don't wish", "not to answer", "rather not", "choose not",
                  "not to say", "not to disclose", "do not want", "don't want", "not want to")
_ACK_OPTION = re.compile(r"^\s*(yes|i understand|understood|i acknowledge|acknowledged|ok|okay|i agree|agree|i confirm|confirm|"
                         r"accept|i accept|i have read.*)\s*[.!]?\s*$", re.I)
# A statement put to the applicant as a required field is an acknowledgement
# whatever it says: "We will only consider you for one role at a time…"
# (Optiver), "Reminder that you can only apply for one role" (HRT).
_CONFERENCE = re.compile(r"conference|career fair|affiliations? with the (below|following) groups", re.I)
_SOCIO = re.compile(r"household earner|free school meals|highest level of education completed by|parents?'? (highest )?(level of )?education|socio-?economic|first.?generation|primary caregiver", re.I)
_LANG_SKILL = re.compile(r"language skills in|proficiency in (reading|writing|speaking)|indicate your language|skills in reading, written and spoken", re.I)
_CONSENT_WORDS = re.compile(r"\b(certify|agree|confirm|accura|consent|acknowledge|terms|policy|code of conduct|i understand|scheduling tool|"
                            r"please note|reminder that|we will only consider|first preference|one (role|position|application)s? at a time)\b", re.I)
# "How did you hear about us?" — and not "if you were referred, name the
# person", which is a different question with a factual answer.
_SOURCE_FIELD = re.compile(
    r"\b(how|where|when) (did you|you)( first| originally)? (hear|heard|find|found|learn|learned)( about| of)?\b"
    r"|\b(what|which) source\b.*\b(hear|heard|learn|learned|find|found)\b|\bsource of (this )?application\b"
    r"|\breferral source\b|\bapplication source\b", re.I)
# In order of preference: the postings come from a job board (the SimplifyJobs
# list), so those options are true; "Other" is true of anything; LinkedIn is not.
_GENERIC_SOURCE_OPTIONS = ("job board", "job site", "job posting", "other", "internet", "online")
_SOURCE_FREE_TEXT = "Job board (SimplifyJobs internship list)"


@dataclass
class Outcome:
    entry_id: str
    status: str  # applied | tailored_only | ready_not_submitted | skipped | needs_login | needs_review | blocked | error
    detail: str = ""
    company: str = ""
    role: str = ""
    pdf: str = ""
    coverage: dict = field(default_factory=dict)
    screenshot: str = ""
    fit: str = ""  # the two judges' scores, e.g. "hiring manager 74/100 pass | screener 68/100 pass"
    when: str = ""  # when the attempt finished, local time, ISO
    answers: list = field(default_factory=list)  # the form as it stood at the end: question, answer, where it came from
    resume_version: int = 0  # which generation of the résumé composer made the cached PDF
    revisions: int = 0  # how many times the résumé was revised from the judges' notes before this verdict
    worker: str = ""  # which parallel loop handled it ("0".."3", "fresh"), for the dashboard


# Bumped whenever the résumé composer or renderer changes in a way that makes
# earlier PDFs wrong to send; a retry whose cached PDF is older is re-tailored.
#  2: bullets can no longer render as ", won the…" (empty action); Letter page size.
#  3: the career record grew from a one-page transcription to the full inventory
#     (2026-09-03 evening) — a résumé drafted before that misses most of it.
#  4: the résumé is composed on the user's own skeleton (resume/base.yaml) in
#     the LaTeX layout and uploaded as First_Last_resume.pdf (2026-09-07) — an
#     earlier draft has the old shape and a company-named file.
RESUME_VERSION = 4


# Wording that says a control is not the one that sends the application,
# whatever else it says: another site's autofill, a draft, a step back.
_NOT_SUBMIT = re.compile(
    r"apply (with|using|via|through)|autofill|linkedin|indeed|dropbox|google drive|\bsave\b|draft|cancel|\bback\b|"
    r"previous|alert|search|filter|later|share|locate|enter manually|attach|add another|sign in|log ?in|"
    r"create (an )?account|withdraw|delete|remove|print|email this|friend", re.I)


def _pick_submit_button(buttons: list[dict]) -> dict | None:
    """A guess here is worse than a skip — clicking the wrong button can cancel
    or save-as-draft instead of submitting. Proceed only when exactly one
    visible button plausibly means submit.

    Plausible, in order of strength: a button that says "submit"; a
    type=submit button; anything that says apply or send. Greenhouse shows an
    "Apply" pill at the top of the posting that merely scrolls to the form,
    above the form's own "Submit application" — the strongest tier decides,
    and only when it holds exactly one button, or exactly one inside the form."""
    candidates = [b for b in buttons if not b.get("disabled") and _SUBMIT_WORDS.search(b.get("text", ""))
                  and not _NOT_SUBMIT.search(b.get("text", ""))]

    def tier(b: dict) -> int:
        if re.search(r"\bsubmit\b", b.get("text", ""), re.I):
            return 0
        if b.get("type") == "submit":
            return 1
        return 2

    for t in range(3):
        at_tier = [b for b in candidates if tier(b) == t]
        if len(at_tier) == 1:
            return at_tier[0]
        if at_tier:
            inside = [b for b in at_tier if b.get("in_form")]
            if len(inside) == 1:
                return inside[0]
            return None  # several equally plausible buttons: refuse
    return None


async def _ask_button(buttons: list[dict], purpose: str) -> dict | None:
    """When the wording on the page settles nothing, the model reads the
    controls as a person would: which one submits the form, or moves it on."""
    from .models import ControlChoice
    from .tailor import _parse

    cands = [b for b in buttons if not b.get("disabled") and b.get("text")][:40]
    if not cands:
        return None
    listing = "\n".join(
        f'{b["id"]}: "{b["text"][:60]}" (type={b.get("type") or "-"}, inside the form={"yes" if b.get("in_form") else "no"})'
        for b in cands)
    want = ("submits the completed application" if purpose == "submit"
            else "moves a multi-step application on to its next page")
    try:
        out = await _parse(
            [{"type": "text", "text": (
                "You operate a job application form in a browser and must choose one control from a list. "
                "Never choose a control that saves a draft, cancels, goes back, withdraws, autofills from another "
                "site, attaches a file, opens sign-in, creates an account, or merely scrolls to the form. Answer "
                "'none' when no listed control does the job.")}],
            f"CONTROLS ON THE PAGE\n{listing}\n\nWhich id {want}?", ControlChoice, effort="low", role="judge")
    except Exception:
        return None
    return next((b for b in cands if b["id"] == (out.id or "").strip()), None)


async def _tick_invalid_boxes(session: ApplySession) -> int:
    """Checkboxes the page itself marks invalid after a rejected step. A box
    has no other value to offer, so ticking it is the only repair — and a
    box the page insists on is an acknowledgement, whatever its label says."""
    ids = {m.group(1) for e in await session.errors() for m in [re.match(r"(rt-\d+): invalid", e)] if m}
    if not ids:
        return 0
    ticked = 0
    from collections import Counter

    boxes = [f for f in await session.describe_form() if f.get("type") == "checkbox"]
    per_question = Counter((f.get("section") or "", f.get("label") or "") for f in boxes)
    for f in boxes:
        if not f.get("checked") and f.get("id") in ids:
            # One box of a choice list ("Yes, I have a disability / No / I
            # do not want to answer") is an answer, not an acknowledgement:
            # ticking the first flagged one once declared a disability.
            if per_question[(f.get("section") or "", f.get("label") or "")] > 1:
                continue
            if (_SELF_ID.search(f.get("label") or "") or _SELF_ID.search(f.get("option_label") or "")
                    or _NO_BANK.search(f.get("label") or "")):
                continue
            try:
                await session.fill(f["selector"], "yes", f)
                session._declined.discard(f["selector"])
                ticked += 1
            except Exception:
                pass
    return ticked


_CODE_WALL = re.compile(r"verification code|security code|code (was |has been )?sent|enter the code|one-time (code|password)|confirm your identity|"
                        r"verification (e-?mail|link)|verify your account|account-verification|\d-(character|digit) code|passcode", re.I)
# A portal that will not sign a new account in until its e-mail is verified
# (Medtronic's and Motorola's Workday tenants).
_VERIFY_WALL = re.compile(r"verify your account|account (may )?need(s)? verification|resend (account )?verification|"
                          r"verification e-?mail (has been |was )?sent|check your (e-?mail|inbox) to (verify|activate|confirm)", re.I)
# A page that sent the applicant an e-mail and now waits for its link to be
# opened ("check your inbox to continue"): nothing to type, a link to follow.
_LINK_WALL = re.compile(r"check your (e-?mail|inbox)|we('ve| have)? (sent|e-?mailed) (you )?(a|an) (link|e-?mail)|link to (continue|verify|sign in|log in)|"
                        r"e-?mail (has been|was) sent|sent (you )?an? e-?mail", re.I)


async def _link_wall(session: ApplySession) -> bool:
    try:
        text = await session.read_text()
    except Exception:
        return False
    return bool(_LINK_WALL.search(text[:6000])) and not await session.describe_form()


async def _code_fields(session: ApplySession) -> list[dict]:
    """The boxes a code wall wants typed into: empty, or holding one stray
    character (a repair round once put "1" in each of Oracle's six)."""
    return [f for f in await session.describe_form()
            if f.get("type") in ("text", "number", "tel", "") and len(str(f.get("value") or "")) <= 1
            and _CODE_WALL.search(" ".join((f.get("label") or "", f.get("name") or "", f.get("hint") or "")))]


async def _enter_code(session: ApplySession, code: str) -> bool:
    """Type a one-time code into the wall's boxes — one digit per box when
    the page splits it (Oracle's six), the whole code otherwise — and press
    the control that verifies it."""
    boxes = await _code_fields(session)
    if not boxes:
        return False
    page = session._page

    async def held() -> str:
        vals = []
        for b in boxes:
            try:
                vals.append(await session._doc.locator(b["selector"]).first.evaluate("e => e.value || ''"))
            except Exception:
                vals.append("")
        return "".join(vals)

    if len(boxes) >= len(code):
        # Six boxes that pass focus along as each character lands (Greenhouse's
        # security code): type the whole code into the first and let them
        # advance; a box left empty is filled on its own afterwards.
        try:
            await session._doc.locator(boxes[0]["selector"]).first.click()
            await page.keyboard.type(code, delay=60)
            await page.wait_for_timeout(400)
        except Exception:
            pass
        if len(await held()) < len(code):
            for box, ch in zip(boxes, code):
                try:
                    await session.fill(box["selector"], ch, box)
                except Exception:
                    pass
    else:
        await session.fill(boxes[0]["selector"], code, boxes[0])
    await page.wait_for_timeout(500)
    buttons = await session.buttons()
    verify = next((b for b in buttons if not b.get("disabled")
                   and re.search(r"\b(verify|confirm|continue|submit|next)\b", b.get("text", ""), re.I)), None)
    if verify is None:
        verify = await _ask_button(buttons, "next")
    if verify is None:
        return False
    # The control here ends the application (Greenhouse) or opens the next
    # step (Oracle): wait for either the form to go or a new step to draw.
    before = await _step_signature(session)
    try:
        await session._doc.locator(verify["selector"]).first.click(timeout=8000)
    except Exception:
        await _click_hard(session, verify["selector"])
    for _ in range(40):
        await page.wait_for_timeout(1500)
        try:
            if await _step_signature(session) != before or not await _code_fields(session):
                break
        except Exception:
            break
    await session._pick_frame()
    return True


def _accounts_allowed(profile: Profile, url: str) -> bool:
    """Whether the user has said the tool may create an account on this host
    (`search.create_accounts_on`, host suffixes)."""
    hosts = [str(h).strip().lower() for h in (profile.answers.get("search", {}).get("create_accounts_on") or []) if str(h).strip()]
    host = (urlsplit(url).netloc or "").lower()
    return any(host == h or host.endswith("." + h) or h in host for h in hosts)


def _site_password() -> str:
    import os

    from .llm import _load_env_file

    _load_env_file()
    return os.environ.get("RESUME_TAILOR_SITE_PASSWORD", "")


async def _click_hard(session: ApplySession, selector: str) -> bool:
    """Click a control that an overlay may be covering (Workday floats a
    click-filter div over its Create Account button): a normal click, then a
    forced one, then the element's own click handler."""
    loc = session._doc.locator(selector).first
    for attempt in ("normal", "force", "js"):
        try:
            if attempt == "normal":
                await loc.click(timeout=5000)
            elif attempt == "force":
                await loc.click(force=True, timeout=5000)
            else:
                await loc.evaluate("e => e.click()")
            return True
        except Exception:
            continue
    return False


async def _create_account(session: ApplySession, profile: Profile) -> str:
    """On a portal's sign-in wall: create the account with the applicant's
    e-mail and the site password, or sign in when the account exists. Returns
    "created", "signed_in" or "" (the wall is still there)."""
    from .apply import _APPLY_JS, detect_blocker

    password = _site_password()
    email = str(profile.career.get("personal_information", {}).get("email") or profile.flat_answers().get("personal.email") or "")
    if not password or not email:
        return ""
    started = time.time()

    async def controls():
        try:
            return await session._doc.evaluate(_APPLY_JS)
        except Exception:
            return []

    async def click_text(pattern: str) -> bool:
        for c in await controls():
            text = (c.get("text") or "").strip()
            if re.search(r"google|linkedin|apple|microsoft|facebook|\bsso\b|okta|single sign", text, re.I):
                continue  # another provider's sign-in, never the applicant's e-mail route
            if re.search(r"^(?:" + pattern + r")$", text, re.I) or re.search(pattern, text, re.I):
                if await _click_hard(session, c["selector"]):
                    await session._page.wait_for_timeout(1500)
                    await session._pick_frame()
                    return True
        # Not among the page's buttons and links as scanned: a plain span or
        # text link ("Need an email account? Create account" on TikTok's
        # sign-in). Find it by its words.
        try:
            rx = re.compile(pattern, re.I)
            for finder in (lambda: session._page.get_by_role("link", name=rx), lambda: session._page.get_by_role("button", name=rx),
                           lambda: session._page.get_by_text(rx)):
                loc = finder().first
                if await loc.count():
                    txt = (await loc.inner_text())[:80]
                    if re.search(r"google|linkedin|apple|microsoft|facebook", txt, re.I):
                        continue
                    await loc.click(timeout=4000)
                    await session._page.wait_for_timeout(1500)
                    await session._pick_frame()
                    return True
        except Exception:
            pass
        return False

    def emails_of(fields: list[dict]) -> list[dict]:
        return [f for f in fields if f.get("type") in ("text", "email")
                and re.search(r"e-?mail|username", (f.get("label") or "") + " " + (f.get("name") or ""), re.I)]

    def passwords_of(fields: list[dict]) -> list[dict]:
        return [f for f in fields if f.get("type") == "password"]

    async def put(f: dict, value: str) -> None:
        """Set one credential directly, on a control found again the instant
        before: Workday redraws the account form as the password rules light
        up, and a control tagged a moment earlier is gone with the redraw."""
        for attempt in range(3):
            try:
                loc = await session._locate(f["selector"], f)
                await loc.click(timeout=3000)
                await loc.fill(value, timeout=3000)
                return
            except Exception:
                if attempt == 2:
                    raise
                # A cookie overlay (OneTrust on AMD's iCIMS) takes the click:
                # clear it, or write into the box without the pointer.
                try:
                    await session._dismiss_cookie_banner()
                except Exception:
                    pass
                try:
                    loc = await session._locate(f["selector"], f)
                    await loc.fill(value, timeout=3000, force=True)
                    return
                except Exception:
                    pass
                await session._page.wait_for_timeout(500)
                same = [g for g in await session.describe_form()
                        if g.get("type") == f.get("type") and (g.get("label") or "") == (f.get("label") or "")]
                if same:
                    f = same[0]

    async def tick_boxes() -> None:
        """Every consent box on the registration form. Workday draws its
        privacy box as a styled control over a hidden input, and a plain
        click can miss it (Caterpillar, Medline: "Please check the box to
        continue"): what is still unchecked afterwards is forced."""
        for f in await session.describe_form():
            if f.get("type") == "checkbox" and not f.get("checked"):
                try:
                    await session.fill(f["selector"], "yes", f)
                except Exception:
                    pass
        for f in await session.describe_form():
            if f.get("type") == "checkbox" and not f.get("checked"):
                try:
                    await _click_hard(session, f["selector"])
                except Exception:
                    pass

    async def fill_credentials(verify: bool) -> bool:
        fields = await session.describe_form()
        if not emails_of(fields) or not passwords_of(fields) or (verify and len(passwords_of(fields)) < 2):
            return False
        for f in emails_of(fields):  # every e-mail box — "Retype Email Address" too (SuccessFactors)
            await put(f, email)
        n_pw = len(passwords_of(fields))
        for i in range(n_pw):
            fresh = passwords_of(await session.describe_form())
            if i < len(fresh):
                await put(fresh[i], password)
        # A registration form that also wants the name and the country
        # beside the credentials (SuccessFactors).
        person = profile.career.get("personal_information", {}) or {}
        first, last = str(person.get("name") or "").strip(), str(person.get("surname") or "").strip()
        for f in await session.describe_form():
            lab = (f.get("label") or "") + " " + (f.get("name") or "")
            if f.get("type") in ("text", "") and f.get("tag") != "select" and not f.get("value"):
                if first and re.search(r"first name|given name", lab, re.I):
                    await put(f, first)
                elif last and re.search(r"last name|surname|family name", lab, re.I):
                    await put(f, last)
            elif f.get("tag") == "select" and re.search(r"country", lab, re.I) and not f.get("value"):
                try:
                    await session.fill(f["selector"], "United States", f)
                except Exception:
                    pass
        await tick_boxes()
        # "Read and accept the data privacy statement": a control that opens
        # the statement, with its own Accept at the end.
        if await click_text(r"read and accept|accept (the )?(data )?privacy|privacy statement"):
            await session._page.wait_for_timeout(1500)
            await click_text(r"^\s*(accept|i accept|agree|i agree|acknowledge|i acknowledge|accept (and|&) continue|ok|okay)\s*$")
            await session._page.wait_for_timeout(800)
        return True

    async def press(pattern: str) -> bool:
        buttons = await session.buttons()
        btn = next((b for b in buttons if not b.get("disabled") and re.search(pattern, b.get("text", ""), re.I) and b.get("type") == "submit"), None) \
            or next((b for b in buttons if not b.get("disabled") and re.search(pattern, b.get("text", ""), re.I)), None)
        if btn is None:
            return False
        baseline = await session._fields_present()
        if not await _click_hard(session, btn["selector"]):
            return False
        await session._page.wait_for_timeout(2500)
        await session._wait_for_fields(20, baseline)
        return True

    # A cookie overlay over the wall (OneTrust on AMD's iCIMS) would take
    # every click below: clear it first.
    try:
        await session._dismiss_cookie_banner()
        await session._pick_frame()
    except Exception:
        pass
    # A captcha on the wall itself ("Confirm you are not a robot" on
    # Grainger's SuccessFactors, hCaptcha over an iCIMS e-mail step): no
    # account can be made here without a person, and the tool does not
    # solve captchas.
    try:
        if await session.challenge_visible():
            return "captcha"
    except Exception:
        pass
    # The create-account form, reached by its link when the sign-in form is
    # what shows; Workday shows both behind one "Create Account" toggle.
    fields = await session.describe_form()
    if not any(f.get("type") == "password" for f in fields):
        # A wall of sign-in routes (Google, LinkedIn, e-mail) before any
        # form — Medline's Workday: the e-mail route leads to the account form.
        if await click_text(r"sign in with e-?mail|continue with e-?mail|use (my )?e-?mail( address)?|apply with e-?mail"):
            fields = await session.describe_form()
    if not any(f.get("type") == "password" for f in fields) and emails_of(fields):
        # "Enter your e-mail to begin" (iCIMS's campus sites, Oracle, TikTok):
        # no password anywhere — the site mails a code or a link next. Give
        # it the e-mail, tick its consent box, press on; the caller reads the
        # inbox for what comes.
        await put(emails_of(fields)[0], email)
        for f in await session.describe_form():
            if f.get("type") == "checkbox" and not f.get("checked"):
                try:
                    await session.fill(f["selector"], "yes", f)
                except Exception:
                    pass
        if await press(r"next|continue|send (verification )?code|get code|sign in|log ?in|submit|begin|start"):
            await session._page.wait_for_timeout(2000)
            still = [f for f in await session.describe_form() if f.get("type") in ("email",) and not f.get("value")]
            if not still or await _code_fields(session) or await _link_wall(session):
                return "email_step"
    if not any(f.get("type") == "password" and re.search(r"verify|confirm|re-?enter", f.get("label") or "", re.I) for f in fields):
        await click_text(r"create (an )?account|sign up|register|new user")
    debug = bool(os.environ.get("RESUME_TAILOR_DEBUG"))

    def note(msg: str) -> None:
        if debug:
            print(f"  account: {msg}", file=sys.stderr, flush=True)

    created = False
    note(f"on {(_page_url(session) or '')[:80]}; fields={[(f.get('label') or '')[:18] + ':' + (f.get('type') or '') for f in fields][:8]}")
    real_pw = [f for f in fields if f.get("type") == "password"]  # not the show/hide "Password" button
    if emails_of(fields) and len(real_pw) == 1:
        # The wall is a sign-in form (SuccessFactors): an account made on an
        # earlier pass signs in here; registering first bounced to "already
        # exists" and then a "Sign In" nav link led off to a privacy page.
        if await fill_credentials(verify=False):
            pressed = await press(r"sign in|log in|^\s*(submit|continue)\s*$")
            if not pressed:
                try:
                    await session._doc.locator(real_pw[0]["selector"]).first.press("Enter")
                    pressed = True
                except Exception:
                    pass
            note(f"sign-in first pressed={pressed}; now on {(_page_url(session) or '')[:80]}")
            await session._page.wait_for_timeout(2500)
            after = (await session.read_text())[:3000]
            if re.search(r"privacy statement|data privacy|terms of use", after, re.I):
                # SuccessFactors puts its data-privacy statement between the
                # sign-in and the form: accept it and go on.
                if await click_text(r"^\s*(accept|i accept|agree|i agree|acknowledge|i acknowledge|accept (and|&) continue)\s*$"):
                    await session._page.wait_for_timeout(2000)
            if debug:
                bl = await session.buttons()
                note(f"after sign-in: fields={len(await session.describe_form())}; buttons={[(b.get('text') or '')[:18] for b in bl][:10]}; "
                     f"text={' '.join((await session.read_text())[:220].split())!r}")
            if await detect_blocker(session, after_apply=True) != "login_required":
                try:
                    await session.save_logins()
                except Exception:
                    pass
                return "signed_in"
            fields = await session.describe_form()
    if await fill_credentials(verify=True):
        note("registration form filled")
        pressed = await press(r"create (an )?account|sign up|register|^\s*create\s*$")
        note(f"create pressed={pressed}; now on {(_page_url(session) or '')[:80]}")
        if any(re.search(r"check the box|must (accept|agree|acknowledge)|accept the (terms|privacy|policy)", e, re.I)
               for e in await session.errors()):
            # The consent box was not taken: force it and press once more.
            note("consent box not taken; forcing it and pressing again")
            await tick_boxes()
            pressed = await press(r"create (an )?account|sign up|register|^\s*create\s*$")
            note(f"create pressed again={pressed}; errors={[e[:60] for e in (await session.errors())[:3]]}")
        text = (await session.read_text())[:5000].lower()
        if re.search(r"already (exists|in use|registered|have an account)|account exists", text):
            created = False
            note("the site says the account already exists")
        else:
            created = await detect_blocker(session, after_apply=True) != "login_required"
            note(f"created={created}; errors={[e[:60] for e in (await session.errors())[:3]]}")
    else:
        note("no registration form found (need e-mail + two password boxes)")
    if not created:
        # Sign in with the same credentials: the account was made on an earlier attempt.
        await click_text(r"sign in with email|use (my )?email|continue with email|sign in|log in|already have an account.*")
        await session._page.wait_for_timeout(1500)
        if re.search(r"privacy statement|data privacy", (await session.read_text())[:4000], re.I):
            # SuccessFactors shows its data-privacy statement on the way to
            # the sign-in form (Westinghouse): accept it, then sign in.
            if await click_text(r"^\s*(accept|i accept|agree|i agree|acknowledge|i acknowledge|accept (and|&) continue)\s*$"):
                await session._page.wait_for_timeout(2000)
            note(f"privacy statement on the way to sign-in; now fields={len(await session.describe_form())}")
        if await fill_credentials(verify=False):
            pressed = await press(r"sign in|log in|^\s*(submit|continue|next)\s*$")
            if not pressed:
                # No button by that name (SuccessFactors labels its submit
                # oddly): Enter in the password box sends the form.
                pw_box = next((f for f in await session.describe_form() if f.get("type") == "password"), None)
                if pw_box:
                    try:
                        await session._doc.locator(pw_box["selector"]).first.press("Enter")
                        pressed = True
                        await session._page.wait_for_timeout(2500)
                    except Exception:
                        pass
            note(f"sign-in pressed={pressed}; now on {(_page_url(session) or '')[:80]}; text={(await session.read_text())[:120]!r}")
    # A tenant that will not sign the new account in until its e-mail is
    # verified (Medtronic, Motorola): with the inbox configured, the
    # verification link is followed and the sign-in tried once more;
    # without it, the wall is named so the loop stops retrying it.
    if _VERIFY_WALL.search((await session.read_text())[:4000]):
        if mailbox.configured():
            # Workday mails the link once, when the account is made; a later
            # sign-in only offers to send it again ("Resend Account
            # Verification"). The sender names the tenant one way or another
            # (deluxe@otp.workday.com, CrewCentral_noreply@vanguardhr.com), so
            # only mail naming it in its sender or subject counts. Ask, wait
            # for it, and failing that take the link sent at creation if the
            # inbox still has it from the last few days.
            host = re.sub(r"^https?://([^/]+).*$", r"\1", _page_url(session) or "")
            tenant = host.split(".")[0] if host.endswith("myworkdayjobs.com") else ""
            require = [tenant] if len(tenant) >= 4 else None
            hints = ["verif", "activat", "confirm", "account"] + ([tenant] if tenant else [])
            asked_at = time.time()
            asked = await click_text(r"resend (account )?verification( e-?mail)?|resend( verification)? e-?mail|"
                                     r"send (the )?(verification )?(e-?mail|link) again")
            found = await mailbox.fetch_secret_async(asked_at if asked else started, hints, timeout_s=150, require=require)
            if not (found or {}).get("link"):
                found = await mailbox.fetch_secret_async(time.time() - 3 * 86400, hints, timeout_s=0, require=require)
            link = (found or {}).get("link")
            if link:
                try:
                    await session.goto(link)
                    await session._page.wait_for_timeout(1500)
                except Exception:
                    pass
                await click_text(r"sign in with email|use (my )?email|continue with email|sign in|log in")
                if await fill_credentials(verify=False):
                    await press(r"sign in|log in")
                if await detect_blocker(session, after_apply=True) != "login_required":
                    try:
                        await session.save_logins()
                    except Exception:
                        pass
                    return "verified"
        return "verify_email"
    blocker = await detect_blocker(session, after_apply=True)
    if blocker == "login_required":
        return ""
    try:
        await session.save_logins()
    except Exception:
        pass
    return "created" if created else "signed_in"


async def _step_signature(session: ApplySession) -> tuple:
    """What identifies the step a multi-page form is on: the URL and the
    questions it shows. Workday keeps one URL for all six steps and two of
    them can hold the same number of fields, so labels, not counts."""
    try:
        labels = tuple((f.get("label") or f.get("name") or "")[:40] for f in (await session.describe_form())[:24])
    except Exception:
        labels = ()
    return (_page_url(session), labels)


def _page_url(session: ApplySession) -> str:
    try:
        return session._page.url if session._page is not None else ""
    except Exception:
        return ""


def _disambiguate(fields: list[dict]) -> None:
    """Five controls all labelled "Education History" (Ashby's education
    block) would share one decision; the second and later get an ordinal."""
    seen: dict[tuple, int] = {}
    for f in fields:
        if f.get("type") in ("radio", "checkbox"):
            continue
        key = (f.get("section") or "", f.get("label") or "")
        if not key[1]:
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            f["label"] = f"{f['label']} ({seen[key]})"


def _document_prompt(label: str) -> str | None:
    """A required upload the profile has no file for may be a request for
    prose — a cover letter, or an essay whose prompt is the label itself.
    Those are written from the record like any other essay answer."""
    if re.search(r"cover letter|letter of (interest|motivation|intent)|motivation letter", label, re.I):
        return "cover letter"
    if re.search(r"portfolio|work samples?|project samples?", label, re.I):
        return "portfolio"
    if len(label) > 40 and re.search(r"essay|\bwords\b|prompt|please (answer|describe|tell|explain|share)|statement|"
                                     r"what is|why do|why are|describe", label, re.I):
        return "essay"
    return None


async def _write_portfolio(profile: Profile, out_dir: Path) -> Path | None:
    """A project portfolio straight from the record — every project with
    its technologies, dates and bullets, plus the research roles — rendered
    as a PDF. No model writes anything here; it is the record, laid out."""
    import html as _html
    from .render import render_pdf_async

    career = profile.career if isinstance(profile.career, dict) else {}
    pi = career.get("personal_information", {}) or {}
    projects = [p for p in (career.get("projects") or []) if p.get("name")]
    if not projects:
        return None
    parts = [f"<h1>{_html.escape(profile.full_name if isinstance(profile.full_name, str) else profile.full_name())} — Project Portfolio</h1>"]
    contact = " · ".join(str(pi.get(k)) for k in ("email", "github", "linkedin") if pi.get(k))
    if contact:
        parts.append(f"<p>{_html.escape(contact)}</p>")
    for p in projects:
        tech = ", ".join(str(x) for x in (p.get("technologies") or []))
        head = _html.escape(str(p["name"])) + (f" <small>({_html.escape(str(p.get('period')))})</small>" if p.get("period") else "")
        parts.append(f"<h2>{head}</h2>")
        if tech:
            parts.append(f"<p><em>{_html.escape(tech)}</em></p>")
        bullets = "".join(f"<li>{_html.escape(str(b))}</li>" for b in (p.get("bullets") or []))
        if bullets:
            parts.append(f"<ul>{bullets}</ul>")
        if p.get("link"):
            parts.append(f"<p>{_html.escape(str(p['link']))}</p>")
    research = [e for e in (career.get("experience_details") or []) if re.search(r"research|lab", str(e.get("industry", "")) + " " + str(e.get("company", "")), re.I)]
    if research:
        parts.append("<h2>Research</h2>")
        for e in research:
            parts.append(f"<p><strong>{_html.escape(str(e.get('position', '')))}</strong>, {_html.escape(str(e.get('company', '')))} ({_html.escape(str(e.get('employment_period', '')))})</p>")
            bl = "".join(f"<li>{_html.escape(str(r.get('responsibility', '')))}</li>" for r in (e.get("key_responsibilities") or []))
            if bl:
                parts.append(f"<ul>{bl}</ul>")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"portfolio-{datetime.now():%Y%m%d}.pdf"
    await render_pdf_async("".join(parts), out, style="clean", title="Project Portfolio")
    return out


async def _write_document(profile: Profile, label: str, kind: str, posting_text: str, out_dir: Path) -> Path | None:
    import html as _html
    from .render import render_pdf_async

    if kind == "portfolio":
        return await _write_portfolio(profile, out_dir)
    m = re.search(r"(\d{3,4})\s*words", label)
    words = 300 if kind == "cover letter" else max(400, min(800, int(m.group(1)) if m else 700))
    question = (f"Write a cover letter for this posting, about {words} words, addressed to the hiring team: why this "
                f"role and company, and what in the record bears on it." if kind == "cover letter" else label)
    text = await freetext.answer_question(question, posting_text, profile, words=words)
    if not text:
        return None
    pi = profile.career.get("personal_information", {}) if isinstance(profile.career, dict) else {}
    contact = " · ".join(str(pi.get(k)) for k in ("email", "phone") if pi.get(k))
    paras = "".join(f"<p>{_html.escape(par.strip())}</p>" for par in re.split(r"\n\s*\n", text) if par.strip())
    name = profile.full_name() if callable(profile.full_name) else str(profile.full_name)
    body = f"<h1>{_html.escape(name)}</h1><p>{_html.escape(contact)}</p>{paras}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{_safe(kind)}-{_safe(label[:30])}-{datetime.now():%Y%m%d%H%M%S}.pdf"
    await render_pdf_async(body, out, style="clean", title=f"{name} — {kind}")
    return out


def _autofill_boilerplate(label: str, field: dict, options: list[str] | None = None) -> str | None:
    """The narrow, explicitly-enumerated set of required fields safe to answer
    without a profile match, because the answer carries no factual claim about
    the candidate. Everything else — anything about the candidate's situation,
    qualifications, or legal status — is answered from the bank or not at all."""
    opts_now = list(options) if options is not None else list(field.get("options") or [])
    if re.fullmatch(r"\s*(today'?s\s+)?date(\s+signed)?\s*[*✱]?\s*", label, re.I) and not opts_now:
        # The date beside a signature line — the self-identification forms'
        # "Name / Date" — is today. The field's own format hint decides how
        # it is written (see apply._date_text).
        return datetime.now().strftime("%m/%d/%Y")
    m = re.fullmatch(r"\s*(today'?s\s+)?date(\s+signed)?\s*[—:-]\s*(month|day|year)\b.*", label, re.I)
    if m and not opts_now:
        # The same date split into Workday's three boxes.
        part = m.group(3).lower()
        return datetime.now().strftime("%m" if part == "month" else "%d" if part == "day" else "%Y")
    if (field.get("type") == "checkbox" and not opts_now and field.get("required") and "?" not in label and not _NO_BANK.search(label)
            and not _SELF_ID.search(" ".join((label, field.get("option_label") or "", field.get("section") or "")))):
        # A required lone checkbox under a statement — "Your application will
        # be reviewed for one position at a time" — is an acknowledgement box,
        # whatever words it uses. Nobody puts a real question behind one.
        # Never a box on a self-identification form: "Yes, I have a
        # disability" is a claim about the candidate, not an acknowledgement.
        return "yes"
    if _CONSENT_WORDS.search(label):
        opts = opts_now
        if field.get("type") in ("checkbox", "yesno") and not opts:
            return "yes"
        if opts:
            # "Terms & Conditions" as a Yes/No or "I agree" choice.
            for opt in opts:
                if re.match(r"^\s*(yes|i agree|agree|i accept|accept|i acknowledge|acknowledge)\b", opt, re.I):
                    return opt
    if opts_now and (_SELF_ID.search(label) or _SELF_ID.search(field.get("section") or "")
                     or any(_SELF_ID.search(o) for o in opts_now)):
        # Voluntary self-identification — known by its question, its heading,
        # or its own options ("Yes, I have a disability…" under "Please check
        # one of the boxes below"): the decline option, as the bank says for EEO.
        for opt in opts_now:
            if any(w in opt.lower() for w in _DECLINE_WORDS):
                return opt
    if _CONFERENCE.search(label):
        # "Indicate your planned attendance at the listed conferences": none.
        for opt in opts_now:
            if re.search(r"\b(none|not attending|will not|no plans?|n/?a|not planning|none of the above)\b", opt, re.I):
                return opt
        if not opts_now and field.get("type") in ("text", "textarea", ""):
            return "None"
    if opts_now and _SOCIO.search(label):
        # Socio-economic background (parents' education, household earner,
        # free school meals): voluntary, and the profile does not hold it.
        for opt in opts_now:
            if any(w in opt.lower() for w in _DECLINE_WORDS) or re.search(r"don'?t know|do not know|not sure|unknown", opt, re.I):
                return opt
    if opts_now and _LANG_SKILL.search(label) and not re.search(r"\benglish\b", label, re.I):
        # Reading/writing/speaking skill in a language the record does not list.
        for opt in opts_now:
            if re.search(r"^\s*(none|no (knowledge|proficiency|ability|skills?)|not (applicable|proficient)|n/?a|0)\b", opt, re.I):
                return opt
    if field.get("required") and opts_now and "?" not in label and all(_ACK_OPTION.match(o) for o in opts_now if o.strip()):
        # A statement to acknowledge ("Reminder: you may apply for one role
        # only") whose every option is a form of yes: the only answer there is.
        return next(o for o in opts_now if o.strip())
    if (field.get("required") and not opts_now and field.get("type") in ("text", "textarea", "")
            and re.search(r"who referred you|referred you\b.*\bname|referr(ed|al) (name|by)|name of (the )?(person|employee) who referred|referrer'?s? name", label, re.I)):
        # A required "who referred you" box with no referral to name: the
        # honest answer, rather than a stalled application or someone's name.
        return "N/A — no referral; found the posting on a job board"
    if (field.get("required") and not opts_now and field.get("type") in ("text", "textarea", "")
            and re.search(r"^\s*(what is |please (provide|enter|list) )?(their|his or her|his/her|the (employee|referrer|referring employee|contact)'?s?) "
                          r"(full |first |last )?(name|e-?mail( address)?|phone( number)?|title|department|relationship)\b", label, re.I)):
        # "What is their name?" — the detail of a referrer or contact the
        # form asks for after a No: there is nobody to name.
        return "N/A"
    if _SOURCE_FIELD.search(label) and field.get("combobox") and not opts_now:
        return None  # a searchable picker (Workday): its list is read live and the option chosen from it
    if _SOURCE_FIELD.search(label):
        opts = list(options) if options is not None else list(field.get("options") or [])
        if opts:
            # Whatever kind of control offers them — a select, a radio or
            # checkbox list, a picker whose list was read — the first generic
            # option present, in order of preference.
            for want in _GENERIC_SOURCE_OPTIONS:
                for opt in opts:
                    if want in opt.strip().lower():
                        return opt
            return None
        if field.get("type") in ("text", "textarea", "") and not field.get("combobox"):
            return _SOURCE_FREE_TEXT
    return None


def _is_essay(field: dict) -> bool:
    return field.get("tag") == "textarea" or (field.get("type") in ("text", "") and len(field.get("label", "")) > 60)


def _fits(answer: str | None, field: dict, options: list[str]) -> bool:
    """Whether an answer is one the field can take. An answer that is not one
    of the field's options is a miss, not an answer: the bank once matched
    "state" in "NYC tri-state area" to address.state and offered "NC" to a
    Yes/No question. A lone checkbox only takes yes or no — "Summer 2027"
    from target_term is not an answer to "I agree to the terms"."""
    if answer is None:
        return False
    if field.get("type") == "checkbox" and not options:
        return answer.strip().lower() in ("yes", "no", "true", "false", "on", "off", "1", "0", "checked")
    return not options or _pick_option(answer, [{"label": o, "value": o} for o in options]) is not None


def _bank(profile: Profile, question: str, field: dict | None = None, options: list[str] | None = None,
          strong: bool = False) -> tuple[str | None, str | None]:
    """The answer bank's word on a question, if it has one it can stand
    behind: a key whose words cover a fair share of the question (half of
    it when `strong`), never the candidate's own details for a question about
    someone else, and only an answer the field can take. The bank holds short
    facts, so an essay question takes one only on a full match."""
    # Under an Education heading only education keys apply — "Start date"
    # there is not the job's earliest start date — and under a work-history
    # heading the bank has nothing to say at all.
    section = (field or {}).get("section") or ""
    sections: tuple[str, ...] | None = None
    if re.search(r"education|school|academic|degree", section, re.I):
        sections = ("education.",)
    elif re.search(r"work history|employment history|experience|previous employ|past employ", section, re.I):
        return None, None
    answer, key = profile.lookup(question, min_score=1.0 if field and _is_essay(field) else 0.5,
                                 min_coverage=0.5 if strong else 1 / 3, sections=sections)
    if answer is None or key is None:
        return None, None
    if planner.THIRD_PARTY.search(question) and key.startswith(planner.SELF_KEYS):
        return None, None
    if key == "about.recent_reading" and not re.search(r"\b(read|reading|book|paper|article|podcast|blog)\b", question, re.I):
        # "What is the most impressive thing you've built with AI?" is not a
        # reading question, whatever words it shares with the key.
        return None, None
    if field is not None and not _fits(answer, field, options or []):
        return None, None
    return answer, key


async def _resolve(question: str, field: dict, options: list[str], profile: Profile, posting_text: str) -> str | None:
    """The answer to one question on its own, when the form-level plan has
    nothing for it: the bank, then the boilerplate rules, then — for required
    fields only — an essay written from the record, or the grounded
    question-answerer. None means nothing could decide it honestly."""
    answer, _ = _bank(profile, question, field, options)
    if answer is None:
        answer = _autofill_boilerplate(question, field, options)
        if not _fits(answer, field, options):
            answer = None
    if answer is None and field.get("required") and _is_essay(field) and not options:
        answer = await freetext.answer_question(question, posting_text, profile, max_chars=field.get("maxlength"))
    if answer is None and field.get("required"):
        answer = await qa.answer(question, options, profile, picker=bool(field.get("combobox")) and not options)
    return answer


_MISSING_FIELD = re.compile(
    r"(?:missing entry for required field|required field|is required|please (?:select|enter|choose|complete|fill in|answer))"
    r"[:\s]*(.+?)\s*$", re.I)


def _missing_labels(errors: list[str]) -> set[str]:
    """The fields a page's own validation names — "Missing entry for required
    field: Which office would you prefer?" — so they can be treated as
    required even where the markup never said so."""
    out: set[str] = set()
    for e in errors:
        for part in re.split(r"\s*\|\s*|\n", e):
            part = part.strip()
            # Workday: "Error-From The field From is required and must have a
            # value.", "Error: The field Date is required…", "Invalid LinkedIn URL".
            m3 = re.search(r"the field (.+?) is required", part, re.I)
            if m3:
                out.add(m3.group(1).strip().lower())
                continue
            m4 = re.match(r"^error\s*[-–:]\s*(.+?)(?:\s+(?:the field|invalid|is required|please)\b.*)?$", part, re.I)
            if m4 and len(m4.group(1)) <= 80:
                out.add(m4.group(1).strip().rstrip(".").lower())
            m5 = re.search(r"\binvalid\s+(.+?)\s*(?:url|format|value|entry|address)?\s*\.?$", part, re.I)
            if m5 and len(m5.group(1)) <= 40:
                out.add(m5.group(1).strip().lower())
            m = _MISSING_FIELD.search(part)
            if m:
                named = m.group(1).strip().rstrip(".").strip().lower()
                if named:
                    out.add(named)
            m2 = re.match(r"^(.+?)\s+is required\.?$", part, re.I)
            if m2:
                out.add(m2.group(1).strip().lower())
    return out


async def _fill_form(session: ApplySession, profile: Profile, pdf_path: Path, posting_text: str = "",
                     force_required: set[str] | None = None) -> tuple[list[str], list[dict]]:
    """Returns the labels of required fields nothing could answer, and the
    record of what the form holds. Non-empty labels mean this application
    does not get submitted. `force_required` names questions the page
    itself has demanded (from a rejected submit), required or not in the markup."""
    unresolved: list[str] = []
    fields = await session.describe_form()

    # Files first. Filling other fields can make a React form re-render its
    # upload widget, and the upload has fallbacks of its own. A failed upload
    # is a review item, not an error to retry blindly.
    file_fields = [f for f in fields if f.get("type") == "file"]
    resume_like = [f for f in file_fields if re.search(
        r"resume|résumé|\bcv\b", " ".join((f.get("label", ""), f.get("dom_id", ""), f.get("name", ""), f.get("section", ""))), re.I)]
    # Ashby puts an "autofill from your résumé" uploader above the real Resume
    # field. When a form has several file inputs, only the résumé-labelled ones
    # get the PDF; a lone unlabelled one gets it regardless.
    # Workday carries the résumé of the account's last application into a
    # new one: before this posting's first upload, whatever PDF the page
    # already holds is that carry-over and is taken off, so one résumé goes
    # — and a slot that took a file already in this session is not fed a
    # second copy by a repair round.
    if not session._uploaded:
        try:
            await session.remove_stale_uploads(Path(pdf_path))
        except Exception:
            pass
    for f in resume_like or file_fields:
        if f["selector"] in session._uploaded or (f.get("label") or "") in session._uploaded:
            continue
        try:
            await session.upload(f["selector"], pdf_path, label=f.get("label", ""))
        except Exception as e:
            unresolved.append(f"{f.get('label') or 'file upload'} ({str(e).splitlines()[0][:120]})")

    # Other documents a form may want — a transcript, a cover letter — come
    # from `answers.yaml → documents` (label word -> file path). A required
    # upload with no configured file is a review item.
    documents = {str(k): Path(str(v)).expanduser() for k, v in (profile.answers.get("documents") or {}).items() if v}
    for f in file_fields:
        if f in (resume_like or file_fields) or f.get("files"):
            continue
        label = f.get("label", "")
        kind = next((k for k in documents if re.search(k.replace("_", r"[ _-]?"), label, re.I)), None)
        if kind is None:
            continue
        try:
            await session.upload(f["selector"], documents[kind], label=label)
        except Exception as e:
            if f.get("required"):
                unresolved.append(f"{label or kind} ({str(e).splitlines()[0][:120]})")

    # A slot that drew after the first scan (Workday's My Experience renders
    # its Resume/CV section a beat after its heading) gets the résumé now.
    try:
        late = [f for f in await session.describe_form() if f.get("type") == "file"
                and f["selector"] not in {g["selector"] for g in file_fields}
                and re.search(r"resume|résumé|\bcv\b|upload a file", " ".join((f.get("label", ""), f.get("section", ""), f.get("dom_id", ""))), re.I)]
    except Exception:
        late = []
    for f in late:
        try:
            await session.upload(f["selector"], pdf_path, label=f.get("label", ""))
            file_fields.append(f)
        except Exception as e:
            unresolved.append(f"{f.get('label') or 'file upload'} ({str(e).splitlines()[0][:120]})")

    # A required upload with no configured file whose label is itself a
    # prompt — "In an essay of about 750 words…", "Cover letter" — is written
    # from the record and rendered to a PDF, like any other essay answer.
    for f in file_fields:
        if f in (resume_like or file_fields) or f.get("files") or not f.get("required"):
            continue
        label = f.get("label", "")
        if not label or any(u.startswith(label[:40]) for u in unresolved):
            continue
        if any(re.search(k.replace("_", r"[ _-]?"), label, re.I) for k in documents):
            continue  # a configured document covers it
        kind = _document_prompt(label)
        if not kind:
            continue
        try:
            written = await _write_document(profile, label, kind, posting_text, Path(pdf_path).parent / "documents")
            if written is None:
                unresolved.append(f"{label[:80]} — the essay writer could not produce it from the record")
            else:
                await session.upload(f["selector"], written, label=label)
        except Exception as e:
            unresolved.append(f"{label[:80]} ({str(e).splitlines()[0][:120]})")

    # The whole form goes to the planner once, every question in view, so the
    # answers are decided together (see planner.py). If that call fails, the
    # questions are answered one at a time instead.
    fields = await session.describe_form()
    if force_required:
        for f in fields:
            if _demanded(f.get("label", ""), force_required):
                f["required"] = True
    groups, _ = _group(fields)
    # A search-as-you-type picker (Greenhouse's react-select) lists nothing
    # in the markup; opened, it shows its fixed choices — "3.4 out of 4.0",
    # "Less than 3 months" — and the planner must see those to answer in
    # the form's own words rather than the profile's.
    for f in fields:
        if f.get("combobox") and not f.get("options") and f.get("type") != "file":
            if _is_search_picker(f):
                continue  # a search box: what it shows unopened is suggestions, not a list
            try:
                f["options"] = await session.combobox_options(f["selector"], f)
            except Exception:
                f["options"] = []
    _disambiguate(fields)
    questions = planner.describe_questions(fields, groups, hint_for=lambda label: _bank(profile, label)[0])
    plan: dict | None = None
    try:
        plan = await planner.plan(questions, profile, posting_text)
    except Exception as e:
        print(f"  planner unavailable ({_brief(e)}); answering field by field", file=sys.stderr, flush=True)

    # Two passes over the controls. A React form (Greenhouse) remounts parts
    # of itself as its pickers change, and a box checked before the remount
    # comes back unchecked; the second pass refills only what is still empty,
    # from answers already decided, so it costs no model calls.
    decided: dict[tuple, str | None] = {}
    sources: dict[tuple, str] = {}  # (section, question) -> where its answer came from
    for _ in range(2):
        fields = await session.describe_form()
        if force_required:
            for f in fields:
                if _demanded(f.get("label", ""), force_required):
                    f["required"] = True
        unresolved = await _fill_pass(session, profile, fields, posting_text, decided, sources, plan, force_required)
        if unresolved or not await session.unfilled_required(force_required):
            break

    # What the page still shows empty or refuses gets a second look, each
    # field with its live options and the page's own complaint.
    # Each round takes a handful of fields; a long form (SpaceX asks forty
    # questions) may need a few rounds, as long as each one makes progress.
    for _ in range(3):
        before = len(unresolved) + len(await session.unfilled_required(force_required))
        unresolved = await _repair(session, profile, posting_text, decided, sources, unresolved, force_required=force_required,
                                   docs_dir=Path(pdf_path).parent / "documents")
        after = len(unresolved) + len(await session.unfilled_required(force_required))
        if not unresolved or after >= before:
            break
    return unresolved, _snapshot(await session.describe_form(), unresolved, sources)


def _demanded(label: str, demanded: set[str] | None) -> bool:
    """Whether a field is one the page named in its rejection."""
    if not demanded or not label:
        return False
    low = " ".join(label.split()).lower().rstrip("*").strip()
    return any(d and (d in low or low in d) for d in demanded)


def _snapshot(fields: list[dict], unresolved: list[str], sources: dict[tuple, str]) -> list[dict]:
    """The form as it stands, question by question — the record of what was
    sent: each question, the value the page holds for it, where the answer
    came from, and, for a question left empty, why."""
    reasons: dict[str, str] = {}
    for u in unresolved:
        m = re.match(r"^(.*?)(?:\s+—\s+(.*)|\s+\((.*)\))?$", u, re.S)
        if m:
            reasons[m[1].strip()] = (m[2] or m[3] or "unanswered").strip()
    groups, grouped = _group(fields)
    out: list[dict] = []
    for f in fields:
        if f["id"] in grouped:
            continue
        label = f.get("label") or f.get("name") or ""
        if f.get("type") == "file":
            value = ", ".join(f.get("files") or [])
        elif f.get("type") == "checkbox":
            value = "Yes" if f.get("checked") else ""
        else:
            value = f.get("value") or ""
        if not label and not value:
            continue
        section = f.get("section") or ""
        out.append({"question": label, "section": section, "widget": planner.widget_of(f),
                    "required": bool(f.get("required")), "answer": value,
                    "source": sources.get((section, label)), "reason": reasons.get(label)})
    for key, members in groups.items():
        label = next((m["label"] for m in members if m.get("label")), key)
        section = members[0].get("section") or ""
        out.append({"question": label, "section": section,
                    "widget": planner.widget_of(members[0], members[0].get("type")),
                    "required": any(m.get("required") for m in members),
                    "answer": " | ".join(m.get("option_label") or "" for m in members if m.get("checked")),
                    "options": [m.get("option_label") or "" for m in members],
                    "source": sources.get((section, label)), "reason": reasons.get(label)})
    return out


_CURRENT_EMPLOYER = re.compile(r"\b(current|present)( or most recent)? (company|employer|organi[sz]ation)\b|\bmost recent employer\b", re.I)


_GRAD_YEAR_Q = re.compile(r"graduat\w* (year|date)|year of (expected )?(graduation|completion)|expected (to )?graduat|"
                          r"(expected|anticipated|actual)( or actual)? graduation|graduation year", re.I)


def _graduation_year(profile: Profile) -> str | None:
    """The four-digit year the record's first education entry completes."""
    for ed in profile.career.get("education_details", []) or []:
        m = re.search(r"20\d\d", str(ed.get("year_of_completion") or ed.get("graduation_date") or ""))
        if m:
            return m.group(0)
    return None


def _current_employer(profile: Profile) -> str | None:
    """The employer of the record's ongoing role (a period ending in
    "Present"), organisation only: Lever's résumé parser filled "Current
    company" with "CS 210 Computer Systems" on nine applications, reading a
    course name off the Education block."""
    for exp in profile.career.get("experience_details", []) or []:
        if re.search(r"present|current|ongoing|now", str(exp.get("employment_period") or ""), re.I):
            company = str(exp.get("company") or "").strip()
            if company:
                return company.split(",")[0].strip() or company
    return None


async def _decide(question: str, field: dict, options: list[str], profile: Profile, posting_text: str,
                  decided: dict[tuple, str | None], sources: dict[tuple, str], plan: dict | None,
                  widget: str) -> str | None:
    """The answer to one question, in order of trust: a boilerplate rule (a
    consent box, "how did you hear"), an exact answer-bank hit for a plain
    field (Email, Phone, First Name), then the form-level plan — whose skip
    is final — and, when there is no plan, the field-by-field chain."""
    section = field.get("section") or ""
    key = (section, question, tuple(options))
    if key in decided:
        return decided[key]
    if not field.get("required") and _LEAVE_BLANK.search(question):
        # A middle name, a phone extension, a second address line: the
        # profile has none, and a fuzzy bank hit ("name", "phone") would put
        # the wrong thing there.
        decided[key] = None
        return None
    picked = sponsorship_answer(profile, question, options)
    if picked is None and not _SELF_ID.search(question):
        g = gpa_answer(profile, question, options)
        if g is not None:
            # The GPA is the bank's number, exactly, or the band it falls in:
            # "3.5" on a text field for a 3.42 is a misstatement a transcript
            # check exposes.
            sources[(section, question)] = "answer bank: education.gpa"
            decided[key] = g
            return g
    if picked is not None:
        # Sponsorship is the bank's to answer, in the option's own words:
        # the model once read "legally allowed to work: Yes" as "Yes, will
        # require firm sponsorship".
        sources[(section, question)] = "answer bank: work_authorization.requires_us_sponsorship"
        decided[key] = picked
        return picked
    if _CURRENT_EMPLOYER.search(question) and not options:
        # A text box or an employer search picker: "Duke University" is
        # searched and chosen like any other picker value.
        emp = _current_employer(profile)
        if emp:
            sources[(section, question)] = "record: the ongoing role's employer"
            decided[key] = emp
            return emp
    if _GRAD_YEAR_Q.search(question) and not options and not field.get("combobox") and field.get("type") in ("text", "number", ""):
        year = _graduation_year(profile)
        if year:
            sources[(section, question)] = "record: education year of completion"
            decided[key] = year
            return year
    if _NO_BANK.search(question):
        # "I currently work here" under a work-experience entry is a fact
        # about that role, decided with the entry by the plan — never the
        # bank's "currently enrolled: Yes", never an acknowledgement box.
        d = plan.get(planner.question_key(question, widget, section)) if plan is not None else None
        answer = d.answer if d is not None and not d.essay else None
        if answer is not None:
            sources[(section, question)] = "form plan" + (f": {d.reason}" if d.reason else "")
        decided[key] = answer
        return answer
    if field.get("combobox") and re.search(r"\b(city|location)\b", question, re.I) and not re.search(r"work|prefer|office|relocat", question, re.I):
        # A place picker lists "Durham, NC, US" beside five other Durhams:
        # the whole place, not the bare city, is what tells them apart.
        flat = profile.flat_answers()
        place = ", ".join(str(flat.get(k)) for k in ("address.city", "address.state", "address.country") if flat.get(k))
        if place:
            decided[key] = place
            sources[(section, question)] = "answer bank: address (city, state, country)"
            return place
    answer = _autofill_boilerplate(question, field, options)
    if not _fits(answer, field, options):
        answer = None
    else:
        sources[(section, question)] = "boilerplate rule"
    if answer is None:
        answer, bank_key = _bank(profile, question, field, options, strong=True)
        if answer is not None:
            sources[(section, question)] = f"answer bank: {bank_key}"
    if answer is None and plan is not None:
        d = plan.get(planner.question_key(question, widget, section))
        if d is not None:
            if d.essay:
                if field.get("required") and not options:
                    answer = await freetext.answer_question(question, posting_text, profile,
                                                            max_chars=field.get("maxlength"))
                    sources[(section, question)] = "essay writer (from the record)"
            else:
                answer = d.answer
                sources[(section, question)] = "form plan" + (f": {d.reason}" if d.reason else "")
            decided[key] = answer
            return answer
    if answer is None:
        answer = await _resolve(question, field, options, profile, posting_text)
        if answer is not None:
            sources[(section, question)] = "field by field"
    decided[key] = answer
    return answer


def _group(fields: list[dict]) -> tuple[dict[str, list[dict]], set[str]]:
    """Radio buttons, and checkbox lists that share a name or a question, are
    one question with several options — not several fields. Ashby gives each
    box of a "select all that apply" list its own name; the question text
    above them is what they share."""
    groups: dict[str, list[dict]] = {}
    boxes = [f for f in fields if f.get("type") == "checkbox"]
    by_name: dict[str, int] = {}
    by_question: dict[tuple, int] = {}
    name_labels: dict[str, set] = {}
    for f in boxes:
        by_name[f.get("group") or ""] = by_name.get(f.get("group") or "", 0) + 1
        q = (f.get("section") or "", f.get("label") or "")
        by_question[q] = by_question.get(q, 0) + 1
        name_labels.setdefault(f.get("group") or "", set()).add(f.get("label") or "")
    for f in fields:
        if f.get("type") == "radio":
            groups.setdefault(f.get("group") or f.get("label", ""), []).append(f)
        elif f.get("type") == "checkbox":
            name = f.get("group") or ""
            q = (f.get("section") or "", f.get("label") or "")
            # A shared name makes a list only when the boxes also share a
            # question. Ashby names every lone consent box "Yes": two of them
            # under two statements are two questions, and ticking one of
            # them is not an answer to the other.
            if name and by_name[name] > 1 and len(name_labels.get(name) or set()) <= 1:
                groups.setdefault(name, []).append(f)
            elif f.get("label") and by_question[q] > 1 and f.get("option_label") and f["option_label"] != f["label"]:
                groups.setdefault("q:" + "|".join(q), []).append(f)
    return groups, {m["id"] for ms in groups.values() for m in ms}


async def _fill_group(session: ApplySession, members: list[dict], options: list[str], answer: str) -> None:
    """Choose the option(s) an answer names: one for a radio group, several
    (separated by ' | ') for a select-all-that-apply list."""
    wanted = [p.strip() for p in answer.split("|")] if members[0].get("type") == "checkbox" else [answer]
    hit = False
    for w in wanted:
        idx = _pick_option(w, [{"label": o, "value": o} for o in options])
        if idx is None:
            continue
        if members[idx].get("checked") and members[idx].get("type") == "radio" and len(members) > 1:
            # Already checked in the DOM yet the site says it is missing (Ashby
            # after a re-render): toggle through another option so a real
            # change event carries the choice into the form's state.
            other = next((m for m in members if m is not members[idx]), None)
            if other is not None:
                try:
                    await session.fill(other["selector"], "yes", other)
                except Exception:
                    pass
        await session.fill(members[idx]["selector"], "yes", members[idx])
        hit = True
    if not hit:
        raise ValueError(f"no option matches {answer!r}")


# A page's way of saying a field it shows filled did not take: the value
# never reached the site's own state, or it wants another one.
_REJECTED_ERR = re.compile(r"is required|must have a value|required field|cannot be (blank|empty)|invalid", re.I)
_NUMERIC_ERR = re.compile(r"valid (currency )?amount|numeric|numbers? only|must be a (valid )?number|digits only|whole number", re.I)
_VALUE_ERR = re.compile(r"\b(valid|invalid|format|numeric|number|amount|digits|too long|too short|at least|at most)\b", re.I)


_MONEY_Q = re.compile(r"\b(compensation|salary|pay|wage|rate|earnings|remuneration)\b", re.I)


def _money_answer(profile: Profile, label: str, field: dict, answer: str | None) -> str | None:
    """A money question answered as one number in the unit the question
    names. "$30-40 per hour" typed into Workday's "Desired Total Annual
    Compensation" box once became 3040: the annual figure the bank holds
    goes to an annual box, the hourly one to an hourly box, and a range
    becomes its midpoint."""
    if not answer or not label or not _MONEY_Q.search(label) or field.get("options") or field.get("combobox"):
        return answer
    if field.get("type") not in ("text", "number", "", "textarea"):
        return answer
    if re.fullmatch(r"\s*\$?\s*[\d,]+(\.\d+)?\s*", answer):
        return re.sub(r"[^\d.]", "", answer)
    flat = profile.flat_answers()
    hourly = _as_amount(str(flat.get("salary_expectations.hourly_rate_usd") or "")) or _as_amount(answer)
    annual = _as_amount(str(flat.get("salary_expectations.annual_salary_expectation") or ""))
    if re.search(r"\b(hour|hourly|hr)\b", label, re.I):
        return hourly or answer
    if re.search(r"annual|year|total|salary|compensation", label, re.I):
        if annual:
            return annual
        if hourly and re.search(r"\bhour", answer, re.I):
            return str(int(float(hourly) * 2080))
    return _as_amount(answer) or answer


def _as_amount(text: str) -> str | None:
    """The one number a form that wants "a valid currency amount" can take,
    out of an answer written for a person: "$30-40 per hour" -> "35",
    "$85,000" -> "85000", "40k" -> "40000". None when there is no number."""
    nums = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", text or ""):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if m.group(2):
            n *= 1000
        nums.append(n)
    if not nums:
        return None
    n = (nums[0] + nums[1]) / 2 if len(nums) >= 2 else nums[0]
    return str(int(n)) if float(n).is_integer() else f"{n:.2f}"


async def _repair(session: ApplySession, profile: Profile, posting_text: str,
                  decided: dict[tuple, str | None], sources: dict[tuple, str], unresolved: list[str],
                  limit: int = 24, force_required: set[str] | None = None, docs_dir: Path | None = None) -> list[str]:
    """Required questions the form still shows empty after filling — an
    answer the widget would not take, a question the plan skipped, a field
    that appeared late — go back to the model one at a time with what was
    tried, what the page says, and the options as they stand now. Returns
    the questions that remain unanswered, each with the reason."""
    still = await session.unfilled_required(force_required)
    errors = await session.errors()
    fields = await session.describe_form()
    # A field the page rejected while it holds a value ("Salary Range Must be
    # a valid currency amount") is not empty, so it is not in `still`; it
    # still needs a different answer.
    rejected = [e for e in errors if _VALUE_ERR.search(e) or _REJECTED_ERR.search(e)]
    named = _missing_labels(errors)
    if rejected or named:
        seen = {f["id"] for f in still}
        for f in fields:
            label = (f.get("label") or "").strip()
            if f["id"] in seen or not f.get("value") or not label or f.get("type") in ("file", "checkbox", "radio"):
                continue
            if any(label[:30].lower() in e.lower() for e in rejected) or _demanded(label, named):
                still.append(f)
                seen.add(f["id"])
    if not still:
        return []
    groups, grouped = _group(fields)
    remaining: list[str] = []
    for f in still[:limit]:
        searched = []
        members = next((ms for ms in groups.values() if any(m["id"] == f["id"] for m in ms)), None) if f["id"] in grouped else None
        seed = ""
        if members:
            label = next((m["label"] for m in members if m.get("label")), "")
            options = [m.get("option_label") or "" for m in members]
            widget = planner.widget_of(members[0], members[0].get("type"))
        else:
            label = f.get("label", "")
            options = list(f.get("options") or [])
            search_picker = _is_search_picker(f)
            if f.get("combobox") and not options and not search_picker:
                try:
                    options = await session.combobox_options(f["selector"], f)
                except Exception:
                    options = []
            widget = planner.widget_of(f)
            searched: list[str] = []
            if f.get("combobox") and not options:
                # A search picker lists nothing until something is typed — and
                # what it shows unopened is the top of an index, never the
                # choices (a Field of Study picker opened on "Accounting", and
                # the model, handed that as the list, chose it). Type what was
                # tried (or the bank's answer), then each part of it ("Computer
                # Science" out of "Computer Science and Mathematics"), and read
                # what the page offers, so the choice is made from real entries.
                seed = str(decided.get((f.get("section") or "", label, ())) or _bank(profile, label, {**f, "required": True}, [], strong=True)[0] or "")
                if re.search(r"\b(city|location)\b", label, re.I) and not re.search(r"work|prefer|office|relocat", label, re.I):
                    # A place picker searched with the bare city ("Durham") lists
                    # six Durhams; the whole place tells them apart.
                    flat = profile.flat_answers()
                    place = ", ".join(str(flat.get(k)) for k in ("address.city", "address.state", "address.country") if flat.get(k))
                    seed = place or seed
                for attempt in ([seed] + _value_parts(seed)) if seed else []:
                    try:
                        searched = await session.combobox_search(f["selector"], f, attempt)
                    except Exception:
                        searched = []
                    if searched:
                        break
                if searched:
                    options = searched
        if f.get("type") == "file":
            # An upload that was tried and failed keeps its own error; one
            # never tried is a file the profile does not have — unless its
            # label is itself a prompt (a cover letter the page turned out to
            # require at submit), which is written from the record and attached.
            earlier = next((u for u in unresolved if label and u.startswith(label[:40])), None)
            kind = _document_prompt(label) if label and not earlier else None
            if kind:
                try:
                    written = await _write_document(profile, label, kind, posting_text, docs_dir or Path("output") / "documents")
                    if written is not None:
                        await session.upload(f["selector"], written, label=label)
                        sources[(f.get("section") or "", label)] = f"{kind} written from the record"
                        continue
                except Exception as e:
                    remaining.append(f"{label} ({_brief(e)})")
                    continue
            remaining.append(earlier or f"{label} — a file the profile does not have")
            continue
        section = f.get("section") or ""
        question = {"qid": "q1", "key": planner.question_key(label, widget, section), "label": label,
                    "section": section, "widget": widget, "options": options, "required": True,
                    "maxlength": f.get("maxlength"), "hint": f.get("hint") or f.get("placeholder") or "", "bank": ""}
        previous = decided.get((section, label, tuple(options))) or f.get("value") or None
        error = next((e for e in errors if f["id"] in e or (label and label[:30].lower() in e.lower())), "")
        if not error:
            # What the widget itself refused — "no option matches '1560';
            # it offered ['1560 out of 1600', …]" — is the useful complaint.
            tried = next((u for u in unresolved if label and u.startswith(label[:40])), "")
            error = tried[len(label):].strip(" (—-)") if tried else ""
        # A field that appeared late — after a choice revealed it — never met
        # the rules or the bank; they come first here too, before the model.
        quick = _autofill_boilerplate(label, {**f, "required": True}, options)
        if not _fits(quick, f, options):
            quick, _ = _bank(profile, label, {**f, "required": True}, options, strong=True)
        if quick is not None and previous and quick.strip().lower() == str(previous).strip().lower():
            quick = None  # the site just refused exactly this; the model sees the error and the live options instead
        if quick is None and previous and not options and _NUMERIC_ERR.search(error or ""):
            quick = _as_amount(str(previous))  # the page wants the number, not the sentence around it
        if quick is None and searched:
            # The entry the page itself offers for what was tried — "Durham,
            # NC, US" for "Durham, NC, United States", "Computer Science" for
            # "Computer Science and Mathematics" — when one clearly fits.
            for cand in [c for c in (previous, seed) if c] + _value_parts(str(previous or seed)):
                j = closest_option(str(cand), searched)
                if j is not None:
                    quick = searched[j]
                    break
        try:
            if quick is not None:
                d = Decision(quick, reason="rule or answer bank")
            else:
                d = await planner.repair(question, previous, error, profile, posting_text)
            if d.essay and not options:
                d = Decision(await freetext.answer_question(label, posting_text, profile, max_chars=f.get("maxlength")))
        except Exception as e:
            remaining.append(f"{label} ({_brief(e)})")
            continue
        if d.answer is None:
            remaining.append(f"{label} — {d.reason}" if d.reason else label)
            continue
        decided[(section, label, tuple(options))] = d.answer
        sources[(section, label)] = "repair pass" + (f": {d.reason}" if d.reason else "")
        try:
            if members:
                await _fill_group(session, members, options, d.answer)
            else:
                await session.fill(f["selector"], _money_answer(profile, label, f, d.answer), f)
        except Exception as e:
            remaining.append(f"{label} ({_brief(e)})")
    for f in still[limit:]:
        remaining.append(f.get("label") or f.get("name") or "?")
    return remaining


# Questions whose answer is a hard fact the bank holds, where a value a page
# already shows may be an earlier attempt's mistake rather than the truth.
_HARD_FACT_Q = re.compile(r"sponsor|visa|authori[sz]|citizen|clearance|relocat|18 years|drug|background check|eligib", re.I)


def _same_answer(answer: str, held: str, options: list[str]) -> bool:
    """Whether what the page holds already means the bank's answer: the same
    option, the same words, or both a yes (or both a no)."""
    a, h = " ".join(answer.split()).lower(), " ".join(held.split()).lower()
    if not h:
        return False
    if a == h or a in h or h in a:
        return True
    if options:
        ia = _pick_option(answer, [{"label": o, "value": o} for o in options])
        ih = _pick_option(held, [{"label": o, "value": o} for o in options])
        if ia is not None and ih is not None:
            return ia == ih
    yes = re.compile(r"^(yes|y|true)\b", re.I)
    no = re.compile(r"^(no|n|false)\b", re.I)
    return bool((yes.match(a) and yes.match(h)) or (no.match(a) and no.match(h)))


_NEXT_WORDS = re.compile(
    r"^\s*(next|continue|proceed|verify|review(( my| your)? application)?|save (and|&) continue)\s*(step)?\s*[›→>]?\s*$", re.I)


def _pick_next_button(buttons: list[dict]) -> dict | None:
    """The one control that moves a multi-step form to its next page."""
    candidates = [b for b in buttons if not b.get("disabled") and _NEXT_WORDS.match(b.get("text", ""))]
    return candidates[0] if len(candidates) == 1 else None


async def _fill_pass(session: ApplySession, profile: Profile, fields: list[dict], posting_text: str,
                     decided: dict[tuple, str | None], sources: dict[tuple, str], plan: dict | None = None,
                     force_required: set[str] | None = None) -> list[str]:
    """One pass over every control that is not yet filled. Returns the
    questions that could not be answered or would not take the answer."""
    unresolved: list[str] = []
    groups, grouped = _group(fields)

    # "I currently work here" before anything else in its entry: ticking it
    # takes the entry's End date boxes off the page (Greenhouse, Workday),
    # which would otherwise be filled with a date the site then refuses as
    # "must be in the past" for a role that has not ended.
    gone: set[str] = set()
    for f in fields:
        if (f.get("type") != "checkbox" or f["id"] in grouped or f.get("checked")
                or not _CURRENT_ROLE.search(f.get("label") or "")):
            continue
        answer = await _decide(f["label"], f, [], profile, posting_text, decided, sources, plan, planner.widget_of(f))
        if answer is None or str(answer).strip().lower() not in _YES_WORDS:
            continue
        try:
            await session.fill(f["selector"], answer, f)
        except Exception as e:
            unresolved.append(f"{f['label']} ({_brief(e)})")
            continue
        gone.add(f["id"])
        m = re.search(r"\d+", f.get("dom_id") or f.get("name") or "")
        if m:
            n = m.group(0)
            for g in fields:
                did = g.get("dom_id") or ""
                if re.search(r"end[-_]?date", did, re.I) and re.search(rf"(?<!\d){n}(?!\d)", did):
                    gone.add(g["id"])
                    # Greenhouse keeps the boxes in the DOM, hidden: the
                    # required-empty check and the repair pass must not
                    # try to fill what the page no longer asks for.
                    session._declined.add(g["selector"])
    fields = [f for f in fields if f["id"] not in gone]

    # Single controls first — text, pickers, yes/no toggles — and the option
    # groups after them, so a picker's remount does not undo a box checked
    # before it.
    for f in fields:
        if f["id"] in grouped or f.get("type") == "file":
            continue
        # Already filled — a retry pass, the site's own prefill, or an earlier
        # attempt's saved draft (Workday keeps one per account). A checkbox is
        # judged by its checked state: its value reads "on" either way. On a
        # hard fact the bank holds — sponsorship, authorization, clearance —
        # a draft that contradicts the bank is corrected, never kept.
        if (f.get("checked") if f.get("type") in ("checkbox", "radio") else f.get("value")):
            label_now = f.get("label") or ""
            if f.get("type") in ("checkbox", "radio") or not (_HARD_FACT_Q.search(label_now) or _CURRENT_EMPLOYER.search(label_now)):
                continue
            held = str(f.get("value") or "")
            opts_now = f.get("options") or []
            if f.get("combobox") and not opts_now:
                try:
                    opts_now = await session.combobox_options(f["selector"], f)
                except Exception:
                    opts_now = []
            answer, why = None, ""
            if _CURRENT_EMPLOYER.search(label_now) and not opts_now:
                # The site's résumé parser pre-fills this from the PDF and
                # has read a course name as the employer.
                answer = _current_employer(profile)
                why = "record: the ongoing role's employer" if answer is not None else ""
            if answer is None:
                answer = sponsorship_answer(profile, f["label"], opts_now)
                why = "answer bank: work_authorization.requires_us_sponsorship" if answer is not None else ""
            if answer is None:
                answer, why = _bank(profile, f["label"], f, opts_now, strong=True)
                why = f"answer bank: {why}" if answer is not None else ""
            if answer is None and plan is not None:
                # The bank cannot always match a long question ("Will you now
                # or in the future require visa sponsorship for employment?");
                # the plan, which saw the profile's own sponsorship answer, can.
                d = plan.get(planner.question_key(f["label"], planner.widget_of(f), f.get("section") or ""))
                if d is not None and d.answer and not d.essay:
                    answer, why = d.answer, "form plan" + (f": {d.reason}" if d.reason else "")
            if answer is None or _same_answer(answer, held, opts_now):
                continue
            sources[(f.get("section") or "", f["label"])] = f"{why} (over a saved draft of {held!r})"
            try:
                await session.fill(f["selector"], answer, f)
            except Exception as e:
                if f.get("required"):
                    unresolved.append(f"{f['label']} ({_brief(e)})")
            continue
        options = f.get("options") or []
        if f.get("combobox") and not options:
            # A picker's fixed list, when it has one, so the answer is held to
            # its options like a select's — and a "how did you hear" picker
            # can be answered from what it offers.
            try:
                options = await session.combobox_options(f["selector"], f)
            except Exception:
                options = []
        answer = await _decide(f["label"], f, options, profile, posting_text, decided, sources, plan,
                               planner.widget_of(f))
        if answer is None:
            if f.get("required"):
                unresolved.append(f["label"])
            continue
        answer = _money_answer(profile, f["label"], f, answer)
        try:
            await session.fill(f["selector"], answer, f)
        except Exception as e:
            # An answer we have but the field will not take — usually a dropdown
            # with no matching option. Treated as unanswered so the posting goes
            # to review with the reason, not failed as an error to be retried.
            if f.get("required"):
                unresolved.append(f"{f['label']} ({_brief(e)})")
        if not session.fast:
            await asyncio.sleep(random.uniform(0.3, 1.0))  # a person pauses between fields

    for key, members in groups.items():
        question = next((m["label"] for m in members if m.get("label")), key)
        if any(m.get("checked") for m in members) and not _demanded(question, force_required):
            continue  # answered — unless the page just said it is missing (a choice the DOM shows but the site never registered)
        options = [m.get("option_label") or "" for m in members]
        required = any(m.get("required") for m in members)
        answer = await _decide(question, {**members[0], "required": required}, options, profile, posting_text,
                               decided, sources, plan, planner.widget_of(members[0], members[0].get("type")))
        if answer is None:
            if required:
                unresolved.append(question)
            continue
        try:
            await _fill_group(session, members, options, answer)
        except Exception as e:
            if required:
                unresolved.append(f"{question} ({_brief(e)})")
        if not session.fast:
            await asyncio.sleep(random.uniform(0.3, 1.0))
    return unresolved


def _brief(e: BaseException) -> str:
    """The first line of an error — Playwright appends a call log that is
    noise in a report."""
    return str(e).splitlines()[0][:160] if str(e) else type(e).__name__


async def run_batch(
    profile: Profile,
    queue_path: str | Path | None = None,
    out_dir: str | Path = "output",
    state_path: str | Path | None = None,
    dry_run: bool = False,
    headless: bool = False,
    pause_seconds: float = 15.0,
    retry_all: bool = False,
    on_progress=None,
    entries: list[QueueEntry] | None = None,
    profile_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run every not-yet-done entry in the queue. Safe to kill and re-invoke —
    it resumes from `state_path` rather than starting over.

    `dry_run=True` runs the whole pipeline — tailor, open the form, fill it,
    screenshot it — and stops one step short of the submit click. Worth doing
    once on a new queue before trusting it to submit unattended.
    """
    out_dir = Path(out_dir)
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    state = RunState.load(state_path or out_dir / "batch-state.json")
    if entries is None:
        if queue_path is None:
            raise ValueError("run_batch needs a queue_path or entries")
        entries = load_queue(queue_path)
    entries = list(entries)
    from .profile import apply_once_at_company
    apply_once = apply_once_at_company(profile.answers)
    retry_statuses = RETRYABLE if not retry_all else set(s for s in
        ("applied", "tailored_only", "ready_not_submitted", "skipped", "needs_review", *RETRYABLE))

    session = ApplySession(headless=headless, **({"profile_dir": Path(profile_dir)} if profile_dir else {}))
    report_path = out_dir / "batch-report.csv"
    counts: dict[str, int] = {}

    from .queue import company_key as _ckey
    sent_this_pass: set[str] = set()
    try:
        for entry in entries:
            if state.already_attempted(entry.id, retry_statuses):
                continue
            if _ckey(entry.company_hint) in sent_this_pass:
                # The pass was dealt three Lyft postings at once and the
                # company cooldown is read at selection time: one application
                # per company per pass, the rest wait for a later pass.
                print(f"  [{entry.company_hint}] skipped this pass: already applied there minutes ago", file=sys.stderr, flush=True)
                continue
            _now("starting", entry)
            try:
                await session.reset()
            except Exception:
                pass
            try:
                outcome = await _process_one(session, profile, entry, out_dir, shots_dir, apply_once, state, dry_run)
            except Exception as e:
                # One posting's page must never end the pass: whatever the
                # browser threw is that posting's record, and the next one runs.
                outcome = Outcome(entry_id=entry.id, status="error", company=entry.company_hint, role=entry.title,
                                  detail=f"the attempt broke off: {_brief(e)}")
            if outcome.status == "error" and _NETWORK_ERROR.search(outcome.detail or ""):
                # The connection dropped, not the posting. Wait for it to come
                # back rather than racing through the rest of the list offline,
                # then try this one again.
                _now("waiting for the internet connection", entry)
                if await _wait_for_network():
                    try:
                        outcome = await _process_one(session, profile, entry, out_dir, shots_dir, apply_once, state, dry_run)
                    except Exception as e:
                        outcome = Outcome(entry_id=entry.id, status="error", company=entry.company_hint, role=entry.title,
                                          detail=f"the attempt broke off: {_brief(e)}")
            _now("idle")
            if outcome.status in ("applied", AWAITING):
                for name in (outcome.company, entry.company_hint):
                    if _ckey(name or ""):
                        sent_this_pass.add(_ckey(name or ""))
            outcome.when = datetime.now().astimezone().isoformat(timespec="seconds")
            outcome.worker = _worker()
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            state.record(entry.id, asdict(outcome))
            _append_report_row(report_path, outcome)
            if on_progress:
                on_progress(outcome)
            await asyncio.sleep(pause_seconds)
    finally:
        await session.stop()

    return {"counts": counts, "total": len(entries), "report": str(report_path), "state": str(state.path)}


def _inbox_wall(state: RunState, entry: QueueEntry, prev_rec: dict | None = None) -> str | None:
    """Why this posting waits for the applicant's inbox, or None: it met an
    e-mail-code wall itself, or another posting at its company did. Every
    attempt at such a wall mails the applicant one more code, so a company's
    other postings wait with the first rather than mailing a code apiece —
    until RESUME_TAILOR_IMAP_PASSWORD is set, when every one is worth a try."""
    if mailbox.configured():
        return None
    prev_rec = prev_rec if prev_rec is not None else (state.done.get(entry.id) or {})
    if (prev_rec.get("detail") or "").startswith("re-queued"):
        return None  # the user put it back in the queue by hand: try it, code or no code
    if mailbox.waiting_for_inbox(prev_rec):
        return prev_rec.get("detail") or "waiting for the inbox to be configured"
    key = company_key(entry.company_hint)
    if not key:
        return None
    for rec_id, rec in state.done.items():
        if rec_id != entry.id and mailbox.waiting_for_inbox(rec) and company_key(rec.get("company") or "") == key:
            return (f"{entry.company_hint} e-mails a verification code before its form (another posting there hit that wall) — "
                    f"not attempted, so as not to mail you one more code; set {mailbox.ENV_HINT} (a Google app password) "
                    "in ~/.resume-tailor/env and the tool will read it")
    return None


def _needs_approval(profile: Profile, *names: str) -> bool:
    """Whether one of the company's names matches `search.approve_before_submit`
    (case-insensitive substrings) — a posting the user wants to see before it goes."""
    pats = [str(p).strip() for p in (profile.answers.get("search", {}).get("approve_before_submit") or []) if str(p).strip()]
    # Whole words, so "Meta" holds Meta and not "Exa (formerly Metaphor)".
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", n, re.I) for p in pats for n in names if n)


async def _process_one(session: ApplySession, profile: Profile, entry: QueueEntry, out_dir: Path,
                        shots_dir: Path, apply_once: bool, state: RunState, dry_run: bool,
                        judge_gate: bool = True, approved: bool = False) -> Outcome:
    o = Outcome(entry_id=entry.id, status="error", company=entry.company_hint, role=entry.title)

    if not entry.has_source:
        o.status, o.detail = "skipped", "queue entry has neither url nor text"
        return o
    if profile.blacklisted(entry.company_hint):
        # Discovery filters the blacklist too; this catches a hand-queued entry.
        o.status, o.detail = "skipped", f"{entry.company_hint} is on your company blacklist"
        return o
    prev_rec = state.done.get(entry.id) or {}
    if (prev_rec.get("detail") or "").startswith("re-queued"):
        judge_gate = False  # the user put it back in the queue by hand: apply, whatever the judges say
    wall = _inbox_wall(state, entry, prev_rec)
    if wall:
        # Every attempt at an e-mail-code wall mails the applicant a fresh
        # code. Until the inbox is configured there is nothing new to try —
        # at this posting or at any other of the company's.
        o.status, o.detail = "needs_login", wall
        o.pdf, o.fit, o.answers, o.screenshot = prev_rec.get("pdf", ""), prev_rec.get("fit", ""), prev_rec.get("answers") or [], prev_rec.get("screenshot", "")
        return o
    # A company goes by two names here: the listing's ("National Information
    # Solutions Cooperative") and the posting's own ("NISC"); both count.
    names = [entry.company_hint, (state.done.get(entry.id) or {}).get("company") or ""]
    if apply_once and any(state.has_applied(c) for c in names if c):
        # Known before anything is spent: no page load, no tailoring.
        o.status, o.detail = "skipped", f"already applied at {entry.company_hint} (apply_once_at_company)"
        return o
    if apply_once and any(state.held_at(c, except_id=entry.id) for c in names if c):
        o.status, o.detail = "skipped", f"another posting at {entry.company_hint} is awaiting your approval (apply_once_at_company)"
        return o

    jd_text = entry.text
    if not jd_text:
        try:
            await session.goto(entry.url)
            jd_text = await session.read_posting_text()
            if len(jd_text) < 1500:
                # The posting container guess came up short (a careers page
                # listing many roles): the whole page beats a fragment.
                full = await session.read_text()
                if len(full) > 2 * len(jd_text):
                    jd_text = full[:15000]
        except Exception as e:
            o.status, o.detail = "error", f"could not load posting: {e}"
            return o
    if len(jd_text.strip()) < 120:
        from .ats import NEEDS_ACCOUNT, host_kind
        if host_kind(entry.url) in NEEDS_ACCOUNT:
            # The board shows nothing to a visitor without an account (Work at
            # a Startup): a login to do by hand, not a page that failed.
            o.status, o.detail = "needs_login", ("this site shows the posting only to an account holder — log in once "
                                                 f"in the tool's browser and rerun: {entry.url}")
            return o
        o.status, o.detail = "skipped", "posting text too short — page likely did not load the description"
        return o
    if entry.company_hint and entry.company_hint.lower() not in jd_text.lower():
        # The essay writer may name only what the posting names; a page that
        # never spells out its own company would leave "Zipline" unsayable.
        jd_text = " — ".join(x for x in (entry.company_hint, entry.title) if x) + "\n\n" + jd_text

    # The address that applies to this posting's location — the default one, or
    # an alternate for postings in the home state — follows the profile into
    # both the form answers and the fit judges.
    profile = profile.for_location(entry.location or jd_text[:1500])

    search = profile.answers.get("search", {}) or {}
    min_score = search.get("min_judge_score") or judge.PASS_SCORE  # one number, or {judge name: score}
    max_revisions = int(search.get("max_revisions") if search.get("max_revisions") is not None else 2)
    # The user's rule: a score below the bar earns the résumé its revisions,
    # then the application goes in anyway. Only an eligibility barrier —
    # citizenship, clearance, degree level, graduation window — still stops it.
    apply_below_bar = bool(search.get("apply_below_bar", True))
    if os.environ.get("RESUME_TAILOR_APPLY_BELOW_BAR", "").strip().lower() in ("0", "false", "no"):
        apply_below_bar = False  # the launch script's word: every judge at the bar, or the posting is held

    prev = state.done.get(entry.id) or {}
    # A cached resume and verdict stand only when the verdict was a pass at
    # today's bar: a posting the judges turned down is judged again on a
    # forced retry, not waved through on the strength of having been looked at.
    cached = (prev.get("pdf") if prev.get("fit") and prev.get("pdf") and Path(prev["pdf"]).is_file()
              and prev.get("status") in RETRYABLE and int(prev.get("resume_version") or 0) >= RESUME_VERSION
              and judge.cached_scores_ok(prev.get("fit", ""), min_score)
              else None)
    o.resume_version = RESUME_VERSION
    if cached:
        # A retry. The resume and both judges' verdicts stand from the last
        # attempt; only the form is tried again, since the answer bank may have
        # grown in between.
        pdf_path = Path(cached)
        o.role = prev.get("role") or entry.title
        o.company = prev.get("company") or entry.company_hint
        o.pdf, o.fit, o.coverage = cached, prev["fit"], prev.get("coverage") or {}
        o.revisions = int(prev.get("revisions") or 0)
    else:
        # Tailor, judge, and — when a judge holds — revise from the judges'
        # notes and judge again, up to `max_revisions` times, keeping the best
        # draft. A disqualifier (citizenship, degree level, graduation window)
        # ends it early: no rewrite meets those.
        best = None  # (weakest score, result, verdicts, fit_ok)
        revision = None
        job_spec = None
        for round_no in range(max_revisions + 1):
            _now("tailoring the résumé" if not round_no else f"revising the résumé (round {round_no})", entry)
            try:
                result = await tailor(profile, jd_text, out_dir=out_dir, label=entry.id, revision=revision,
                                      variant=f"-r{round_no}" if round_no else "", job=job_spec)
            except Exception as e:
                if best is not None:
                    break
                o.status, o.detail = "error", f"tailoring failed: {e}"
                return o
            job_spec = result.job
            if round_no == 0:
                o.role, o.company = result.job.role_title, result.job.company or entry.company_hint
                if profile.blacklisted(o.company):
                    o.status, o.detail = "skipped", f"{o.company} is on your company blacklist"
                    return o
                if apply_once and o.company and state.has_applied(o.company):
                    o.status, o.detail = "skipped", f"already applied at {o.company} (apply_once_at_company)"
                    return o
                if apply_once and o.company and state.held_at(o.company, except_id=entry.id):
                    o.status, o.detail = "skipped", f"another posting at {o.company} is awaiting your approval (apply_once_at_company)"
                    return o
                if result.roles_included == 0:
                    o.status, o.detail = "skipped", "no experience survived selection and audit for this posting — poor fit"
                    return o
            # Two independent match judges, both of which must pass before
            # anything is submitted. Judged on the PDF's extracted text — what a
            # recruiter's tools will actually read — against the posting.
            _now("judging the résumé", entry)
            try:
                fit_ok, verdicts = await judge.two_independent(
                    jd_text, extract_pdf_text(result.pdf_path), profile.applicant_facts(), min_score=min_score)
            except Exception as e:
                if best is not None:
                    break
                o.status, o.detail = "error", f"fit judging failed: {e}"
                return o
            score = judge.weakest(verdicts)
            if best is None or score > best[0]:
                best = (score, result, verdicts, fit_ok)
            if fit_ok or judge.disqualified(verdicts) or round_no == max_revisions:
                break
            revision = judge.revision_notes(verdicts)
            o.revisions = round_no + 1
        score, result, verdicts, fit_ok = best
        o.pdf, o.coverage = str(result.pdf_path), result.coverage
        pdf_path = result.pdf_path
        # The résumé lint: a PDF whose text layer lost a bullet, the GPA, the
        # city or the graduation year is held for a look, never sent.
        lint = [v for v in (result.render_violations or [])
                if v.startswith(("[clipped]", "[leading-punctuation]", "[missing-in-pdf]"))]
        if lint:
            o.status, o.detail = "needs_review", "résumé lint: " + "; ".join(v[:160] for v in lint[:3])
            return o
        o.fit = judge.summarize(verdicts, min_score) + (f" (revised ×{o.revisions})" if o.revisions else "")
        if not fit_ok and judge_gate:
            if judge.disqualified(verdicts) or not apply_below_bar:
                o.status, o.detail = "fit_rejected", judge.reasons(verdicts, min_score)
                return o
            o.fit += " — applied below the bar"

    if not entry.apply_url:
        o.status, o.detail = "tailored_only", "resume generated; no apply URL was given, so nothing was submitted"
        return o

    # An explicit apply_url is respected; otherwise the ATS convention decides
    # where the form lives relative to the posting.
    apply_url = entry.apply_url if entry.apply_url != entry.url else ats.apply_url_for(entry.url)
    _now("opening the application form", entry, url=apply_url)
    try:
        await session.goto(apply_url)
        blocker = await detect_blocker(session)
        if blocker == "no_form_found" and await session.click_apply_control():
            # The posting page hid its form behind an Apply control. One click
            # (two, past a portal's interstitial), then look again — a login
            # wall or CAPTCHA behind it is reported the same way as anywhere.
            blocker = await detect_blocker(session, after_apply=True)
        if blocker == "no_form_found":
            # A company page that embeds a Greenhouse board and never showed
            # it: the board's own application page carries the same form.
            embed = await session.greenhouse_embed_url(apply_url)
            if embed:
                await session.goto(embed)
                blocker = await detect_blocker(session, after_apply=True)
    except Exception as e:
        o.status, o.detail = "error", f"could not open the application form: {e}"
        return o

    if blocker == "login_required" and _accounts_allowed(profile, _page_url(session) or apply_url):
        # A portal the user told us to make an account on (Workday): create
        # it, or sign in if an earlier attempt already did, and go on.
        _now("creating the site account", entry, url=apply_url)
        try:
            how = await _create_account(session, profile)
        except Exception as e:
            how = ""
            print(f"  account step failed: {_brief(e)}", file=sys.stderr, flush=True)
        if how == "captcha":
            o.status = "blocked"
            o.detail = ("a captcha guards this site's sign-in and registration, which the tool does not solve — "
                        f"create the account or sign in once by hand and rerun: {_page_url(session) or apply_url}")
            return o
        if how == "verify_email":
            # The account exists; the portal wants its e-mail verified first.
            # Named as an e-mail wall, so the loop leaves it alone until the
            # inbox is configured rather than retrying it every pass.
            o.status = "needs_login"
            o.detail = ("this site e-mailed an account-verification link and wants it opened before any sign-in — "
                        + ("the inbox showed no verification e-mail in time; " if mailbox.configured() else
                           "set RESUME_TAILOR_IMAP_PASSWORD (a Google app password) in ~/.resume-tailor/env and the tool will follow it; ")
                        + f"or open it in Chrome, verify by hand, and rerun: {_page_url(session) or apply_url}")
            return o
        if how:
            blocker = await detect_blocker(session, after_apply=True)
            if blocker == "no_form_found" and await session.click_apply_control():
                blocker = await detect_blocker(session, after_apply=True)
    if blocker == "login_required":
        where = _page_url(session) or entry.apply_url
        o.status = "needs_login"
        if _accounts_allowed(profile, where):
            o.detail = ("this site wants an account or a sign-in before its form — the account step ran but the wall stayed "
                        f"(see the log's 'account:' notes); open it in Chrome to finish by hand, or log in once and rerun: {where}")
        else:
            o.detail = ("this site wants an account or a sign-in before its form — its host is not in search.create_accounts_on; "
                        f"open it in Chrome to finish by hand, or log in once and rerun: {where}")
        return o
    if blocker == "bot_check":
        o.status, o.detail = "blocked", "a bot/verification check was on the page — not attempted"
        return o
    if blocker == "posting_gone":
        o.status, o.detail = "skipped", "the posting is no longer there — the page says it was removed or closed"
        return o
    if blocker == "already_applied":
        o.status, o.detail = "skipped", "the site says this account has already applied for this job — nothing to send"
        return o
    if blocker == "no_form_found":
        try:
            page_text = (await session.read_text()).strip()
        except Exception:
            page_text = ""
        if len(page_text) < 80 or re.search(r"\bERR_[A-Z_]+\b|site can.t be reached|no internet", page_text, re.I):
            # A page with nothing on it is a load that failed — the
            # connection dropped, or the page was still blank when read —
            # not a posting without a form: it takes the network
            # wait-and-retry, and does not use up a review attempt.
            o.status, o.detail = "error", "could not load the application form: the page came up blank or as a browser error"
            return o
        if ats.host_kind(entry.apply_url or entry.url) in ats.NEEDS_ACCOUNT:
            # A portal that shows its form only to an account holder (TikTok,
            # EY's Yello, Goldman, Apple): a login to do once, not a review.
            where = _page_url(session) or entry.apply_url
            o.status = "needs_login"
            o.detail = f"this site shows its form only to an account holder — log in once in the tool's browser and rerun: {where}"
            return o
        o.status, o.detail = "needs_review", "no application form was found on this page"
        return o

    _now("filling the form", entry, url=apply_url)
    form_started = time.time()
    try:
        unresolved, o.answers = await _fill_form(session, profile, pdf_path, jd_text)
        # A multi-step form: when this page has no submit control but one
        # Next/Continue, move on and fill the next page too — as long as this
        # one is complete, since the site will not advance an incomplete page.
        codes_tried = 0
        for _ in range(9):  # Workday walks six named steps before its Review page
            code_wall = any(_CODE_WALL.search(u) for u in unresolved) or await _code_fields(session)
            link_wall = not code_wall and await _link_wall(session)
            if (code_wall or link_wall) and mailbox.configured() and codes_tried < 2:
                # The site e-mailed a one-time code or a sign-in link: read it
                # from the inbox, as a person would, and carry on behind it.
                codes_tried += 1
                _now("waiting for the verification e-mail", entry, url=apply_url)
                found = await mailbox.fetch_secret_async(form_started, [entry.company_hint, "verification", "code", "link"])
                if not found:
                    break
                if code_wall and found.get("code"):
                    if not await _enter_code(session, found["code"]):
                        break
                elif found.get("link"):
                    await session.goto(found["link"])
                    await session._wait_for_fields(15)
                else:
                    break
                unresolved, more = await _fill_form(session, profile, pdf_path, jd_text)
                o.answers = o.answers + more
                continue
            if unresolved or await session.unfilled_required():
                break
            if not await session._wait_for_fields(20):
                break  # the step never rendered: nothing to fill, nothing to press
            buttons = await session.buttons()
            if _pick_submit_button(buttons) is not None:
                break
            nxt = _pick_next_button(buttons)
            if nxt is None and not _pick_submit_button(buttons):
                nxt = await _ask_button(buttons, "next")
                if nxt is not None and _NOT_SUBMIT.search(nxt.get("text", "")):
                    nxt = None  # "Back to Job Details" is not a next step
            if nxt is None:
                break
            page_before = await _step_signature(session)
            await session.advance(nxt["selector"])
            if await _step_signature(session) == page_before and await _tick_invalid_boxes(session):
                # The page stayed and flagged a box — a terms checkbox the
                # scanner could not name (Oracle's e-mail step). Ticked; again.
                await session.advance(nxt["selector"])
            if await _step_signature(session) == page_before:
                # Still the same step: the site refused to move on. When it
                # says which fields (Workday: "The field From is required and
                # must have a value", "Invalid LinkedIn URL"), those are
                # decided again with the complaint in view and the step is
                # pressed once more — twice at most. Re-filling the same page
                # nine times would only pile up answers.
                moved = False
                for round_no in range(2):
                    errors = await session.errors()
                    demanded = set(_missing_labels(errors))
                    invalid_ids = {m.group(1) for e in errors for m in [re.match(r"(rt-\d+): ", e)] if m}
                    for f in await session.describe_form():
                        label = (f.get("label") or "").strip()
                        if label and (f.get("id") in invalid_ids or any(
                                label[:30].lower() in e.lower() for e in errors if not re.match(r"rt-\d+: ", e))):
                            demanded.add(label.lower())
                    if not demanded:
                        if round_no == 0 and not errors:
                            # Nothing named and nothing said: the click may have
                            # gone to a menu the page left open (Workday's last
                            # dropdown). Once more, after closing it.
                            try:
                                await session._page.keyboard.press("Escape")
                            except Exception:
                                pass
                            await session.advance(nxt["selector"])
                            if await _step_signature(session) != page_before:
                                moved = True
                        break
                    _now(f"repairing the step (round {round_no + 1})", entry, url=apply_url)
                    unresolved, more = await _fill_form(session, profile, pdf_path, jd_text, force_required=demanded)
                    o.answers = o.answers + more
                    if unresolved or await session.unfilled_required(demanded):
                        break
                    buttons = await session.buttons()
                    again = _pick_next_button(buttons) or await _ask_button(buttons, "next")
                    if again is None or _NOT_SUBMIT.search(again.get("text", "")):
                        break
                    await session.advance(again["selector"])
                    if await _step_signature(session) != page_before:
                        moved = True
                        break
                if not moved:
                    complaints = [e for e in await session.errors() if not re.match(r"rt-\d+: ", e)][:4]
                    unresolved = unresolved or [f"the page did not advance past this step" + (": " + "; ".join(complaints) if complaints else "")]
                    break
            unresolved, more = await _fill_form(session, profile, pdf_path, jd_text)
            o.answers = o.answers + more
    except Exception as e:
        o.status, o.detail = "error", f"filling the form failed: {e}"
        return o

    still_empty = await session.unfilled_required()
    if any(re.search(r"\bpassword\b", u, re.I) for u in unresolved) or any(
            f.get("type") == "password" for f in still_empty):
        # The form creates a site account along the way. Accounts are the
        # user's to create; the tool stops here and says so.
        shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png")
        o.status = "needs_login"
        o.detail = ("this site wants an account created as part of applying (password fields) — the tool does not "
                    "create accounts; open it in Chrome to finish by hand")
        o.screenshot = str(shot)
        return o
    if any(_CODE_WALL.search(u) for u in unresolved) or any(_CODE_WALL.search(f.get("label") or "") for f in still_empty) \
            or (not still_empty and not unresolved and await _link_wall(session)):
        # The site e-mailed a one-time code and wants it typed in: only the
        # applicant's inbox can finish this one.
        shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png")
        o.status = "needs_login"
        o.detail = ("this site e-mailed a verification code and wants it typed in before the form — "
                    + ("the inbox showed no code in time; " if mailbox.configured() else
                       "set RESUME_TAILOR_IMAP_PASSWORD (a Google app password) in ~/.resume-tailor/env and the tool will read it; ")
                    + f"or open it in Chrome to finish by hand with the code from your inbox: {_page_url(session) or entry.apply_url}")
        o.screenshot = str(shot)
        return o
    if unresolved or still_empty:
        shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png")
        o.status = "needs_review"
        parts = []
        if unresolved:
            parts.append("could not answer: " + ", ".join(unresolved))
        if still_empty:
            # Filled without complaint, yet empty on re-read: the widget did
            # not take the value, or the site cleared it.
            parts.append("still empty after filling: " + ", ".join(
                (f.get("label") or f.get("name") or f.get("type") or "?")[:80] for f in still_empty))
        o.detail = "; ".join(parts)
        o.screenshot = str(shot)
        return o

    pre_shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-{'dry-run' if dry_run else 'pre-submit'}.png")
    o.screenshot = str(pre_shot)
    if dry_run:
        o.status, o.detail = "ready_not_submitted", "dry run — filled and screenshotted, submit not clicked"
        return o
    # A multi-step form ends on a Review page — the answers as text, no
    # controls, and the Submit — the one page a submit is meant for. Any other
    # page with nothing on it is a step still loading or a wall, never a form
    # to send: pressing whatever button it shows (Oracle's NEXT on a blank
    # verification step) once produced a false "applied".
    buttons = await session.buttons()
    review_page = await _is_review_page(session, profile, buttons)
    if not review_page and not await session._wait_for_fields(10):
        signin = any(_SIGNIN_TEXT.search(b.get("text") or "") for b in buttons)
        recovered = False
        if signin and _accounts_allowed(profile, _page_url(session) or apply_url):
            # An empty page with a Sign In control (RTX's globalhr tenant):
            # the same account step as a login wall, then on with the form.
            _now("creating the site account", entry, url=apply_url)
            try:
                how = await _create_account(session, profile)
            except Exception as e:
                how = ""
                print(f"  account step failed: {_brief(e)}", file=sys.stderr, flush=True)
            recovered = bool(how) and how not in ("captcha", "verify_email") and await session._wait_for_fields(10)
        if not recovered:
            shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png")
            if signin:
                o.status = "needs_login"
                o.detail = ("this site wants an account or a sign-in before its form (a sign-in button on an empty page); "
                            f"open it in Chrome to finish by hand, or log in once and rerun: {_page_url(session) or apply_url}")
            else:
                o.status, o.detail = "needs_review", "the form disappeared before submit — the page shows no fields"
            o.screenshot = str(shot)
            return o
    if not any(a.get("answer") for a in o.answers):
        o.status, o.detail = "needs_review", "nothing was filled on this page; not submitted"
        return o
    if not review_page and not _looks_like_application(await session.describe_form()):
        # A posting page's "Apply now" is not a submit, however the button
        # picker reads it: the page must hold the application itself.
        shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png")
        o.status, o.detail = "needs_review", "the page at submit time is not an application form (a job page or a sign-in step); not submitted"
        o.screenshot = str(shot)
        return o
    if not approved and _needs_approval(profile, o.company, entry.company_hint):
        # Only a page that passed every guard above — a real, filled form —
        # is worth holding for the user's approval.
        o.status = AWAITING
        o.detail = ("filled and ready — this company is on your approve-before-submit list; review the answers and "
                    "the résumé on the dashboard and press Approve and submit")
        return o
    buttons = await session.buttons()
    submit = _pick_submit_button(buttons)
    if submit is None:
        submit = await _ask_button(buttons, "submit")
        if submit is not None and (_NEXT_WORDS.match(submit.get("text", "")) or _NOT_SUBMIT.search(submit.get("text", ""))):
            submit = None  # the model may not turn a Next or Verify into a submit
    if submit is None:
        o.status = "needs_review"
        o.detail = ("could not identify a single unambiguous submit button; the page offers: "
                    + ", ".join(repr(b.get("text", "")[:30]) for b in buttons[:12]))
        o.screenshot = str(await session.screenshot(shots_dir / f"{_safe(entry.id)}-needs-review.png"))
        return o

    # "applied" means the form went, not that a button was clicked: a submit
    # the site rejected leaves the form on the page with its errors showing,
    # and that is a review item — recorded as such, with the screenshot.
    _now("submitting", entry, url=apply_url)
    submit_started = time.time()
    submitted, why = await session.submit(submit["selector"])
    rounds = 0
    while not submitted and "still on the page" in why and rounds < 3:
        # The page said what it wants: "Missing entry for required field: X",
        # a control flagged invalid, a field named in an error. Those fields
        # count as required, are decided again with the page's complaint in
        # view, and the form is sent again — up to three rounds.
        rounds += 1
        errors = await session.errors()
        demanded = set(_missing_labels(errors))
        invalid_ids = {m.group(1) for e in errors for m in [re.match(r"(rt-\d+): ", e)] if m}
        fields_now = await session.describe_form()
        for f in fields_now:
            label = (f.get("label") or "").strip()
            if not label:
                continue
            if f.get("id") in invalid_ids:
                demanded.add(label)
            elif any(label[:30].lower() in e.lower() for e in errors if not re.match(r"rt-\d+: ", e)):
                demanded.add(label)
        if not demanded:
            break
        _now(f"repairing the form (round {rounds})", entry, url=apply_url)
        try:
            more_unresolved, more = await _fill_form(session, profile, pdf_path, jd_text, force_required=demanded)
            o.answers = o.answers + [a for a in more if a.get("answer")]
            if more_unresolved or await session.unfilled_required(demanded):
                why += "; asked for: " + ", ".join(sorted(demanded))[:300]
                break
            buttons = await session.buttons()
            again = _pick_submit_button(buttons) or await _ask_button(buttons, "submit")
            if again is None or _NEXT_WORDS.match(again.get("text", "")) or _NOT_SUBMIT.search(again.get("text", "")):
                break
            submitted, why = await session.submit(again["selector"])
        except Exception as e:
            why += f"; repair round {rounds} failed: {_brief(e)}"
            break
    if not submitted and mailbox.configured():
        # Greenhouse (Coinbase) e-mails a security code after Submit and
        # holds the application until it is typed in — the "still in
        # progress" it looked like. Read the code from the inbox and finish,
        # as a person would.
        try:
            boxes = await _code_fields(session)
        except Exception:
            boxes = []
        if boxes:
            _now("waiting for the security-code e-mail", entry, url=apply_url)
            found = await mailbox.fetch_secret_async(submit_started, [entry.company_hint, "security code", "code"], timeout_s=180)
            if found and found.get("code") and await _enter_code(session, found["code"]):
                await session._page.wait_for_timeout(2500)
                try:
                    fields_after = await session.describe_form()
                    text_after = (await session.read_text()).lower()
                    still_asking = bool(await _code_fields(session))
                except Exception:
                    fields_after, text_after, still_asking = [], "", True
                if not still_asking and (not _looks_like_application(fields_after) or re.search(
                        r"thank you for applying|application (has been |was )?(submitted|received)|we'?ve received your application", text_after)):
                    submitted, why = True, "submitted — verified with the e-mailed security code"
                else:
                    why += "; the e-mailed security code was entered but the page still shows the form"
            else:
                why += "; the page asked for an e-mailed security code and none arrived in time"
    post_shot = await session.screenshot(shots_dir / f"{_safe(entry.id)}-post-submit.png")
    o.screenshot = str(post_shot)
    if not submitted:
        # A spam or bot verdict is the site's, not the form's: blocked, and
        # worth another try from a more convincing browser later.
        o.status = "blocked" if re.search(r"spam|bot\b|captcha|robot|verif", why, re.I) else "needs_review"
        o.detail = why
        return o
    o.status, o.detail = "applied", f"submitted — {why}"
    if apply_once:
        for name in (o.company, entry.company_hint):
            if name:
                state.mark_applied(name)
    return o


async def _is_review_page(session: ApplySession, profile: Profile, buttons: list[dict]) -> bool:
    """Whether the page is a multi-step form's Review page: a Submit control,
    no application controls, and the answers shown back — the word Review,
    or the applicant's own e-mail address, in the text."""
    if _pick_submit_button(buttons) is None:
        return False
    try:
        fields = await session.describe_form()
        text = (await session.read_text())[:12000]
    except Exception:
        return False
    if _looks_like_application(fields):
        return False
    email = str(profile.career.get("personal_information", {}).get("email") or profile.flat_answers().get("personal.email") or "").strip().lower()
    return bool(re.search(r"\breview\b", text[:4000], re.I)) or bool(email and email in text.lower())


def _wall_url(record: dict | None, entry: QueueEntry) -> str:
    """Where the site stopped the tool last time — the sign-in page it was
    sent to — or the posting's apply URL."""
    detail = (record or {}).get("detail") or ""
    urls = re.findall(r"https?://[^\s'\"]+", detail)
    return urls[-1].rstrip(".,;)") if urls else (entry.apply_url or entry.url)


async def login_and_apply(profile: Profile, entry: QueueEntry, out_dir: str | Path = "output",
                          state_path: str | Path | None = None, on_progress=None, timeout_s: float = 20 * 60) -> Outcome:
    """A visible Chrome window on the site's sign-in page. The user logs in
    (or creates the account) and leaves the window open; once the wall is
    gone the tool takes over in that same window — fills the form and
    submits it, as the batch would — and keeps the site's cookies so the
    next posting there goes through headless."""
    from .apply import DEFAULT_PROFILE_DIR, _looks_like_application, detect_blocker

    out_dir = Path(out_dir)
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    state = RunState.load(state_path or out_dir / "batch-state.json")
    from .profile import apply_once_at_company
    apply_once = apply_once_at_company(profile.answers)
    record = state.done.get(entry.id) or {}
    o = Outcome(entry_id=entry.id, status="by_hand", company=record.get("company") or entry.company_hint, role=entry.title,
                pdf=record.get("pdf", ""), fit=record.get("fit", ""))
    session = ApplySession(headless=False, fast=True,
                           profile_dir=DEFAULT_PROFILE_DIR.parent / "review-profiles" / _safe(entry.id)[:16])
    try:
        await session.start()
        await session.goto(_wall_url(record, entry))
        print("\nLog in to this site in the Chrome window (or create the account it asks for) and leave the window "
              "open. As soon as the sign-in wall is gone, the tool fills the application and submits it.",
              file=sys.stderr, flush=True)
        deadline = asyncio.get_event_loop().time() + timeout_s
        through = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                if session._page.is_closed():
                    break
                await session._pick_frame()
                blocker = await detect_blocker(session, after_apply=True)
                fields = await session.describe_form()
            except Exception:
                continue  # mid-navigation
            if blocker not in ("login_required", "bot_check") and fields and _looks_like_application(fields):
                through = True
                break
            if blocker not in ("login_required", "bot_check") and "/login" not in (_page_url(session) or "").lower() \
                    and (_page_url(session) or "") != _wall_url(record, entry) and not fields:
                # Signed in and back on the job page: the batch's own flow
                # (Apply, interstitials) takes it from here.
                through = True
                break
        if not through:
            o.detail = ("the sign-in window was closed, or nothing changed in twenty minutes — nothing was submitted; "
                        "open it again from the dashboard when you are ready")
            o.when = datetime.now().astimezone().isoformat(timespec="seconds")
            state.record(entry.id, asdict(o))
            return o
        try:
            await session.save_logins()
        except Exception as e:
            print(f"  (could not save the site's cookies: {_brief(e)})", file=sys.stderr, flush=True)
        print("  signed in — filling and submitting…", file=sys.stderr, flush=True)
        o = await _process_one(session, profile, entry, out_dir, shots_dir, apply_once, state, dry_run=False,
                               judge_gate=False)
        o.when = datetime.now().astimezone().isoformat(timespec="seconds")
        state.record(entry.id, asdict(o))
        if on_progress:
            on_progress(o)
        await asyncio.sleep(4)  # a moment to see the confirmation before the window goes
        return o
    finally:
        try:
            await session.stop()
        except Exception:
            pass


async def review_by_hand(profile: Profile, entry: QueueEntry, out_dir: str | Path = "output",
                         state_path: str | Path | None = None, profile_dir: str | Path | None = None,
                         on_progress=None, timeout_s: float = 4 * 3600) -> Outcome:
    """Open one posting's form in a visible Chrome window, filled in as far as
    the tool can take it, and hand it over: the window stays open for the
    user to check, complete and submit. The judges' verdict is shown but does
    not stop anything — the user asked for this one by name. If the page
    shows a confirmation while the window is open, the posting is recorded as
    applied; otherwise its dry-run verdict is recorded and it stays retryable.
    """
    from .apply import DEFAULT_PROFILE_DIR, _SUCCESS_TEXT

    out_dir = Path(out_dir)
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    state = RunState.load(state_path or out_dir / "batch-state.json")
    from .profile import apply_once_at_company
    apply_once = apply_once_at_company(profile.answers)
    # A profile of its own per posting, so the headless loop and any number
    # of review windows never fight over one Chrome profile — and a login the
    # user creates for a site stays with that posting's window.
    session = ApplySession(headless=False, fast=True,
                           profile_dir=Path(profile_dir) if profile_dir
                           else DEFAULT_PROFILE_DIR.parent / "review-profiles" / _safe(entry.id)[:16])
    try:
        o = await _process_one(session, profile, entry, out_dir, shots_dir, apply_once, state, dry_run=True,
                               judge_gate=False)
        o.when = datetime.now().astimezone().isoformat(timespec="seconds")
        if on_progress:
            on_progress(o)
        if o.status in ("skipped", "tailored_only") or session._page is None or session._page.is_closed():
            state.record(entry.id, asdict(o))
            return o
        if o.status == "error":
            # The automated fill hit a wall; the page is still there for the
            # user to take over, which is the point of this window.
            print(f"\nThe automated fill stopped ({o.detail}). The page stays open for you.", file=sys.stderr, flush=True)
        print("\nThe form is open in Chrome. Check it, finish anything left blank, and press Submit yourself.\n"
              "Close the window when you are done.", file=sys.stderr, flush=True)
        deadline = asyncio.get_event_loop().time() + timeout_s
        submitted = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                if session._page.is_closed():
                    break
                text = await session._page.evaluate("() => document.body.innerText || ''")
            except Exception:
                break  # the window went away
            if _SUCCESS_TEXT.search(text):
                submitted = True
                break
        if submitted:
            try:
                o.screenshot = str(await session.screenshot(shots_dir / f"{_safe(entry.id)}-post-submit.png"))
            except Exception:
                pass
            o.status, o.detail = "applied", "submitted by hand after review"
            o.when = datetime.now().astimezone().isoformat(timespec="seconds")
            if apply_once:
                for name in (o.company, entry.company_hint):
                    if name:
                        state.mark_applied(name)
        else:
            # Closed without a confirmation being seen. It is in the user's
            # hands now — the loop must not send it on its own; the dashboard
            # can mark it applied or skipped, or open it again.
            o.status = "by_hand"
            o.detail = ("opened in Chrome by hand and closed without a confirmation being seen — "
                        "if you submitted it, mark it applied; otherwise open it again or mark it skipped")
            o.when = datetime.now().astimezone().isoformat(timespec="seconds")
        state.record(entry.id, asdict(o))
        if on_progress:
            on_progress(o)
        return o
    finally:
        try:
            await session.stop()
        except Exception:
            pass


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s)[:60].strip("-") or "entry"


# A navigation that times out outright is the connection's doing far more
# often than the site's (four in a row this afternoon), so it takes the
# same wait-and-retry as an outright disconnect.
_NETWORK_ERROR = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_(RESET|REFUSED|TIMED_OUT)|"
                            r"ERR_ADDRESS_UNREACHABLE|ERR_TIMED_OUT|Page\.goto: Timeout \d+ms exceeded|the page came up blank")


async def _wait_for_network(timeout_s: float = 3600, every_s: float = 20) -> bool:
    """Poll until the internet answers, or give up after `timeout_s`."""
    import socket
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            await asyncio.get_event_loop().run_in_executor(None, lambda: socket.create_connection(("1.1.1.1", 443), timeout=5).close())
            return True
        except OSError:
            await asyncio.sleep(every_s)
    return False


CURRENT_FILE = Path.home() / ".resume-tailor" / "current.json"


def _worker() -> str:
    """This process's worker number ("0".."3") when it is one of several
    parallel loops (RESUME_TAILOR_SHARD=k/n), else empty."""
    import os

    return (os.environ.get("RESUME_TAILOR_SHARD") or "").split("/")[0].strip()


def _current_file() -> Path:
    w = _worker()
    return CURRENT_FILE.with_name(f"current-w{w}.json") if w else CURRENT_FILE


def _now(stage: str, entry: QueueEntry | None = None, **extra) -> None:
    """What this process is doing right now, for the dashboard: the posting
    and the stage. One file per worker. Best effort; a failure to write it
    changes nothing."""
    try:
        import json
        import os

        CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stage": stage, "pid": os.getpid(), "worker": _worker(),
                   "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "entry_id": entry.id if entry else "", "company": entry.company_hint if entry else "",
                   "role": entry.title if entry else "", **extra}
        _current_file().write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


_REPORT_COLUMNS = ["entry_id", "status", "company", "role", "fit", "detail", "pdf", "screenshot"]


def _append_report_row(path: Path, o: Outcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_REPORT_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(asdict(o))
