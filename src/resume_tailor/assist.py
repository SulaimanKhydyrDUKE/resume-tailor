"""The agent driver's side of the queue: what it works, in what order, and
how its results come back.

The unattended loop fills the boards it is good at — Greenhouse, Ashby,
Workable — and hands everything else to a Claude Code session from the
first attempt (batch.agent_driver_host → status `for_agent`), along with
the postings it held itself: needs_review (a field it would not guess),
needs_login (a wall only a person can pass), blocked (a captcha) and
awaiting_approval. The session is the sibling kit's agent-as-runtime: it
drives the tool's own Playwright browser through the resume-tailor MCP
server (a dedicated Chrome profile, visible, where the user signs in to a
portal once), takes a batch of one portal at a time (`next_batch`), reads
each posting, has the résumé and the two judges run when it gets there
(`prepare`, so a posting it never reaches costs no tokens), fills, checks a
screenshot, submits, and writes the outcome back (`record`) so the loop
never touches the posting again. An answer the user supplied along the way
is kept (`learn`) in answers.yaml → hand_answers, where Profile.lookup finds
it first on every later form.

Nothing here opens a browser; `prepare` is the one function that calls a
model, through batch.fit_for_agent. This module is the queue's edge.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import ats
from .queue import RunState, company_key

# The outcomes the driver works, in the order worth its time: a filled form
# waiting for a click first, then the postings the loop handed over unopened
# (new, so their applicant pools are still filling), then forms held on a
# field the loop would not guess (the résumé and judges already paid for),
# then walls the driver's window can pass with a sign-in, then captchas.
ASSIST_STATUSES = ("awaiting_approval", "for_agent", "needs_review", "needs_login", "blocked")
_RANK = {"awaiting_approval": 0, "for_agent": 1, "needs_review": 2, "needs_login": 4, "blocked": 5}
# A posting older than this is unlikely to be open; the brief says so and
# the queue leaves it out unless asked.
MAX_AGE_DAYS = 45
# What the driver may record. `applied` and `by_hand` mark the company applied
# to; `skipped` closes the posting; `needs_review` hands it back with a note;
# `needs_login` says a sign-in is owed; `blocked` says a captcha stood.
RECORDABLE = ("applied", "by_hand", "skipped", "needs_review", "needs_login", "blocked")
_DEFAULT_DETAIL = {"applied": "submitted by the agent driver in the tool's browser", "by_hand": "submitted by hand",
                   "skipped": "left alone on purpose", "needs_review": "handed back with a note",
                   "needs_login": "a sign-in is still needed", "blocked": "a captcha or bot check stood on the page"}


def listings_by_id(out_dir: str | Path) -> dict[str, dict]:
    """The discovery cache, keyed by listing id — for a record's URL and the
    posting's date and liveness."""
    try:
        data = json.loads((Path(out_dir) / "listings-cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ls = data.get("listings", data) if isinstance(data, dict) else data
    return {(l.get("id") or l.get("url")): l for l in (ls or []) if isinstance(l, dict)}


def _unresolved(detail: str) -> list[str]:
    """The fields a needs_review detail names: "could not answer: X — …, Y —
    …; still empty after filling: Z" → [X, Y, Z]. Best effort."""
    out: list[str] = []
    for m in re.finditer(r"(?:could not answer|still empty after filling):\s*([^;]*)", detail or ""):
        for part in re.split(r",\s*(?=[A-Z(\"'])", m.group(1)):
            name = part.split(" — ")[0].strip().rstrip(",")
            if name and name not in out:
                out.append(name[:120])
    return out


def _needs_you(rec: dict, url: str, profile=None) -> str:
    """Why the user's own hands are needed in the driver's window, or ''."""
    d = (rec.get("detail") or "").lower()
    if d.startswith("hand lane:"):
        return "a sign-in at this host, done once in the driver's window"
    if rec.get("status") == "blocked" or "captcha" in d or "bot/verification" in d:
        return "a captcha to pass in the driver's window"
    if rec.get("status") == "needs_login":
        return "a sign-in or an account at this portal"
    if profile is not None:
        try:
            from .batch import hand_lane_host
            if hand_lane_host(profile, url, rec.get("url") or ""):
                return "a sign-in at this host, done once in the driver's window"
        except Exception:
            pass
    if url and ats.host_kind(url) in ats.NEEDS_ACCOUNT:
        try:
            from .discover import logged_in_since
            if not logged_in_since(url, ""):
                return "possibly a portal account: sign in once in the driver's window if the page asks"
        except Exception:
            pass
    return ""


DEFAULT_HOST_BLACKLIST = ["linkedin.com"]  # discover.Prefs's default too: LinkedIn is the user's Chrome extension's, never this browser's


def _host_blacklisted(profile, url: str) -> bool:
    """A host the tool's browser never opens (`search.host_blacklist`,
    default LinkedIn): the same list discovery filters on."""
    hosts = DEFAULT_HOST_BLACKLIST
    if profile is not None:
        raw = (getattr(profile, "answers", {}) or {}).get("search", {}) or {}
        if raw.get("host_blacklist") is not None:
            hosts = [str(h) for h in (raw.get("host_blacklist") or [])]
    host = (urlsplit(url).netloc or "").lower()
    return any(host == h.lower() or host.endswith("." + h.lower().lstrip(".")) or host.endswith(h.lower()) for h in hosts if h)


def _tier(profile, company: str) -> int | None:
    """0 for a `search.company_tiers.first` firm, 1 for `then`, 2 otherwise,
    None for `never` — the driver works first-tier firms first and never
    touches a never-tier one (the user's 'no tier 4' rule, 2026-10-18)."""
    if profile is None or not company:
        return 2
    try:
        from .discover import _named
        tiers = (getattr(profile, "answers", {}) or {}).get("search", {}).get("company_tiers") or {}
        name = company.lower()
        if any(_named(str(c), name) for c in (tiers.get("never") or [])):
            return None
        if any(_named(str(c), name) for c in (tiers.get("first") or [])):
            return 0
        if any(_named(str(c), name) for c in (tiers.get("then") or [])):
            return 1
    except Exception:
        pass
    return 2


def _approval_required(profile, company: str) -> bool:
    """Whether the company is on `search.approve_before_submit`: the driver
    fills but leaves the submit to the dashboard's Approve."""
    if profile is None or not company:
        return False
    try:
        from .batch import _needs_approval
        return bool(_needs_approval(profile, company, company))
    except Exception:
        return False


def _pdf_state(rec: dict, profile=None) -> tuple[bool, bool]:
    """(prepared, stale). Prepared: a PDF and a verdict are on file for the
    posting. Stale: that PDF predates the résumé the user sends now — its
    composer generation is below batch.RESUME_VERSION or the file is older
    than the fixed PDF's / skeleton's last edit (tailor.resume_updated_at) —
    so it is not to be sent; `prepare` makes a new one."""
    pdf = rec.get("pdf") or ""
    if not pdf or not rec.get("fit") or not Path(pdf).is_file():
        return False, bool(pdf)
    from .batch import RESUME_VERSION
    stale = int(rec.get("resume_version") or 0) < RESUME_VERSION
    if not stale and profile is not None:
        try:
            from .tailor import resume_updated_at
            stale = Path(pdf).stat().st_mtime < resume_updated_at(profile)
        except Exception:
            stale = True
    return not stale, stale


def _rank_of(rec_or_brief: dict) -> int:
    status = rec_or_brief.get("status")
    return _RANK.get(status, 9) + (0 if rec_or_brief.get("fit") or status != "needs_review" else 1)


def queue(state: RunState, out_dir: str | Path, profile=None, limit: int = 10,
          statuses: tuple[str, ...] = ASSIST_STATUSES, max_age_days: int = MAX_AGE_DAYS,
          listings: dict[str, dict] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """The postings worth the driver's time, best first. Left out: companies
    already applied to (the one-application rule holds for the driver too),
    postings the list says are gone, and postings older than `max_age_days`
    where the date is known."""
    by = listings if listings is not None else listings_by_id(out_dir)
    now = now if now is not None else time.time()
    applied = set(state.applied_companies)
    rows: list[tuple[tuple, dict]] = []
    for rid, rec in state.done.items():
        status = rec.get("status")
        if status not in statuses:
            continue
        company = rec.get("company") or (by.get(rid) or {}).get("company_name") or ""
        if company_key(company) in applied:
            continue
        listing = by.get(rid) or {}
        if listing.get("active") is False:
            continue
        posted = int(listing.get("date_posted") or 0)
        age = (now - posted) / 86400 if posted else None
        if age is not None and max_age_days and age > max_age_days:
            continue
        url = rec.get("url") or listing.get("url") or ""
        if not url:
            continue  # nothing to open: a record from before URLs were kept, whose listing is gone from the cache
        if _host_blacklisted(profile, url):
            continue  # LinkedIn and the like: never opened in the tool's browser (search.host_blacklist)
        tier = _tier(profile, company)
        if tier is None:
            continue  # search.company_tiers.never: nobody applies there, the driver included
        b = _brief_of(rid, rec, listing, url, age, profile)
        b["tier"] = tier
        try:
            last = datetime.fromisoformat(str(rec.get("when") or "")).timestamp()
        except ValueError:
            last = 0.0
        # The user's tier first, then the newest posting (the user, 2026-10-29),
        # then the hold's rank, then the latest attempt.
        rows.append(((tier, -(posted or 0), _rank_of(rec), -last), b))
    rows.sort(key=lambda r: r[0])
    return [b for _, b in rows[:limit]] if limit else [b for _, b in rows]


def _portal_of(b: dict) -> str:
    """The batching key: the ATS kind when known (every Workday tenant is
    the same portal to drive), else the host itself."""
    return b["ats"] if b.get("ats") and b["ats"] != "other" else b["host"]


def next_batch(state: RunState, out_dir: str | Path, profile=None, size: int = 5, portal: str = "",
               statuses: tuple[str, ...] = ASSIST_STATUSES, max_age_days: int = MAX_AGE_DAYS) -> dict[str, Any]:
    """The driver's next batch: up to `size` postings on one portal, the
    sibling kit's grouping. One portal at a time means a sign-in done for
    the first posting holds for the rest and a quirk learned on one form
    applies to the next. The portal is the one holding the best posting by
    the queue's order (tier 1 firms, then the newest); `portal` (an ATS kind such
    as workday, or a host) picks one by hand. `hosts` lists every portal
    with its count so the driver can see the whole queue."""
    rows = queue(state, out_dir, profile, limit=0, statuses=statuses, max_age_days=max_age_days)
    groups: dict[str, list[dict]] = {}
    for b in rows:
        groups.setdefault(_portal_of(b), []).append(b)
    if not groups:
        return {"portal": "", "ats": "", "postings": [], "remaining": 0, "portals": []}

    def order(key: str) -> tuple:
        # The portal holding the best-tier posting, then the newest one; rows
        # arrive in that order already, so a portal's first row is its best.
        return rows.index(groups[key][0])

    keys = sorted(groups, key=order)
    pick = keys[0]
    if portal:
        want = portal.lower().strip()
        hit = [k for k in keys if k == want or k.endswith(want) or any(b["host"].endswith(want) for b in groups[k])]
        if hit:
            pick = hit[0]
    chosen = groups[pick][:size]
    return {"portal": pick, "ats": chosen[0]["ats"], "postings": chosen, "remaining": len(rows) - len(chosen),
            "portals": [{"portal": k, "count": len(groups[k])} for k in keys]}


def _brief_of(rid: str, rec: dict, listing: dict, url: str, age: float | None, profile=None) -> dict[str, Any]:
    answers = [a for a in (rec.get("answers") or []) if isinstance(a, dict)]
    decided = [a for a in answers if a.get("answer")]
    prepared, stale = _pdf_state(rec, profile)
    return {
        "id": rid,
        "company": rec.get("company") or listing.get("company_name") or "",
        "role": rec.get("role") or listing.get("title") or "",
        "url": url,
        "apply_url": ats.apply_url_for(url) if url else "",
        "host": (urlsplit(url).netloc or "").lower(),
        "ats": ats.host_kind(url) if url else "",
        "status": rec.get("status"),
        "why_stuck": rec.get("detail") or "",
        "unresolved": _unresolved(rec.get("detail") or ""),
        "needs_you": _needs_you(rec, url, profile),
        "approval_required": _approval_required(profile, rec.get("company") or listing.get("company_name") or ""),
        "fit": rec.get("fit") or "",
        "pdf": rec.get("pdf") or "",
        "prepared": prepared,
        "pdf_stale": stale,
        "screenshot": rec.get("screenshot") or "",
        "attempts": int(rec.get("attempts") or 0),
        "last_attempt": rec.get("when") or "",
        "posted_days_ago": None if age is None else round(age),
        "answers_decided": len(decided),
        "answers_total": len(answers),
    }


def brief(state: RunState, out_dir: str | Path, entry_id: str, profile=None) -> dict[str, Any] | None:
    """Everything the driver needs for one posting: the queue row plus every
    answer the loop decided (question, answer, where it came from) and the
    listing's own fields."""
    rec = state.done.get(entry_id)
    if not rec:
        return None
    by = listings_by_id(out_dir)
    listing = by.get(entry_id) or {}
    url = rec.get("url") or listing.get("url") or ""
    posted = int(listing.get("date_posted") or 0)
    age = (time.time() - posted) / 86400 if posted else None
    b = _brief_of(entry_id, rec, listing, url, age, profile)
    b["answers"] = [{"question": a.get("question") or a.get("label") or "", "section": a.get("section") or "",
                     "answer": a.get("answer") or "", "source": a.get("source") or ""}
                    for a in (rec.get("answers") or []) if isinstance(a, dict)]
    b["listing"] = {k: listing.get(k) for k in ("title", "company_name", "locations", "terms", "date_posted", "source", "active") if k in listing}
    return b


async def prepare(state: RunState, out_dir: str | Path, entry_id: str, profile, jd_text: str) -> dict[str, Any]:
    """The résumé and the two judges for one posting, when the driver gets to
    it — the loop's own fit step (batch.fit_for_agent) on the posting text
    the driver read. A cached PDF and verdict that still stand (current
    composer, current résumé, a pass at today's bar) are reused and nothing
    is spent. The record takes the PDF, the verdict, the company and role the
    posting names, without that counting as an attempt; a verdict that ends
    the posting (fit_rejected, a lint hold, nothing survived selection) is
    recorded as the agent's. A posting the user re-queued by hand is applied
    to whatever the judges say, as in the loop."""
    from .batch import fit_for_agent, posting_for_models

    rec = state.done.get(entry_id)
    if rec is None:
        return {"error": f"no record {entry_id!r}"}
    listing = (listings_by_id(out_dir) or {}).get(entry_id) or {}
    company = rec.get("company") or listing.get("company_name") or ""
    title = rec.get("role") or listing.get("title") or ""
    text, notes = posting_for_models(jd_text or "", company, title)
    if len(text.strip()) < 120:
        return {"error": "posting text too short — open the posting page in the browser first (browser_open), or pass posting_text"}
    locs = listing.get("locations")
    location = " ".join(str(x) for x in locs) if isinstance(locs, list) else str(locs or "")
    prof = profile.for_location(location or text[:1500])
    terms = listing.get("terms")
    term = str(terms[0]) if isinstance(terms, list) and terms else ""
    if term and not term.lower().startswith("summer"):
        prof = prof.for_term(term)  # winter / spring availability answers for a winter / spring posting
    detail = rec.get("detail") or ""
    judge_gate = not (detail.startswith("re-queued") or "re-queued by hand" in detail)
    if judge_gate:
        # The deterministic reads first, as in the loop (eligibility.py).
        from . import eligibility
        verdict, why = eligibility.scan(text, eligibility.facts_from_profile(prof))
        ending = None
        if verdict == "bar":
            ending = ("fit_rejected", f"eligibility: {why}")
        elif eligibility.skip_tier4(prof):
            small = eligibility.tier4_signal(text)
            if small:
                ending = ("skipped", f"company tier 4 — a seed-stage or tiny-team signal in the posting (search.skip_tier4): {small!r}")
        if ending:
            state.update(entry_id, {"status": ending[0], "detail": "assist: " + ending[1], "worker": "assist",
                                    "when": datetime.now().astimezone().isoformat(timespec="seconds")})
            return {"status": ending[0], "detail": ending[1], "cached": False, "pdf": "", "fit": "", "notes": notes, "judge_gate": True}
    res = await fit_for_agent(prof, text, entry_id, Path(out_dir), rec, judge_gate=judge_gate)
    if res.get("error"):
        return res
    fields = {k: res[k] for k in ("pdf", "fit", "company", "role", "coverage", "resume_version", "revisions") if res.get(k) not in (None, "")}
    if res.get("status"):
        fields.update(status=res["status"], detail="assist: " + res["detail"], worker="assist",
                      when=datetime.now().astimezone().isoformat(timespec="seconds"))
    state.update(entry_id, fields)
    res["notes"] = notes
    res["judge_gate"] = judge_gate
    return res


def record(state: RunState, entry_id: str, status: str, detail: str = "", company: str = "", role: str = "") -> dict[str, Any]:
    """Write the driver's outcome on top of the record. `applied` and
    `by_hand` mark the company applied to, so the loop never sends there
    again; every outcome is stamped `worker: assist`, which keeps the loop
    off the posting for good (discover._worn_out)."""
    if status not in RECORDABLE:
        raise ValueError(f"cannot record {status!r}; one of {', '.join(RECORDABLE)}")
    rec = dict(state.done.get(entry_id) or {"entry_id": entry_id})
    rec["status"] = status
    rec["detail"] = "assist: " + (detail.strip() or _DEFAULT_DETAIL[status])
    rec["when"] = datetime.now().astimezone().isoformat(timespec="seconds")
    rec["worker"] = "assist"
    if company:
        rec["company"] = company
    if role:
        rec["role"] = role
    if status in ("applied", "by_hand"):
        for name in (rec.get("company"), company):
            if name:
                state.mark_applied(name)
    state.record(entry_id, rec)
    return rec


def normalise_question(text: str) -> str:
    """A form label as a key: lowercase, no stars or punctuation, one space."""
    t = re.sub(r"\((?:[^)]*\b(?:e\.g\.|i\.e\.|such as|for example)\b[^)]*)\)", " ", text or "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return " ".join(t.split())


def learn(root: str | Path, question: str, answer: str, source: str = "assist") -> Path:
    """Keep an answer the user gave for a question the bank missed, in
    answers.yaml → hand_answers (a list of {question, answer, source,
    when}), where Profile.lookup finds it first. The file is edited in
    place with the owner's comments kept (settings.load_rt/save_rt); a
    question already there is updated, not repeated."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    from .settings import load_rt, save_rt

    path = Path(root).expanduser() / "answers.yaml"
    data = load_rt(path)
    seq = data.get("hand_answers")
    if seq is None:
        seq = CommentedSeq()
        data["hand_answers"] = seq
        data.yaml_set_comment_before_after_key("hand_answers", before=(
            "Answers you gave while finishing a form the loop could not, kept by the assist lane.\n"
            "The bank's word match looks here first, on the question's own words."))
    key = normalise_question(question)
    for item in seq:
        if isinstance(item, dict) and normalise_question(str(item.get("question") or "")) == key:
            item["answer"] = str(answer)
            item["source"] = source
            item["when"] = time.strftime("%Y-%m-%d")
            break
    else:
        entry = CommentedMap()
        entry["question"] = str(question).strip()
        entry["answer"] = str(answer)
        entry["source"] = source
        entry["when"] = time.strftime("%Y-%m-%d")
        seq.append(entry)
    save_rt(path, data)
    return path


_STOP = {"the", "a", "an", "of", "to", "in", "for", "and", "or", "your", "you", "are", "is", "do", "does", "have", "has",
         "with", "on", "at", "be", "please", "any", "this", "that", "we", "our", "us", "if", "will", "would", "can"}


def hand_answer(answers: dict, question: str) -> tuple[str | None, str | None]:
    """The hand-taught answer for a question, or (None, None): the same
    question once normalised, else a stored question whose every
    significant word the asked one carries (three or more of them, so
    "Yes" for "Are you authorized?" never lands on "Are you a veteran?")."""
    items = answers.get("hand_answers") if isinstance(answers, dict) else None
    if not isinstance(items, list) or not items:
        return None, None
    asked = normalise_question(question)
    if not asked:
        return None, None
    asked_words = {w for w in asked.split() if w not in _STOP and len(w) > 2}
    best: tuple[int, str, str] | None = None
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("answer") in (None, ""):
            continue
        stored = normalise_question(str(item.get("question") or ""))
        if not stored:
            continue
        if stored == asked:
            return str(item["answer"]), f"hand_answers[{i}]"
        words = {w for w in stored.split() if w not in _STOP and len(w) > 2}
        if len(words) >= 3 and words <= asked_words:
            if best is None or len(words) > best[0]:
                best = (len(words), str(item["answer"]), f"hand_answers[{i}]")
    return (best[1], best[2]) if best else (None, None)
