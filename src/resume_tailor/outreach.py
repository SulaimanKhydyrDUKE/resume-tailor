"""A short note to the people who hire interns, after the application is in.

For each company the loop has applied to, find a recruiting address that
is actually published somewhere — the coordinator who wrote back, the
campus team's inbox on the careers page, an address a web search finds
printed on a page — and send one e-mail from the applicant's own account:
who they are, which role, one real project, a low-key ask for a short
call, the tailored résumé attached, LinkedIn in the signature.

Rules that do not bend: one e-mail per company; never to an address that
was guessed rather than found; never after a rejection; never twice;
weekdays in working hours; a daily cap. Every send is logged in
`output/outreach.json`, and a bounce read by the inbox scan retires the
address so the next attempt uses another.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .queue import company_key

LOG_NAME = "outreach.json"
DAILY_CAP = 15
SEND_HOURS = (8, 18)  # local time, Monday to Friday
TZ = ZoneInfo("America/New_York")

SUBJECT = "{role}, Summer 2027 - quick hello"
BODY = """Hi {greeting},

This is Sulaiman, Duke CS and Math junior. I just applied for the {role} role and wanted to put a name to the application. {project}

If you have 15 minutes in the next couple of weeks I'd really like to hear what the team is looking for this summer. Happy to work around your schedule.

Resume attached. LinkedIn: {linkedin}

Thanks,
Sulaiman
"""

NOREPLY = re.compile(r"no-?reply|do-?not-?reply|donotreply|notification|mailer-daemon|postmaster|bounce|automated|noreply|"
                     r"unsubscribe|privacy|legal|press|media|sales|billing|abuse|security@|webmaster|info@|support@|help@|"
                     # Mailboxes that exist for one mechanical purpose: an assessment platform, an accommodation
                     # request line, a scheduler, a code sender — a "quick hello" there reaches no one.
                     r"assessment|accommodat|survey|feedback|verif|otp|passcode|calendar|schedul|interview@|hackerrank|"
                     r"codesignal|coderbyte|litmus|criteria|events?@|invite|marketing|newsletter|alerts?@|jobalert|onboarding", re.I)
# Words that make a sender's display name a mailbox label rather than a person
# ("Roblox Assessment", "GuideWell Talent Acquisition", "Netic Hiring Team").
TEAM_WORDS = re.compile(r"\b(team|assessments?|support|notifications?|hr|talent|recruit(ing|ment|er)s?|careers?|hiring|acquisition|"
                        r"university|campus|people|jobs?|noreply|no-reply|system|portal|candidates?|applications?|program|"
                        r"internships?|engineering|group|inc|llc|corp|company)\b", re.I)


# The applicant-tracking system's own senders and HR service desks: an
# account-verification sender, an HR support line, a servicing mailbox.
SYSTEM_BOX = re.compile(r"workday|oracle|icims|greenhouse|lever\.co|ashby|smartrecruiters|taleo|"
                        r"successfactors|brassring|hr[._-]?support|hrsupport|servicing|operations|seeyourself|^contact@|^hello@|"
                        r"wotc|^hr@|^jobs-|candidate[._-]?(care|experience|support)", re.I)
# Mail whose sender is a mechanism, whatever the address looks like.
MECHANICAL_SUBJECT = re.compile(r"verif|survey|assessment|password|your (candidate )?account|security code|one-time|wotc|"
                                r"complete your profile|sign in|log in", re.I)


def _writable(addr: str) -> bool:
    """An address a person or a recruiting team reads — and a real one: a
    page's JSON once yielded "u003e@anduril.com" (an escaped ">")."""
    local = (addr or "").split("@")[0]
    if not addr or NOREPLY.search(addr) or SYSTEM_BOX.search(addr):
        return False
    if re.fullmatch(r"u00[0-9a-f]{2}[0-9a-f]*|[0-9a-f]{8,}|x[0-9a-f]{2}", local, re.I) or len(local) < 2:
        return False
    return True


def _unligate(text: str) -> str:
    """Typographic ligatures in an older PDF's text layer ("Oﬃce") back to letters."""
    for lig, plain in (("\ufb00", "ff"), ("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb03", "ffi"), ("\ufb04", "ffl")):
        text = text.replace(lig, plain)
    return text


