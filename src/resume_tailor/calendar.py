"""Two calendars from what is already on disk, plus one pass over the inbox.

The first calendar is the days the loop applied: every attempt in
`batch-state.json` has a `when`, and grouping by local date gives "what
went out on the 11th", each entry linking to its posting, its PDF and its
screenshot. No model, no network.

The second is deadlines and dates: an assessment to finish by Friday, an
interview slot, a webinar. The inbox scan (mailscan.py) already keeps one
timeline entry per message, but it names a date only when its classifier
happened to see one — six of nine hundred messages at the time of writing.
`scan_deadlines` goes back over the messages worth reading (assessments,
interviews, offers, and anything whose subject talks about a deadline),
fetches each body once, and extracts the date two ways: a set of plain
patterns ("by September 30", "within 7 days", an ISO stamp) that need no
model, and gpt-4o, which reads the message and answers with the date, the
kind of thing it is, and what has to be done. The result is cached per
message in `output/deadlines.json`, so a second pass costs nothing.

Dates are sanity-checked against when the message arrived: a date before
it, or more than half a year after, is a mention ("we opened in 2023"),
not a deadline. Every message body is scrubbed (untrusted.py) before a
model sees it. `write_ics` turns both calendars into one .ics file for
Google or Apple Calendar.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .queue import company_key
from .untrusted import GUARD, scrub

TZ = ZoneInfo("America/New_York")
DEADLINES_NAME = "deadlines.json"
KINDS = ("assessment", "interview", "deadline", "event", "other")

# A subject worth fetching the body for, even when the stage is "applied"
# or "other": something is due, scheduled or expiring.
DEADLINE_WORDS = re.compile(
    r"deadline|\bdue\b|expir|complete (the|your)|finish (the|your)|within \d+ (day|hour|business)|schedul|invit|reminder|"
    r"last chance|closes?\b|time.?sensitive|action required|next steps?|assessment|hackerrank|codesignal|codility|"
    r"interview|webinar|info session|event|rsvp", re.I)
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTH_NUM = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MONTH_NUM["sept"] = 9


def _local_date(iso: str) -> str:
    """The calendar day, in the applicant's time zone, of an ISO timestamp."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    return dt.date().isoformat()


def _parse_received(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _find_dates(text: str, received: datetime) -> list[datetime]:
    """Dates a message names, without a model: "by September 30", "due
    10/03/2026", "within 7 days", "2026-09-14T23:59". Years missing from a
    month-day are the received year, or the next one when that day has
    passed. Only dates after the message arrived count."""
    found: list[datetime] = []
    t = text or ""
    year = received.year
    for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d))?", t):
        try:
            found.append(datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), tzinfo=TZ))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d\d|\d\d)\b", t):
        y = int(m[3]) if len(m[3]) == 4 else 2000 + int(m[3])
        try:
            found.append(datetime(y, int(m[1]), int(m[2]), tzinfo=TZ))
        except ValueError:
            pass
    for m in re.finditer(rf"\b(?:(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?,?\s+)?({MONTHS})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?"
                         rf"(?:[^.\n]{{0,25}}?(\d{{1,2}})(?::(\d\d))?\s*(am|pm|a\.m\.|p\.m\.))?", t, re.I):
        mon = _MONTH_NUM.get(m[1].lower()[:4], _MONTH_NUM.get(m[1].lower()[:3]))
        day = int(m[2])
        y = int(m[3]) if m[3] else year
        hour = int(m[4]) if m[4] else 0
        if m[6] and m[6].lower().startswith("p") and hour < 12:
            hour += 12
        try:
            dt = datetime(y, mon, day, hour, int(m[5] or 0), tzinfo=TZ)
        except (ValueError, TypeError):
            continue
        if not m[3] and dt < received - timedelta(days=2):
            dt = dt.replace(year=y + 1)
        found.append(dt)
    for m in re.finditer(r"within (\d{1,2}) (business )?(day|hour)s?", t, re.I):
        n = int(m[1])
        if m[3].lower() == "day":
            n = round(n * 7 / 5) if m[2] else n
            found.append(received + timedelta(days=n))
        else:
            found.append(received + timedelta(hours=n))
    return sorted({d for d in found if received - timedelta(days=1) <= d <= received + timedelta(days=183)})