def _person(name: str, company: str = "") -> bool:
    """A named human: two or three capitalised words, none of them a team or
    company word, and not the company's own name."""
    words = (name or "").replace(",", " ").split()
    if not 2 <= len(words) <= 3 or TEAM_WORDS.search(name) or RECRUITING.search(name):
        return False
    if company and company.split()[0].lower() in name.lower():
        return False
    return all(re.fullmatch(r"[A-Z][a-zA-Z'’.-]+", w) for w in words)
RECRUITING = re.compile(r"recruit|campus|universit|talent|career|intern|hiring|early|college|student|people|acquisition|"
                        r"emerging|graduate|jobs@|apply|application", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ---------------------------------------------------------------------------
# The log

def load_log(out_dir: Path) -> dict:
    p = out_dir / LOG_NAME
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"sent": {}, "lookups": {}, "skipped": {}}


def save_log(out_dir: Path, log: dict) -> None:
    tmp = out_dir / (LOG_NAME + ".tmp")
    tmp.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_dir / LOG_NAME)


def sent_today(log: dict) -> int:
    today = datetime.now(TZ).date().isoformat()
    return sum(1 for s in log["sent"].values() if (s.get("sent_at") or "")[:10] == today)


def in_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    return now.weekday() < 5 and SEND_HOURS[0] <= now.hour < SEND_HOURS[1]


# ---------------------------------------------------------------------------
# Who to write to, at which company

def _score(fit: str) -> int:
    nums = [int(m) for m in re.findall(r"(\d+)/100", fit or "")]
    return min(nums) if nums else 0


def candidates(out_dir: Path) -> list[dict]:
    """One entry per company applied to: its best posting (highest judge
    score, latest on a tie), the résumé sent, the posting link."""
    state = json.loads((out_dir / "batch-state.json").read_text(encoding="utf-8"))
    listings: dict[str, dict] = {}
    cache = out_dir / "listings-cache.json"
    if cache.is_file():
        for l in json.loads(cache.read_text(encoding="utf-8")):
            listings[l.get("id") or ""] = l
    best: dict[str, dict] = {}
    for eid, rec in (state.get("done") or {}).items():
        if rec.get("status") not in ("applied", "by_hand"):
            continue
        key = company_key(rec.get("company") or "")
        if not key:
            continue
        l = listings.get(eid, {})
        cand = {"key": key, "company": rec.get("company") or l.get("company_name") or "", "role": rec.get("role") or l.get("title") or "",
                "id": eid, "url": l.get("url") or "", "pdf": rec.get("pdf") or "", "score": _score(rec.get("fit") or ""),
                "when": rec.get("when") or "", "locations": l.get("locations") or []}
        cur = best.get(key)
        if cur is None or (cand["score"], cand["when"]) > (cur["score"], cur["when"]):
            best[key] = cand
    return sorted(best.values(), key=lambda c: (-c["score"], c["when"]), reverse=False)


def _clean(addr: str) -> str:
    return addr.strip().strip(".,;:()<>[]\"'").lower()


def from_inbox(results: dict, key: str) -> list[dict]:
    co = (results.get("companies") or {}).get(key) or {}
    out = []
    for a in co.get("addresses") or []:
        addr = _clean(a.get("address") or "")
        if not _writable(addr) or MECHANICAL_SUBJECT.search(a.get("subject") or ""):
            continue
        person = _person(a.get("name") or "", co.get("company") or "")
        rank = 0 if (person and a.get("source") == "reply-to") else 1 if person else 2 if RECRUITING.search(addr) else 3
        out.append({"address": addr, "name": a.get("name") or "", "source": "inbox: " + (a.get("subject") or a.get("source") or ""), "rank": rank})
    return sorted(out, key=lambda x: x["rank"])


def _fetch(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600_000).decode("utf-8", errors="replace")


def from_pages(url: str) -> list[dict]:
    """Addresses printed on the posting page or the careers site's front page."""
    found: list[dict] = []
    host = urlsplit(url).netloc
    pages = [url] if url else []
    if host and not re.search(r"greenhouse|lever\.co|ashbyhq|myworkday|icims|smartrecruiters|oracle|workable|jobvite|taleo|successfactors|applytojob|yello|eightfold", host, re.I):
        pages.append(f"{urlsplit(url).scheme}://{host}/")
    for page in pages:
        try:
            html = _fetch(page)
        except Exception:
            continue
        text = re.sub(r"<[^>]+>", " ", html)
        for m in EMAIL.finditer(html):
            addr = _clean(m.group(0))
            if NOREPLY.search(addr) or addr.endswith((".png", ".jpg", ".gif", ".svg")) or "example.com" in addr:
                continue
            around = text[max(0, text.find(addr) - 160): text.find(addr) + 80] if addr in text else ""
            if RECRUITING.search(addr.split("@")[0]) or RECRUITING.search(around):
                if not any(f["address"] == addr for f in found):
                    found.append({"address": addr, "name": "", "source": "page: " + page[:80], "rank": 2})
    return found


def from_web(company: str, url: str) -> list[dict]:
    """gpt-4o with web search: addresses for the company's campus or
    university recruiting that are printed on a page it can cite. A guess
    from a naming pattern is reported as such and never used."""
    try:
        from openai import OpenAI, RateLimitError
        from .llm import _load_env_file
        _load_env_file()
        client = OpenAI(timeout=120, max_retries=0)
        domain = urlsplit(url).netloc if url else ""
        prompt = (
            f"Find e-mail addresses for university, campus, early-career or intern recruiting at the company \"{company}\""
            + (f" (careers site: {domain})" if domain else "") + ", or a named campus/university recruiter there. "
            "Search the company's own site, its careers pages, its university-recruiting or students pages, and public posts. "
            "Return ONLY a JSON object: {\"addresses\": [{\"address\": \"...\", \"name\": \"person or team\", \"title\": \"...\", "
            "\"page_url\": \"the page where this exact address is printed\", \"evidence\": \"published\" or \"guessed\"}]}. "
            "Mark an address \"published\" only if you saw it written on a page; an address inferred from a naming pattern is "
            "\"guessed\". If you find nothing, return {\"addresses\": []}."
        )
        r = None
        for attempt in range(4):
            try:
                r = client.responses.create(model=os.environ.get("RESUME_TAILOR_SEARCH_MODEL", "gpt-4o"),
                                            tools=[{"type": "web_search_preview"}], input=prompt)
                break
            except RateLimitError as e:
                # gpt-4o is allowed 3 requests a minute on this account: wait
                # what the server asks (or 21 s) rather than give the company up.
                if attempt == 3:
                    raise
                m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)", str(e))
                wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 21.0
                time.sleep(min(90.0, wait + 1.0))
        text = r.output_text or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = []
        for a in data.get("addresses") or []:
            addr = _clean(str(a.get("address") or ""))
            if not EMAIL.fullmatch(addr) or NOREPLY.search(addr):
                continue
            if str(a.get("evidence") or "").lower() != "published" or not str(a.get("page_url") or "").startswith("http"):
                continue
            out.append({"address": addr, "name": str(a.get("name") or "")[:60], "title": str(a.get("title") or "")[:60],
                        "source": "web: " + str(a.get("page_url"))[:100], "rank": 1 if a.get("name") and not RECRUITING.search(str(a.get("name"))) else 2})
        return out
    except Exception as e:
        print(f"  web lookup failed for {company}: {str(e)[:100]}", file=sys.stderr, flush=True)
        return []


def find_addresses(cand: dict, results: dict, log: dict, use_web: bool = True) -> list[dict]:
    key = cand["key"]
    cached = log["lookups"].get(key)
    if cached and time.time() - float(cached.get("at") or 0) < 14 * 86400:
        found = list(cached.get("addresses") or [])
    else:
        found = from_inbox(results, key) + from_pages(cand["url"])
        if not found and use_web:
            found = from_web(cand["company"], cand["url"])
        log["lookups"][key] = {"at": time.time(), "addresses": found}
    bounced = set(results.get("bounced") or [])
    own = (_smtp_creds()[0] or "").lower()
    seen: set[str] = set()
    out = []
    for f in found:
        a = f["address"]
        if a in seen or a in bounced or a == own or not _writable(a):
            continue  # the cache may hold what an earlier, looser filter let through
        if MECHANICAL_SUBJECT.search(str(f.get("source") or "")):
            continue
        # The company's own domain, or a recruiting-looking address anywhere else.
        seen.add(a)
        out.append(f)
    return sorted(out, key=lambda x: x.get("rank", 3))