def _plausible(iso: str, received_iso: str) -> bool:
    d, r = _parse_received(iso), _parse_received(received_iso)
    if d is None:
        return False
    if r is None:
        return True
    return r - timedelta(days=1) <= d <= r + timedelta(days=183)


# ---------------------------------------------------------------------------
# The inbox pass

_last_model_call = 0.0


def _model_deadline(company: str, subject: str, body: str, received: str) -> dict | None:
    """gpt-4o reads one message and says whether it sets a date: JSON with
    has_deadline, date (ISO), kind, what. None when the model is unavailable."""
    try:
        from openai import OpenAI, RateLimitError
        client = OpenAI(timeout=60, max_retries=1)
        prompt = (
            f"An e-mail received {received[:16]} (America/New_York) by a college student who applied to internships, "
            f"about the company {company}.\n\nSubject: {subject}\n\n{scrub(body)[0][:4500]}\n\n"
            "Does this message set a date the student must act by or show up on: an assessment or take-home to complete "
            "by a deadline, an interview or recruiter call at a time, an offer response deadline, an event or webinar time? "
            "Reply with JSON: {\"has_deadline\": true or false, \"date\": the ISO 8601 date or datetime (local, no timezone "
            "suffix needed) or null, \"kind\": one of \"assessment\", \"interview\", \"deadline\", \"event\", \"other\", "
            "\"what\": at most 12 words saying what must be done or attended}. A message that only mentions a past date, "
            "or says a window like 'within 7 days' with no date, sets has_deadline true with the date computed from the "
            "received time. Marketing with no action is has_deadline false."
        )
        global _last_model_call
        model = os.environ.get("RESUME_TAILOR_MAIL_MODEL", "gpt-4o")
        r = None
        for attempt in range(5):
            gap = 1.5 - (time.time() - _last_model_call)
            if gap > 0:
                time.sleep(gap)
            _last_model_call = time.time()
            try:
                r = client.chat.completions.create(model=model, messages=[{"role": "system", "content": GUARD},
                                                                         {"role": "user", "content": prompt}],
                                                   response_format={"type": "json_object"}, temperature=0)
                break
            except RateLimitError as e:
                if attempt == 4:
                    raise
                m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)", str(e))
                wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 20.0
                time.sleep(min(90.0, wait + 1.0))
        out = json.loads(r.choices[0].message.content or "{}")
        kind = str(out.get("kind") or "other").lower()
        return {"has_deadline": bool(out.get("has_deadline")), "date": out.get("date") or None,
                "kind": kind if kind in KINDS else "other", "what": str(out.get("what") or "")[:120]}
    except Exception as e:
        print(f"  model unavailable for {company}: {str(e)[:80]}", file=sys.stderr, flush=True)
        return None


def load_deadlines(out_dir: Path) -> dict:
    p = Path(out_dir) / DEADLINES_NAME
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("events", {})
            return d
        except Exception:
            pass
    return {"events": {}, "scanned_at": None}


def save_deadlines(out_dir: Path, data: dict) -> None:
    p = Path(out_dir) / DEADLINES_NAME
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def candidate_messages(results: dict) -> list[dict]:
    """Timeline entries worth reading for a date: assessments, interviews,
    offers, and anything whose subject talks about something due."""
    out = []
    for key, co in (results.get("companies") or {}).items():
        for e in co.get("timeline") or []:
            stage = e.get("stage") or ""
            if stage in ("oa", "interview", "offer") or DEADLINE_WORDS.search(e.get("subject") or "") or e.get("date"):
                out.append({"key": key, "company": co.get("company") or key, **e})
    return out


def scan_deadlines(out_dir: str | Path, use_model: bool = True, refresh: bool = False, limit: int | None = None,
                   mailbox: str = "[Gmail]/All Mail") -> dict:
    """Read the bodies of the messages worth reading and record every date
    they set, in output/deadlines.json. Messages already read are skipped
    unless `refresh`."""
    import email
    import imaplib

    from .mailscan import _body_text, _env, load_results

    out_dir = Path(out_dir)
    env = _env()
    if not (env["user"] and env["password"]):
        raise RuntimeError("the mailbox is not configured (RESUME_TAILOR_IMAP_USER / _PASSWORD)")
    results = load_results(out_dir)
    data = load_deadlines(out_dir)
    todo = [m for m in candidate_messages(results) if refresh or str(m.get("uid")) not in data["events"]]
    if limit:
        todo = todo[:limit]
    summary = {"candidates": len(todo), "read": 0, "dated": 0, "by_model": 0, "by_rule": 0}
    if not todo:
        return summary
    box = imaplib.IMAP4_SSL(env["host"], timeout=90)
    try:
        box.login(env["user"], env["password"])
        typ, _ = box.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            box.select("INBOX", readonly=True)
        for n, m in enumerate(todo, 1):
            uid = str(m.get("uid"))
            try:
                typ, bd = box.uid("fetch", uid, "(BODY.PEEK[])")
                body = _body_text(email.message_from_bytes(bd[0][1])) if bd and isinstance(bd[0], tuple) else ""
            except Exception as e:
                print(f"  [{n}/{len(todo)}] {m['company'][:28]:28s} fetch failed: {str(e)[:60]}", file=sys.stderr, flush=True)
                continue
            summary["read"] += 1
            received = _parse_received(m.get("when") or "") or datetime.now(TZ)
            rule_dates = _find_dates((m.get("subject") or "") + "\n" + body, received)
            entry = {"uid": uid, "key": m["key"], "company": m["company"], "subject": (m.get("subject") or "")[:140],
                     "received": m.get("when") or "", "stage": m.get("stage") or "other", "date": None, "kind": "other",
                     "what": "", "source": "", "rule_dates": [d.isoformat(timespec="minutes") for d in rule_dates[:5]]}
            verdict = _model_deadline(m["company"], m.get("subject") or "", body, m.get("when") or "") if use_model else None
            if verdict is not None and verdict.get("has_deadline") and verdict.get("date") and _plausible(str(verdict["date"]), m.get("when") or ""):
                entry.update({"date": str(verdict["date"]), "kind": verdict["kind"], "what": verdict["what"], "source": "model"})
                summary["by_model"] += 1
            elif rule_dates and (m.get("stage") in ("oa", "interview", "offer") or verdict is None):
                # No model, or the model saw nothing but the text names a date
                # on a message that is an assessment or interview: keep the
                # earliest, named by its stage.
                entry.update({"date": rule_dates[0].isoformat(timespec="minutes"), "source": "rule",
                              "kind": {"oa": "assessment", "interview": "interview", "offer": "deadline"}.get(m.get("stage") or "", "deadline"),
                              "what": (verdict or {}).get("what") or (m.get("note") or "")[:120]})
                summary["by_rule"] += 1
            if entry["date"]:
                summary["dated"] += 1
            data["events"][uid] = entry
            data["scanned_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            save_deadlines(out_dir, data)
            print(f"  [{n}/{len(todo)}] {m['company'][:28]:28s} {entry['date'] or '—':17s} {entry['kind'] if entry['date'] else ''} {entry['what'][:50]}",
                  file=sys.stderr, flush=True)
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return summary


def _settled_date(e: dict) -> str:
    """The event's date, with one correction: a model that answers with the
    message's own arrival time ("invitation to the event on October 7th",
    dated when it arrived) has named no date; when the text itself names a
    later one, that is the date."""
    date = str(e.get("date") or "")
    if not date:
        return ""
    d, r = _parse_received(date), _parse_received(e.get("received") or "")
    if d is not None and r is not None and abs((d - r).total_seconds()) < 2 * 3600:
        later = [x for x in (e.get("rule_dates") or []) if (_parse_received(x) or r) > r + timedelta(hours=2)]
        if later:
            return sorted(later)[0]
    return date


# ---------------------------------------------------------------------------
# The calendars

def build_calendar(out_dir: str | Path) -> dict:
    """Both calendars from the files on disk: applications by the day they
    went out (every attempt, with its status, so the page can show only the
    applied ones or all), and dated events from the inbox pass, with the
    scan's own dates as a fallback for messages the pass has not read."""
    from .dashboard import _file_url
    from .mailscan import load_results

    out_dir = Path(out_dir)
    state_path = out_dir / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"done": {}}
    listings: dict[str, dict] = {}
    cache = out_dir / "listings-cache.json"
    if cache.is_file():
        try:
            for l in json.loads(cache.read_text(encoding="utf-8")):
                listings[l.get("id") or ""] = l
        except Exception:
            pass
    shots_dir = out_dir / "screenshots"
    applied: dict[str, list[dict]] = {}
    for eid, rec in (state.get("done") or {}).items():
        day = _local_date(rec.get("when") or "")
        if not day:
            continue
        shot = None
        if shots_dir.is_dir():
            for kind in ("post-submit", "pre-submit", "needs-review", "dry-run"):
                p = shots_dir / f"{eid}-{kind}.png"
                if p.is_file():
                    shot = _file_url(out_dir, str(p))
                    break
        l = listings.get(eid, {})
        applied.setdefault(day, []).append({
            "id": eid, "company": rec.get("company") or l.get("company_name") or "", "role": rec.get("role") or l.get("title") or "",
            "status": rec.get("status") or "?", "when": rec.get("when") or "", "fit": rec.get("fit") or "",
            "url": l.get("url") or "", "pdf": _file_url(out_dir, rec.get("pdf")), "screenshot": shot,
        })
    for day in applied:
        applied[day].sort(key=lambda a: a["when"])

    results = load_results(out_dir)
    deadlines_data = load_deadlines(out_dir)
    events: dict[str, dict] = {}
    for uid, e in (deadlines_data.get("events") or {}).items():
        date = _settled_date(e)
        if date and _plausible(date, e.get("received") or ""):
            events[str(uid)] = {"uid": str(uid), "company": e.get("company") or "", "key": e.get("key") or "", "kind": e.get("kind") or "other",
                                "date": date, "what": e.get("what") or "", "subject": e.get("subject") or "",
                                "received": e.get("received") or "", "source": e.get("source") or "model"}
    for key, co in (results.get("companies") or {}).items():
        for e in co.get("timeline") or []:
            uid = str(e.get("uid"))
            if uid in events or not e.get("date") or not _plausible(str(e["date"]), e.get("when") or ""):
                continue
            if e.get("stage") not in ("oa", "interview", "offer", "applied"):
                continue
            events[uid] = {"uid": uid, "company": co.get("company") or key, "key": key,
                           "kind": {"oa": "assessment", "interview": "interview", "offer": "deadline"}.get(e.get("stage") or "", "deadline"),
                           "date": str(e["date"]), "what": e.get("note") or "", "subject": e.get("subject") or "",
                           "received": e.get("when") or "", "source": "scan"}
    deadlines: dict[str, list[dict]] = {}
    for ev in events.values():
        deadlines.setdefault(_local_date(ev["date"]) or ev["date"][:10], []).append(ev)
    for day in deadlines:
        deadlines[day].sort(key=lambda x: x["date"])
    today = datetime.now(TZ).date()
    upcoming = sorted((ev for ev in events.values() if _local_date(ev["date"]) >= today.isoformat()), key=lambda x: x["date"])[:40]
    return {
        "today": today.isoformat(),
        "applied": applied,
        "deadlines": deadlines,
        "upcoming": upcoming,
        "counts": {"attempts": sum(len(v) for v in applied.values()),
                   "applied": sum(1 for v in applied.values() for a in v if a["status"] in ("applied", "by_hand")),
                   "events": len(events)},
        "deadlines_scanned_at": deadlines_data.get("scanned_at"),
        "inbox_scanned_at": results.get("scanned_at"),
    }


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def write_ics(out_dir: str | Path, cal: dict | None = None) -> Path:
    """One .ics with both calendars: a timed event per deadline, and one
    all-day event per day of applications listing the companies."""
    out_dir = Path(out_dir)
    cal = cal or build_calendar(out_dir)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//resume-tailor//calendar//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:resume-tailor"]
    stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    for day, evs in sorted(cal["deadlines"].items()):
        for ev in evs:
            d = _parse_received(ev["date"])
            if d is None:
                continue
            timed = len(ev["date"]) > 10
            lines += ["BEGIN:VEVENT", f"UID:deadline-{ev['uid']}@resume-tailor", f"DTSTAMP:{stamp}",
                      (f"DTSTART;TZID=America/New_York:{d.strftime('%Y%m%dT%H%M%S')}" if timed else f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}"),
                      f"SUMMARY:{_ics_escape(ev['company'] + ' — ' + (ev['what'] or ev['kind']))}",
                      f"DESCRIPTION:{_ics_escape(ev['kind'] + ': ' + ev['subject'])}", "CATEGORIES:DEADLINE", "END:VEVENT"]
    for day, apps in sorted(cal["applied"].items()):
        sent = [a for a in apps if a["status"] in ("applied", "by_hand")]
        if not sent:
            continue
        d = date.fromisoformat(day)
        names = ", ".join(dict.fromkeys(a["company"] for a in sent))
        summary = f"Applied: {len(sent)} ({names[:60]}" + ("…" if len(names) > 60 else "") + ")"
        lines += ["BEGIN:VEVENT", f"UID:applied-{day}@resume-tailor", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_ics_escape(summary)}",
                  f"DESCRIPTION:{_ics_escape(chr(10).join(a['company'] + ' — ' + a['role'] for a in sent))}", "CATEGORIES:APPLIED", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    p = out_dir / "calendar.ics"
    p.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return p