# ---------------------------------------------------------------------------
# The note

def _resume_text(pdf: str) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(pdf).pages)[:6000]
    except Exception:
        return ""


def project_sentence(company: str, role: str, resume_text: str) -> str:
    """One sentence naming the résumé item most relevant to this role, in the
    applicant's voice, with nothing the résumé does not say."""
    from openai import OpenAI
    from .llm import _load_env_file
    _load_env_file()
    client = OpenAI()
    prompt = (
        f"The applicant is e-mailing a recruiter at {company} after applying for: {role}.\n"
        "Write ONE sentence, first person, casual and plain, that names the single project or role on the résumé below "
        "most relevant to that posting, with one concrete detail taken from the résumé. Rules: 18 to 30 words; start with "
        "'Most recently', 'Lately', 'This past year' or 'On the side'; no superlatives, no awards, no numbers the résumé "
        "does not state, no buzzwords, no exclamation marks; it must read like a student typed it.\n\n"
        f"RÉSUMÉ TEXT\n{resume_text[:5000]}\n\nReply with the sentence only."
    )
    r = client.chat.completions.create(model=os.environ.get("RESUME_TAILOR_MODEL", "o3-mini"),
                                       messages=[{"role": "user", "content": prompt}])
    sent = (r.choices[0].message.content or "").strip().strip('"').split("\n")[0].strip()
    # A fact guard: every capitalised word in the sentence must appear in the résumé.
    low = _unligate(resume_text).lower()
    for w in re.findall(r"\b[A-Z][A-Za-z0-9+#.-]{2,}\b", sent):
        if w.lower() not in low and w not in ("Most", "Lately", "This", "On", "I", "Duke"):
            raise ValueError(f"the sentence names {w!r}, which the résumé does not")
    if not sent or len(sent.split()) > 40:
        raise ValueError("no usable sentence")
    return sent


def compose(cand: dict, to: dict, profile_linkedin: str, resume_text: str) -> tuple[str, str]:
    name = (to.get("name") or "").strip()
    first = name.split()[0] if _person(name, cand.get("company") or "") else ""
    greeting = first if first else f"{cand['company']} recruiting team"
    project = project_sentence(cand["company"], cand["role"], resume_text)
    role = re.sub(r"\s*[-–(]\s*(summer|fall|spring)\s*20\d\d\)?\s*$", "", cand["role"], flags=re.I).strip() or "internship"
    return SUBJECT.format(role=role), BODY.format(greeting=greeting, role=role, project=project, linkedin=profile_linkedin)


# ---------------------------------------------------------------------------
# Sending

def _smtp_creds() -> tuple[str, str]:
    from .llm import _load_env_file
    _load_env_file()
    user = os.environ.get("RESUME_TAILOR_IMAP_USER") or ""
    if not user:
        try:
            from .mailbox import _profile_email
            user = _profile_email()
        except Exception:
            user = ""
    return user, (os.environ.get("RESUME_TAILOR_IMAP_PASSWORD") or "").replace(" ", "")