def run_cli(args) -> int:
    out = Path(args.out)
    if args.action == "scan":
        s = scan_deadlines(out, use_model=not getattr(args, "no_model", False), refresh=bool(getattr(args, "refresh", False)),
                           limit=getattr(args, "limit", None))
        print(f"read {s['read']} of {s['candidates']} candidate messages: {s['dated']} set a date "
              f"({s['by_model']} by the model, {s['by_rule']} by pattern)", file=sys.stderr)
        return 0
    cal = build_calendar(out)
    if args.action == "ics":
        p = write_ics(out, cal)
        print(f"wrote {p} ({len(cal['deadlines'])} days with deadlines, {len(cal['applied'])} days with applications)", file=sys.stderr)
        return 0
    print(f"today {cal['today']} · {cal['counts']['applied']} applied over {len(cal['applied'])} days · {cal['counts']['events']} dated events")
    print("\nUPCOMING")
    for ev in cal["upcoming"][:20]:
        print(f"  {ev['date'][:16]:17s} {ev['kind']:11s} {ev['company'][:28]:28s} {ev['what'][:60]}")
    if not cal["upcoming"]:
        print("  (none on file — run `resume-tailor calendar scan` to read the inbox for dates)")
    print("\nAPPLIED, BY DAY (last 14 days with activity)")
    for day in sorted(cal["applied"])[-14:]:
        sent = [a for a in cal["applied"][day] if a["status"] in ("applied", "by_hand")]
        print(f"  {day}  {len(sent):3d} applied / {len(cal['applied'][day]):3d} attempts   " + ", ".join(dict.fromkeys(a["company"] for a in sent))[:90])
    return 0