def send(to_addr: str, subject: str, body: str, pdf: str, display_name: str = "Sulaiman Khydyr") -> str:
    user, password = _smtp_creds()
    if not (user and password):
        raise RuntimeError("no mailbox credentials (RESUME_TAILOR_IMAP_PASSWORD)")
    msg = EmailMessage()
    msg["From"] = formataddr((display_name, user))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    p = Path(pdf)
    if p.is_file():
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename="Sulaiman_Khydyr_resume.pdf")
    host = os.environ.get("RESUME_TAILOR_SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, 465, timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)
    return msg["Message-ID"]


# ---------------------------------------------------------------------------
# The run

def run(out_dir: str | Path, dry_run: bool = True, max_send: int = DAILY_CAP, force: bool = False,
        use_web: bool = True, only: str | None = None) -> list[dict]:
    from .mailscan import load_results
    from .profile import Profile

    out_dir = Path(out_dir)
    log = load_log(out_dir)
    results = load_results(out_dir)
    profile = Profile.load()
    linkedin = str(profile.career.get("personal_information", {}).get("linkedin") or "").strip()
    if not dry_run and not force and not in_window():
        print("outside the sending window (weekdays 8:00-18:00 Eastern); use --force to send now", file=sys.stderr)
        return []
    budget = max(0, min(max_send, DAILY_CAP - sent_today(log))) if not force else max_send
    done: list[dict] = []
    for cand in candidates(out_dir):
        if budget <= 0:
            break
        key = cand["key"]
        if only and only.lower() not in (cand["company"].lower() + " " + key):
            continue
        if key in log["sent"]:
            continue
        stage = ((results.get("companies") or {}).get(key) or {}).get("stage") or ""
        if stage == "rejected":
            log["skipped"][key] = "rejected already"
            continue
        if stage in ("oa", "interview", "offer"):
            # They have already written back with an assessment or an
            # interview: a "quick hello" now would read as if that was missed.
            log["skipped"][key] = f"already in process ({stage})"
            continue
        if not cand["pdf"] or not Path(cand["pdf"]).is_file():
            log["skipped"][key] = "no résumé file on disk"
            continue
        addrs = find_addresses(cand, results, log, use_web=use_web)
        save_log(out_dir, log)
        if not addrs:
            log["skipped"][key] = "no published recruiting address found"
            continue
        to = addrs[0]
        try:
            resume_text = _resume_text(cand["pdf"])
            subject, body = compose(cand, to, linkedin, resume_text)
        except Exception as e:
            log["skipped"][key] = f"could not write the note: {str(e)[:100]}"
            continue
        entry = {"company": cand["company"], "role": cand["role"], "to": to["address"], "name": to.get("name", ""),
                 "source": to.get("source", ""), "subject": subject, "body": body, "posting_id": cand["id"], "pdf": cand["pdf"],
                 "alternatives": [a["address"] for a in addrs[1:4]]}
        if dry_run:
            done.append(entry)
            budget -= 1
            continue
        try:
            mid = send(to["address"], subject, body, cand["pdf"])
        except Exception as e:
            log["skipped"][key] = f"send failed: {str(e)[:120]}"
            save_log(out_dir, log)
            print(f"  send failed to {to['address']} ({cand['company']}): {str(e)[:120]}", file=sys.stderr, flush=True)
            continue
        entry.update({"sent_at": datetime.now(TZ).isoformat(timespec="seconds"), "message_id": mid})
        entry.pop("body", None)
        log["sent"][key] = entry
        log["skipped"].pop(key, None)
        save_log(out_dir, log)
        print(f"  sent: {cand['company']} <{to['address']}> — {subject}", file=sys.stderr, flush=True)
        done.append(entry)
        budget -= 1
        time.sleep(20)  # a person does not send fifteen e-mails in a second
    save_log(out_dir, log)
    return done


def run_cli(args) -> int:
    out = Path(args.out)
    if args.action in ("plan", "send"):
        done = run(out, dry_run=(args.action == "plan"), max_send=args.max, force=args.force, use_web=not args.no_web, only=args.only)
        for d in done:
            print(f"\n=== {d['company']} — {d['role']}\nTo: {d['name'] + ' ' if d.get('name') else ''}<{d['to']}>   ({d['source']})"
                  + (f"\nalso found: {', '.join(d['alternatives'])}" if d.get("alternatives") else "")
                  + f"\nSubject: {d['subject']}\n\n{d.get('body', '(sent)')}")
        print(f"\n{'would send' if args.action == 'plan' else 'sent'}: {len(done)}", file=sys.stderr)
        log = load_log(out)
        skipped = {k: v for k, v in log["skipped"].items() if k not in log["sent"]}
        if skipped:
            print("skipped: " + "; ".join(f"{k}: {v}" for k, v in list(skipped.items())[:20]), file=sys.stderr)
        return 0
    if args.action == "log":
        log = load_log(out)
        for k, s in sorted(log["sent"].items(), key=lambda kv: kv[1].get("sent_at") or ""):
            print(f"{s.get('sent_at','')[:16]}  {s['company'][:28]:28s} {s['to']:40s} {s['subject'][:50]}")
        print(f"sent: {len(log['sent'])}, today: {sent_today(log)}")
        return 0
    return 1
