"""A short note to the people who hire interns, after the application is in.

For each company the loop has applied to, find a recruiting address that
is actually published somewhere — the coordinator who wrote back, the
campus team's inbox on the careers page, an address a web search finds
printed on a page — and send one e-mail from the applicant's own account:
who they are, which role, one real project, a low-key ask for a short
call, the tailored résumé attached, LinkedIn in the signature.

A named recruiter's own address is preferred to a shared mailbox
wherever one is published: a reply-to on a confirmation, a name printed
next to an address on a students page, a career-fair directory a web
search turns up. A personal-looking mailbox (first.last@) counts as a
person even with no name beside it. The shared mailbox is the fallback,
not the target; `--people` on a lookup goes back over companies that
only have a team mailbox, and on a send writes to named people only.

Rules that do not bend: one e-mail per company; never to an address that
was guessed rather than found; never after a rejection; never twice;
weekdays in working hours; a daily cap. Every send is logged in
`output/outreach.json`, and a bounce read by the inbox scan retires the
address so the next attempt uses another.

`resume-tailor outreach lookup` fills the address cache for every company
first — inbox, then the posting's pages, then a web search whose every
address is checked against the page it cites — so a send never waits on
searches and the cache can be read before anything goes out.

A connection failure — DNS, a dropped socket, a timeout, on the model call
or on SMTP — says nothing about the company, so the run waits and tries
that company again (`NETWORK_WAITS`), and stops altogether once
`OUTAGE_STOP` companies in a row are lost that way: a run that walked the
whole list during a half-hour Wi-Fi outage once marked forty companies
skipped and sent eight of its fifteen.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid, parseaddr
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .queue import company_key
from .untrusted import GUARD

LOG_NAME = "outreach.json"
PLAN_NAME = "outreach-plan.json"   # what the next live run would send, written by every run and by `audit`
HEARTBEAT_NAME = "outreach-heartbeat.json"
DAILY_CAP = 15
HOURLY_CAP = 4                     # a person does not send more than a few in an hour
FOLLOW_UP_CAP = 5                  # in-thread replies a day
FOLLOW_UP_MAX_AGE_DAYS = 10        # a thread older than this is not replied to
KILL_SWITCH = Path.home() / ".resume-tailor" / "outreach.off"   # touch it and nothing is sent, lookups go on
SEND_HOURS = (8, 18)  # local time, Monday to Friday
SEND_PAUSE = 20  # seconds between two e-mails: a person does not send fifteen in a second
NETWORK_WAITS = (30, 60, 120)  # seconds to wait after a connection failure before trying the same company again
OUTAGE_STOP = 2  # companies lost in a row to the connection before the run stops rather than walking the list
TZ = ZoneInfo("America/New_York")

SUBJECT = "{role}, Summer 2027 - quick hello"
BODY = """Hi {greeting},

This is Sulaiman, a Duke CS and Math junior. I just applied for the {role} role and wanted to put a name to the application. {project}

If you have 15 minutes in the next couple of weeks I'd really like to hear what the team is looking for this summer. Happy to work around your schedule.

Resume attached. LinkedIn: {linkedin}

Thanks,
Sulaiman
"""

NOREPLY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|replies@|notification|mailer-daemon|postmaster|bounce|automated|noreply|"
                     r"unsubscribe|privacy|legal|press|media|sales|billing|abuse|security@|webmaster|info@|support@|help@|"
                     # Mailboxes that exist for one mechanical purpose: an assessment platform, an accommodation
                     # request line, a scheduler, a code sender — a "quick hello" there reaches no one.
                     r"assessment|accommodat|accessib|disabilit|survey|feedback|verif|otp|passcode|calendar|schedul|interview@|hackerrank|"
                     # Mailboxes for everything a company publishes an address for except hiring.
                     r"ethics|compliance|whistle|fraud|investor|\bir@|\bpr@|dataprotection|gdpr|benefits|payroll|procurement|vendor|supplier|"
                     r"codesignal|coderbyte|litmus|criteria|events?@|invite|marketing|newsletter|alerts?@|jobalert|onboarding", re.I)
# Words that make a sender's display name a mailbox label rather than a person
# ("Roblox Assessment", "GuideWell Talent Acquisition", "Netic Hiring Team").
TEAM_WORDS = re.compile(r"\b(team|assessments?|support|notifications?|hr|talent|recruit(ing|ment|er)s?|careers?|hiring|acquisition|"
                        r"university|campus|people|jobs?|noreply|no-reply|system|portal|candidates?|applications?|programs?|"
                        r"internships?|engineering|group|inc|llc|corp|company|human|resources?|department|office|cent(er|re)|"
                        r"services?|desk|staffing|employment|admin|info|contact|help|solutions|technologies|systems|partners|"
                        r"associates|ltd|global|corporate|college|students?|early|onboarding|payroll|benefits)\b", re.I)
# Mailbox words that are not a first or last name, for telling "dominique.burns@"
# from "campus.recruiting@", "north.america@" or "new.grad@".
NOT_A_NAME = set("""us uk na emea apac latam intern interns summer early college school student students new grad grads global
    corporate north south east west america americas canada europe asia africa india tech technology data software engineering
    product design research dev eng it web email mail contact info apply applications jobs job work talent hiring hire hires
    recruit careers career staffing hr people team teams office general enquiries inquiries questions help support service
    services customer client partner partners media press news events event alumni campus university universities diversity
    inclusion dei program programs programme internship internships onboarding payroll benefits legal privacy security ops admin
    sales marketing finance accounting learning training education academy volunteer community foundation giving brand comms
    communications external internal relations public affairs policy government gov federal state city county health care
    medical clinical group inc llc corp co company ltd the and of for at in on to from by with join hello hi hey ask get go be
    my our your no not do reply noreply mailer daemon""".split())
NAME_LOCAL = re.compile(r"^([a-z]{2,20})[._-]([a-z]{2,25})$")


# The applicant-tracking system's own senders and HR service desks: an
# account-verification sender, an HR support line, a servicing mailbox.
SYSTEM_BOX = re.compile(r"workday|oracle|icims|greenhouse|lever\.co|ashby|smartrecruiters|taleo|"
                        r"successfactors|brassring|hr[._-]?support|hrsupport|servicing|operations|seeyourself|default|system|^contact@|^hello@|"
                        r"wotc|^hr@|^jobs-|candidate[._-]?(care|experience|support)|help[._-]?desk|ask[._-]?hr|shared[._-]?services|"
                        r"administrat|assistance|^ip[._-]?admin|^admin@|^it[._-]?(help|support|admin|service)|^ops@|^office@|^mail@|"
                        r"^enquir|^inquir|^team@", re.I)
# Mail whose sender is a mechanism, whatever the address looks like.
MECHANICAL_SUBJECT = re.compile(r"verif|survey|assessment|password|your (candidate )?account|security code|one-time|wotc|"
                                r"complete your (profile|application)|continue (to apply|your application)|almost there|confirm your identity|"
                                r"sign in|log in", re.I)


# Hosts that only route mail — bulk-sending subdomains, ATS relays, ticket
# systems — and local parts that are tokens rather than names: a ticketing
# reply-to was once "69e25fca-1c38-…@pinpoint.email", a CRM's
# "equifax-careers-n2yxkfylr7@talentcrm.equifax.com".
RELAY_HOST = re.compile(r"(^|\.)(mail|e-?mails?|marketing\d*|careeralerts?|jobalerts?|alerts?|notify|notifications?|talentcrm|"
                        r"hrsystem|orc|recruiting|recruitment|bounces?|repl(y|ies)|mailer|mkt|news|links?|click|em|e)\.|"
                        r"pinpoint\.email$|default\.com$|greenhouse-mail\.io$|hire\.lever\.co$|icims\.com$|myworkday(jobs)?\.com$|"
                        r"smartrecruiters\.com$|applytojob\.com$|jobvite\.com$|ashbyhq\.com$|zendesk\.com$|freshdesk\.com$|"
                        r"helpscout\.net$|intercom-mail\.com$|hubspot|salesforce|mailchimp|sendgrid|amazonses|indeed\.com$|"
                        r"ziprecruiter\.com$|linkedin\.com$|pure\.cloud$|genesys|force\.com$|salesforce-sites", re.I)
TOKEN_LOCAL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|-(?=[a-z]*\d)[a-z0-9]{8,}$|^(?=[a-z]*\d)[a-z0-9]{24,}$", re.I)
# Mail that is plainly about an application, whoever sent it.
APPLICATION_SUBJECT = re.compile(r"appl(y|ying|ied|ication)|candidate|position|\brole\b|intern|interview|recruit|hiring|"
                                 r"thank(s| you)|follow[- ]?up|next step|submission|your (r\u00e9sum\u00e9|resume)", re.I)


def _writable(addr: str) -> bool:
    """An address a person or a recruiting team reads — and a real one: a
    page's JSON once yielded "u003e@anduril.com" (an escaped ">"), and a
    ticketing system's reply-to was a UUID at pinpoint.email."""
    local, _, host = (addr or "").partition("@")
    if not addr or not host or NOREPLY.search(addr) or SYSTEM_BOX.search(addr):
        return False
    if RELAY_HOST.search(host) or TOKEN_LOCAL.search(local) or not re.search(r"\.[a-z]{2,}$", host, re.I):
        return False
    if re.fullmatch(r"u00[0-9a-f]{2}[0-9a-f]*|[0-9a-f]{8,}|x[0-9a-f]{2}", local, re.I) or len(local) < 2:
        return False
    return True


def _about_company(addr: str, subject: str, company: str) -> bool:
    """Whether an inbox address belongs to this company's hiring: it comes
    from the company's own domain, or the mail is plainly about an
    application. The inbox scan's company match is loose enough that a
    Devpost newsletter once stood for "Post Consumer Brands" and a UPS
    mailing for "TikTok"; neither is anyone to write to."""
    host = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    parts = host.split(".")
    root = parts[-2] if len(parts) >= 2 else host
    key = company_key(company)
    squashed = re.sub(r"[^a-z0-9]", "", key)
    tokens = [t for t in key.split() if len(t) >= 5]
    if root and len(root) >= 3 and squashed and (root in squashed or squashed in root or any(t in root for t in tokens)):
        return True
    return bool(APPLICATION_SUBJECT.search(subject or ""))


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
    if re.search(r"\b(no|do not|don'?t) ?reply\b|\bauto(mated|matic)?\b|\bsystem\b|\bmailer\b", name, re.I):
        return False
    if company and company.split()[0].lower() in name.lower():
        return False
    return all(re.fullmatch(r"[A-Z][a-zA-Z'’.-]+", w) for w in words)


def _personal_local(addr: str) -> str:
    """The name a personal-looking mailbox spells out: "dominique.burns@" is
    Dominique Burns; "careers@", "hr.lplfinancial@", "campus.recruiting@"
    and "dboren@" are not (the last may well be a person, but it names no
    one, so it is ranked by what is printed beside it)."""
    m = NAME_LOCAL.fullmatch(addr.split("@")[0].lower())
    if not m or not _writable(addr):  # "bootstrap-icons@1.10.5" spells no one's name
        return ""
    host = addr.rsplit("@", 1)[-1].lower()
    roots = {p for p in host.split(".") if len(p) >= 3}
    for w in m.groups():
        if w in NOT_A_NAME or TEAM_WORDS.search(w) or RECRUITING.search(w) or NOREPLY.search(w + "@") or SYSTEM_BOX.search(w + "@"):
            return ""
        if w in roots or re.search(r"recommend|digest|update|newsletter|tips|ideas|insight|alert|promo|offer|survey", w):
            return ""  # "pinterest-recommendations@ideas.pinterest.com" names a feed, not a person
    return " ".join(w.title() for w in m.groups())


CAP_WORD = re.compile(r"[A-Z][a-zA-Z'’.-]+")


def _name_near(addr: str, around: str, company: str = "") -> str:
    """A person's name printed beside an address that is plainly theirs:
    "Contact David Boren, University Recruiter, at dboren@…". Every two- and
    three-word run of capitalised words is tried ("Contact David Boren" is
    not a person; "David Boren" is), and the name's first or last name must
    show in the mailbox, so a name beside careers@ attaches to nothing."""
    local = re.sub(r"[^a-z]", "", addr.split("@")[0].lower())
    toks = [re.sub(r"[^A-Za-z'’.-]", "", w) for w in (around or "").split()]
    for i in range(len(toks)):
        for size in (2, 3):
            window = toks[i:i + size]
            if len(window) < size or not all(CAP_WORD.fullmatch(w) for w in window):
                break
            name = " ".join(window)
            if not _person(name, company):
                continue
            parts = [re.sub(r"[^a-z]", "", w.lower()) for w in window]
            first, last = parts[0], parts[-1]
            if (len(last) >= 3 and last[:4] in local) or (len(first) >= 3 and first in local):
                return name
    return ""


def _ranked(f: dict, company: str = "") -> dict:
    """An address entry with its rank settled by what it is now known to be:
    a named human or a personal-looking mailbox ranks 1 (a confirmation's
    reply-to keeps 0); an entry whose "name" is a label ("Human Resources")
    is a team mailbox whatever an earlier pass said. An address on a page
    that would not load stays where the search left it."""
    f = dict(f)
    name = str(f.get("name") or "")
    rank = int(f.get("rank", 3))
    if "unreachable" in str(f.get("source") or ""):
        return f
    guess = _personal_local(f["address"])
    if _person(name, company):
        f["rank"] = min(rank, 1)
    elif guess:
        f["rank"], f["name"] = min(rank, 1), guess
    elif rank <= 1:
        f["rank"] = 2
    return f
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
    return {"sent": {}, "lookups": {}, "skipped": {}, "replies": {}, "tests": {}}


def save_log(out_dir: Path, log: dict) -> None:
    """Write the log merged with whatever another process wrote since this
    one loaded it — a lookup pass and a send may run at the same time —
    under a lock and atomically. Sent and lookup entries are merged, this
    process winning where both hold one; skip reasons are this run's own."""
    import fcntl

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / LOG_NAME
    with open(out_dir / (LOG_NAME + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                on_disk = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            except json.JSONDecodeError:
                on_disk = {}
            merged = {}
            for section in ("sent", "lookups", "replies", "tests"):
                merged[section] = dict(on_disk.get(section) or {})
                merged[section].update(log.get(section) or {})
            merged["skipped"] = dict(log.get("skipped") or {})
            tmp = out_dir / (LOG_NAME + ".tmp")
            tmp.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            for section, value in merged.items():
                log[section] = value
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sent_today(log: dict) -> int:
    today = datetime.now(TZ).date().isoformat()
    return sum(1 for s in log["sent"].values() if (s.get("sent_at") or "")[:10] == today)


def sent_since(log: dict, hours: float, section: str = "sent") -> int:
    """Sends in the last `hours`, from the log's timestamps."""
    floor = datetime.now(TZ) - timedelta(hours=hours)
    n = 0
    for s in (log.get(section) or {}).values():
        try:
            if datetime.fromisoformat(s.get("sent_at") or "") >= floor:
                n += 1
        except ValueError:
            continue
    return n


def _own_addresses() -> set[str]:
    """Every address that is the applicant's own: the mailbox read, the
    record's e-mail, RESUME_TAILOR_OWN_ADDRESSES. Never written to, never
    harvested as a recruiter (the Duke address printed in a confirmation body
    once ranked as a named person at fourteen companies)."""
    from .mailbox import own_addresses
    return own_addresses(_smtp_creds()[0])


def outreach_enabled(profile=None) -> tuple[bool, str]:
    """The kill switch: ~/.resume-tailor/outreach.off, or answers.yaml →
    outreach.enabled false. Lookups keep running; nothing is sent."""
    if KILL_SWITCH.is_file():
        return False, f"{KILL_SWITCH} exists"
    answers = getattr(profile, "answers", None) or {}
    cfg = answers.get("outreach") if isinstance(answers.get("outreach"), dict) else {}
    if cfg.get("enabled", True) in (False, 0, "false", "no", "off"):
        return False, "answers.yaml → outreach.enabled is off"
    return True, ""


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
                "when": rec.get("when") or "", "locations": l.get("locations") or [],
                "company_url": str(l.get("company_url") or "")}
        cur = best.get(key)
        if cur is None or (cand["score"], cand["when"]) > (cur["score"], cur["when"]):
            best[key] = cand
    return sorted(best.values(), key=lambda c: (-c["score"], c["when"]), reverse=False)


def _clean(addr: str) -> str:
    return addr.strip().strip(".,;:()<>[]\"'").lower()


def _domain_tie(addr: str, company: str) -> bool:
    host = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    parts = host.split(".")
    root = parts[-2] if len(parts) >= 2 else host
    key = company_key(company)
    squashed = re.sub(r"[^a-z0-9]", "", key)
    tokens = [t for t in key.split() if len(t) >= 5]
    return bool(root and len(root) >= 3 and squashed and (root in squashed or squashed in root or any(t in root for t in tokens)))


def from_inbox(results: dict, key: str, own: set[str] | None = None) -> list[dict]:
    """The addresses the inbox scan harvested for this company, ranked. A
    named human survives a mechanical subject (the recruiter who sent the
    assessment invitation is a person; "assessment" in the subject says only
    that the mail was about one), but a person must be tied to the company by
    domain or by the company's name in the mail — a stranger's Teams link in
    a body is not a recruiter here. The applicant's own addresses never count."""
    co = (results.get("companies") or {}).get(key) or {}
    company = co.get("company") or ""
    own = own if own is not None else _own_addresses()
    out = []
    for a in co.get("addresses") or []:
        addr = _clean(a.get("address") or "")
        subject = a.get("subject") or ""
        if not addr or addr in own or not _writable(addr):
            continue
        person = _person(a.get("name") or "", company)
        guess = "" if person else _personal_local(addr)
        if person or guess:
            # A person counts only from the company's own domain: an event
            # organiser whose subject says "Inside Adobe" is not Adobe's recruiter.
            if not _domain_tie(addr, company):
                continue
        elif MECHANICAL_SUBJECT.search(subject) or not _about_company(addr, subject, company):
            continue
        rank = 0 if (person and a.get("source") == "reply-to") else 1 if (person or guess) else 2 if RECRUITING.search(addr) else 3
        out.append({"address": addr, "name": (a.get("name") if person else guess) or "",
                    "source": "inbox: " + (a.get("subject") or a.get("source") or ""), "rank": rank})
    return sorted(out, key=lambda x: x["rank"])


def _fetch(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600_000).decode("utf-8", errors="replace")


ATS_HOST = re.compile(r"greenhouse|lever\.co|ashbyhq|myworkday|icims|smartrecruiters|oracle|workable|jobvite|taleo|successfactors|"
                      r"applytojob|yello|eightfold|bamboohr|rippling|jazz|breezy|recruitee|teamtailor|paylocity|paycom|ultipro|adp\.com", re.I)
CAMPUS_LINK = re.compile(r"universit|campus|student|early.?career|intern|graduate|new.?grad|emerging|entry.?level", re.I)
_HREF = re.compile(r'<a\s[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _campus_links(html: str, base: str, limit: int = 6) -> list[str]:
    """Links on a careers page that lead to its students, university or
    early-careers pages — on the same site, in page order, a few at most."""
    from urllib.parse import urljoin
    host = urlsplit(base).netloc.lower()
    root = ".".join(host.split(".")[-2:])
    out: list[str] = []
    for href, text in _HREF.findall(html):
        label = re.sub(r"<[^>]+>", " ", text)
        if not (CAMPUS_LINK.search(href) or CAMPUS_LINK.search(label)):
            continue
        full = urljoin(base, href.split("#")[0])
        h = urlsplit(full).netloc.lower()
        if not full.startswith("http") or not (h == host or h.endswith("." + root)) or full in out:
            continue
        if full.rstrip("/") == base.rstrip("/") or re.search(r"\.(pdf|png|jpe?g|gif|svg|css|js)(\?|$)", full, re.I):
            continue
        out.append(full)
        if len(out) >= limit:
            break
    return out


def _harvest(html: str, page: str, found: list[dict]) -> None:
    """Every recruiting-looking address printed on one page: in the text, in
    mailto links, or anywhere on a page that is itself a students or
    university page."""
    text = re.sub(r"<[^>]+>", " ", html)
    campus_page = bool(CAMPUS_LINK.search(urlsplit(page).path))
    posting_page = bool(ATS_HOST.search(urlsplit(page).netloc))
    candidates_ = [m.group(0) for m in EMAIL.finditer(html)] + re.findall(r"mailto:([^\"'?#<>\s]+)", html, re.I)
    for raw in candidates_:
        addr = _clean(raw)
        if not EMAIL.fullmatch(addr) or not _writable(addr) or addr.endswith((".png", ".jpg", ".gif", ".svg")) or "example.com" in addr:
            continue
        at = text.find(addr)
        around = text[max(0, at - 600): at + 200] if at >= 0 else ""
        local = addr.split("@")[0]
        named_recruiting = bool(RECRUITING.search(local + "@"))
        # A posting page prints addresses for accommodation requests and
        # technical help far more often than for recruiters: there, the
        # mailbox's own name has to say recruiting. Elsewhere, "if you need
        # an accommodation to apply, e-mail …" disqualifies an opaque name.
        if posting_page and not named_recruiting:
            continue
        if re.search(r"accommodat|disabilit|accessib|assistance", around, re.I) and not named_recruiting:
            continue
        near = text[max(0, at - 160): at + 80] if at >= 0 else ""
        name = _name_near(addr, near) or _personal_local(addr)
        if campus_page or named_recruiting or RECRUITING.search(around) or (name and RECRUITING.search(near)):
            if not any(f["address"] == addr for f in found):
                found.append({"address": addr, "name": name, "source": "page: " + page[:80], "rank": 1 if name else 2})


def _company_site(cand: dict, results: dict) -> str:
    """The company's own web site, for the careers crawl: the listing's
    company_url when it is not a job-board profile page, else the domain the
    company's own confirmation mail came from (an ATS relay does not count)."""
    cu = str(cand.get("company_url") or "")
    if cu.startswith("http") and not re.search(r"simplify\.jobs|linkedin\.com|indeed\.com", cu, re.I):
        return cu
    co = (results.get("companies") or {}).get(cand["key"]) or {}
    for a in co.get("addresses") or []:
        addr = _clean(a.get("address") or "")
        host = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        if not host or ATS_HOST.search(host) or RELAY_HOST.search(host) or BROKER_HOST.search(host):
            continue
        parts = host.split(".")
        root = ".".join(parts[-2:]) if len(parts) >= 2 else host
        if _about_company(addr, "", cand.get("company") or "") and root.split(".")[0] not in ("gmail", "outlook", "yahoo"):
            return f"https://{root}/"
    return ""


def from_pages(url: str, company_url: str = "") -> list[dict]:
    """Addresses printed on the posting page, the careers site's front page,
    the company's own site and its /careers page, and the students,
    university or early-careers pages those link to. Ten fetches at most."""
    found: list[dict] = []
    starts = [url] if url else []
    host = urlsplit(url).netloc if url else ""
    if host and not ATS_HOST.search(host):
        starts.append(f"{urlsplit(url).scheme}://{host}/")
    if company_url:
        cu = company_url if company_url.startswith("http") else "https://" + company_url
        root = ".".join(urlsplit(cu).netloc.lower().split(".")[-2:])
        # The corporate root, then the places a careers site usually lives.
        starts += [cu, cu.rstrip("/") + "/careers", f"https://careers.{root}/", f"https://jobs.{root}/", f"https://{root}/careers/"]
    queue = list(dict.fromkeys(starts))
    seen: set[str] = set()
    fetched = 0
    while queue and fetched < 12:
        page = queue.pop(0)
        if page in seen:
            continue
        seen.add(page)
        try:
            html = _fetch(page)
        except Exception:
            continue
        fetched += 1
        _harvest(html, page, found)
        if page in starts:  # only the entry pages spawn further links
            for link in _campus_links(html, page):
                if link not in seen:
                    queue.append(link)
    return found


# People-search and data-broker sites: an address there is scraped, usually
# masked, and never something the company published. A search that cites one
# has not found a recruiting address.
BROKER_HOST = re.compile(r"signalhire|rocketreach|zoominfo|contactout|apollo\.io|growjo|lusha|hunter\.io|kaspr|wiza|lead411|seamless\.ai|"
                         r"clearbit|snov\.io|getprospect|anymailfinder|voilanorbert|findthatlead|aeroleads|uplead|adapt\.io|cognism|datanyze|"
                         r"leadiq|seamless\.ai|snov\.io|anymail|aeroleads|clearbit|adapt\.io|salesql|getprospect|"
                         r"emailfinder|findymail|datanyze|crunchbase|theorg\.com|allbiz|vcnewsdaily|manta\.com|buzzfile|dnb\.com|"
                         r"bbb\.org|glassdoor|yellowpages|opencorporates|bloomberg|pitchbook|owler|cbinsights|dealroom", re.I)


def _broker_page(url: str) -> bool:
    return bool(BROKER_HOST.search(urlsplit(url).netloc or ""))


def _cited_broker(source: str) -> bool:
    """Whether a cached address's source names a directory or people-search
    page — one an earlier, looser lookup may have accepted."""
    m = re.search(r"https?://\S+", source or "")
    return bool(m) and _broker_page(m.group(0))


def _printed_on(addr: str, url: str) -> bool | None:
    """Whether the address is actually printed on the page a search cited:
    True or False when the page loads, None when it cannot be fetched."""
    try:
        html = _fetch(url)
    except Exception:
        return None
    low = html.lower()
    local, _, host = addr.lower().partition("@")
    forms = (addr.lower(), f"{local} [at] {host}", f"{local} (at) {host}", f"{local} at {host}",
             f"{local}&#64;{host}", f"{local}%40{host}")
    return any(f in low for f in forms)


PERSON_PROMPT = (
    "Find a NAMED recruiter at the company \"{company}\"{site} whose own individual e-mail address is printed on a public page: "
    "a university, campus, early-careers, intern or technical recruiter, or a university-relations manager. Places such "
    "addresses get printed: university career-fair employer directories and 'employer contact' pages, career-center event "
    "listings, conference and hackathon sponsor pages, 'meet the recruiting team' pages on the company's site, the recruiter's "
    "own public posts, event flyers, press releases. Report only a person's individual address (first.last@, flast@, first@), "
    "never a shared mailbox such as careers@, recruiting@, hr@ or talent@. Do NOT use people-search or data-broker sites "
    "(SignalHire, RocketReach, ZoomInfo, ContactOut, Apollo, Growjo, Lusha, Hunter, Kaspr, Wiza) and never report a masked, "
    "partial or pattern-inferred address. Return ONLY a JSON object: {{\"addresses\": [{{\"address\": \"...\", \"name\": "
    "\"First Last\", \"title\": \"...\", \"page_url\": \"the page where this exact address is printed\", \"evidence\": "
    "\"published\" or \"guessed\"}}]}}. Mark an address \"published\" only if you saw it written on that page. If you find "
    "no such person, return {{\"addresses\": []}}."
)


def from_web(company: str, url: str, people: bool = False) -> list[dict]:
    """A web search (RESUME_TAILOR_SEARCH_MODEL, gpt-4o by default) for
    the company's campus or university recruiting addresses printed on a
    page it can cite. Each address is then looked for on that page: printed
    there, it is used; absent from a page that loads, it is dropped; on a
    page that will not load, it is kept only when its domain is the
    company's own, and at the lowest rank. A guess from a naming pattern is
    reported as such and never used. With `people`, the search asks for a
    named recruiter's own address and keeps nothing else."""
    try:
        from openai import OpenAI, RateLimitError
        from .llm import _load_env_file
        _load_env_file()
        client = OpenAI(timeout=180, max_retries=2)  # the SDK retries connection errors and 429s with backoff
        domain = urlsplit(url).netloc if url else ""
        prompt = (
            f"Find e-mail addresses for university, campus, early-career or intern recruiting at the company \"{company}\""
            + (f" (careers site: {domain})" if domain else "") + ", or a named campus/university recruiter there. "
            "Search the company's own site, its careers and university-recruiting pages, university career-center pages, and "
            "public posts by the company or its recruiters. Do NOT use people-search or data-broker sites (SignalHire, RocketReach, "
            "ZoomInfo, ContactOut, Apollo, Growjo, Lusha, Hunter, Kaspr, Wiza) and never report a masked or partial address. "
            "Return ONLY a JSON object: {\"addresses\": [{\"address\": \"...\", \"name\": \"person or team\", \"title\": \"...\", "
            "\"page_url\": \"the page where this exact address is printed\", \"evidence\": \"published\" or \"guessed\"}]}. "
            "Mark an address \"published\" only if you saw it written on that page; an address inferred from a naming pattern is "
            "\"guessed\". If you find nothing, return {\"addresses\": []}."
        )
        if people:
            prompt = PERSON_PROMPT.format(company=company, site=f" (careers site: {domain})" if domain else "")
        prompt = GUARD + "\n\n" + prompt  # the pages the search reads are third-party text (untrusted.py)
        r = None
        for attempt in range(4):
            try:
                r = client.responses.create(model=os.environ.get("RESUME_TAILOR_SEARCH_MODEL", "gpt-4o"),
                                            tools=[{"type": "web_search_preview"}], input=prompt)
                break
            except RateLimitError as e:
                # gpt-4o here is 50 requests but only 10k tokens a minute, and
                # a search answer is several thousand tokens: a few lookups at
                # once trip the token limit. Wait what the server asks (a few
                # seconds), never minutes — a company that stays limited is
                # retried on the next pass, not sat on.
                if attempt == 3:
                    raise
                m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)", str(e))
                wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 8.0
                time.sleep(min(30.0, wait + 1.0))
        text = r.output_text or ""
        debug = bool(os.environ.get("RESUME_TAILOR_DEBUG"))
        if debug:
            print(f"  [web] {company}: {text[:600]!r}", file=sys.stderr, flush=True)
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = []
        for a in data.get("addresses") or []:
            addr = _clean(str(a.get("address") or ""))
            page = str(a.get("page_url") or "")
            verdict = ""
            if not EMAIL.fullmatch(addr) or not _writable(addr):
                verdict = "not a writable address"
            elif str(a.get("evidence") or "").lower() != "published" or not page.startswith("http"):
                verdict = "not marked published on a page"
            elif _broker_page(page):
                verdict = "cited page is a data broker"
            if verdict:
                if debug:
                    print(f"  [web] {company}: drop {addr} — {verdict}", file=sys.stderr, flush=True)
                continue
            printed = _printed_on(addr, page)
            if printed is False or (printed is None and not _about_company(addr, "", company)):
                if debug:
                    print(f"  [web] {company}: drop {addr} — {'not printed on' if printed is False else 'unreachable, foreign domain:'} {page[:80]}",
                          file=sys.stderr, flush=True)
                continue
            name = str(a.get("name") or "")[:60]
            person = _person(name, company)
            if not person:
                name = _personal_local(addr)
            if people and not name:
                if debug:
                    print(f"  [web] {company}: drop {addr} — not a person's own address", file=sys.stderr, flush=True)
                continue
            rank = (1 if name else 2) if printed else 3
            out.append({"address": addr, "name": name, "title": str(a.get("title") or "")[:60],
                        "source": ("web: " if printed else "web, page unreachable: ") + page[:100], "rank": rank})
        return out
    except Exception as e:
        if _network_error(e):  # says nothing about the company: the caller waits, and nothing is cached
            raise NetworkDown(f"web search: {str(e)[:100]}") from e
        print(f"  web lookup failed for {company}: {str(e)[:100]}", file=sys.stderr, flush=True)
        return []


def _has_person(found: list[dict], company: str = "") -> bool:
    return any(_ranked(f, company)["rank"] <= 1 for f in found)


def search(cand: dict, results: dict, use_web: bool = True, people: bool = True) -> list[dict]:
    """Every source for one company: the inbox, the pages, then a web
    search when nothing was found, and a person-targeted web search when
    all that was found is a shared mailbox."""
    found = from_inbox(results, cand["key"]) + from_pages(cand["url"], _company_site(cand, results))
    if use_web and not found:
        found = from_web(cand["company"], cand["url"])
    if use_web and people and not _has_person(found, cand["company"]):
        found = found + [f for f in from_web(cand["company"], cand["url"], people=True)
                         if f["address"] not in {g["address"] for g in found}]
    return found


def find_addresses(cand: dict, results: dict, log: dict, use_web: bool = True, cache_only: bool = False) -> list[dict]:
    """The addresses for one company, best first. With `cache_only` (what
    plan and send use) nothing slow runs: the cache, whatever its age, plus
    the inbox, which is in memory — a company never looked up has no
    address yet and the lookup lane will get to it. The lookup pass is the
    only caller that fetches pages or searches the web."""
    key = cand["key"]
    cached = log["lookups"].get(key)
    if cache_only or not use_web:
        found = list((cached or {}).get("addresses") or [])
        found += [f for f in from_inbox(results, key) if f["address"] not in {g["address"] for g in found}]
    elif cached and time.time() - float(cached.get("at") or 0) < 14 * 86400:
        found = list(cached.get("addresses") or [])
    else:
        found = search(cand, results, use_web=use_web)
        log["lookups"][key] = {"at": time.time(), "addresses": found}
    bounced = set(results.get("bounced") or [])
    own = _own_addresses()
    seen: set[str] = set()
    out = []
    for f in found:
        f = _ranked(f, cand.get("company") or "")  # the cache holds the rank an earlier, looser pass gave
        a = f["address"]
        if a in seen or a in bounced or a in own or not _writable(a):
            continue  # the cache may hold what an earlier, looser filter let through
        if (MECHANICAL_SUBJECT.search(str(f.get("source") or "")) and not (f.get("name") or _personal_local(a))) or _cited_broker(str(f.get("source") or "")):
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


def _sentence_problems(sent: str, resume_text: str) -> list[str]:
    """Why a drafted sentence cannot go: a capitalised word or a number the
    résumé does not print, or no sentence at all. The same idea as the
    résumé's own gates — the note may only say what the document it is
    attached to says."""
    from .gates import numbers_in
    plain = _unligate(resume_text)
    low = plain.lower()
    if not sent or len(sent.split()) > 40:
        return ["no usable sentence"]
    out = []
    for w in re.findall(r"\b[A-Z][A-Za-z0-9+#.-]{2,}\b", sent):
        if w.lower() not in low and w not in ("Most", "Lately", "This", "On", "I", "Duke"):
            out.append(f"names {w!r}, which the résumé does not")
    allowed = numbers_in(plain)
    for n in sorted(numbers_in(sent)):
        if n not in allowed:
            out.append(f"uses the number {n}, which the résumé does not state")
    return out


def project_sentence(company: str, role: str, resume_text: str) -> str:
    """One sentence naming the résumé item most relevant to this role, in the
    applicant's voice, with nothing the résumé does not say. A sentence the
    guard rejects is sent back once with the reason; a second failure skips
    the company rather than sending a note the résumé does not back."""
    from openai import OpenAI
    from .llm import _load_env_file
    _load_env_file()
    client = OpenAI(timeout=120)  # the SDK retries a connection failure twice on its own; the run waits beyond that
    prompt = (
        f"The applicant is e-mailing a recruiter at {company} after applying for: {role}.\n"
        "Write ONE sentence, first person, casual and plain, that names the single project or role on the résumé below "
        "most relevant to that posting, with one concrete detail taken from the résumé. Rules: 18 to 30 words; start with "
        "'Most recently', 'Lately', 'This past year' or 'On the side'; no superlatives, no awards, no numbers the résumé "
        "does not state, no buzzwords, no exclamation marks; it must read like a student typed it.\n\n"
        f"RÉSUMÉ TEXT\n{resume_text[:5000]}\n\nReply with the sentence only."
    )
    note = ""
    problems = ["no usable sentence"]
    for _attempt in range(2):
        r = client.chat.completions.create(model=os.environ.get("RESUME_TAILOR_MODEL", "o3-mini"),
                                           messages=[{"role": "user", "content": prompt + note}])
        sent = (r.choices[0].message.content or "").strip().strip('"').split("\n")[0].strip()
        problems = _sentence_problems(sent, resume_text)
        if not problems:
            return sent
        note = ("\n\nYour previous sentence was rejected because it " + "; ".join(problems)
                + ". Write a different sentence using only names and numbers printed in the résumé text above.")
    raise ValueError(problems[0] if problems[0].startswith("no usable") else f"the sentence {problems[0]}")


def _role_label(role: str) -> str:
    """The posting's title without its term, since the subject line names
    the term itself: "Tech & Data Program Summer 2027 - Software Engineer
    Intern" reads "Tech & Data Program - Software Engineer Intern"."""
    pieces = re.split(r"\b(?:summer|fall|spring|winter)\s*'?(?:20)?\d\d\b", role or "", flags=re.I)
    parts = [" ".join(q.strip(" -\u2013\u2014:|()[]").split()) for q in pieces]
    return " - ".join(q for q in parts if q) or "internship"


def _greeting(to: dict, company: str) -> str:
    name = (to.get("name") or "").strip()
    if not _person(name, company):
        name = _personal_local(to.get("address") or "")
    return name.split()[0] if name else f"{company} recruiting team"


def compose(cand: dict, to: dict, profile_linkedin: str, resume_text: str) -> tuple[str, str]:
    greeting = _greeting(to, cand.get("company") or "")
    project = project_sentence(cand["company"], cand["role"], resume_text)
    role = _role_label(cand["role"])
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


class NetworkDown(RuntimeError):
    """The connection failed through every wait in NETWORK_WAITS."""


def _network_error(e: BaseException) -> bool:
    """A failure of the connection rather than of the company: DNS, a
    refused or dropped socket, a timeout, the OpenAI SDK's connection error.
    Bad credentials, a refused recipient or a rejected sentence are not."""
    import socket
    if isinstance(e, (NetworkDown, socket.gaierror, socket.timeout, TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected,
                      smtplib.SMTPConnectError)):
        return True
    if isinstance(e, OSError) and getattr(e, "errno", None) in (8, 51, 54, 60, 61, 64, 65):
        return True
    if type(e).__name__ in ("APIConnectionError", "APITimeoutError"):
        return True
    msg = str(e).lower()
    return any(t in msg for t in ("connection error", "nodename nor servname", "timed out", "connection reset",
                                  "network is unreachable", "temporary failure in name resolution"))


def _through_outages(fn):
    """Call fn; after a connection failure wait and call it again, once per
    entry in NETWORK_WAITS; raise NetworkDown when the last try fails the
    same way. Any other failure raises at once, since it says something
    about the company rather than the connection."""
    for wait in NETWORK_WAITS:
        try:
            return fn()
        except Exception as e:
            if not _network_error(e):
                raise
            print(f"  connection failed ({str(e)[:60]}); waiting {wait}s before trying again", file=sys.stderr, flush=True)
            time.sleep(wait)
    try:
        return fn()
    except Exception as e:
        if not _network_error(e):
            raise
        raise NetworkDown(str(e)[:120]) from e


class SendBlocked(RuntimeError):
    """The account cannot send at all right now — bad credentials, Gmail's
    daily limit — so a run stops instead of marking every company skipped."""


def build_message(to_addr: str, subject: str, body: str, pdf: str | None, display_name: str = "Sulaiman Khydyr",
                  in_reply_to: str | None = None, references: str | None = None, extra_headers: dict | None = None,
                  user: str = "") -> EmailMessage:
    """The mail as it goes: From the applicant's own account, a Message-ID on
    the account's domain (the laptop's hostname was going out in it, a spam
    heuristic), a Date, the résumé attached when a PDF is given, and the
    threading headers when this is a reply."""
    msg = EmailMessage()
    msg["From"] = formataddr((display_name, user))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(TZ))
    msg["Message-ID"] = make_msgid(domain=user.rsplit("@", 1)[-1] if "@" in user else None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    p = Path(pdf) if pdf else None
    if p and p.is_file():
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename="Sulaiman_Khydyr_resume.pdf")
    return msg


def send(to_addr: str, subject: str, body: str, pdf: str | None, display_name: str = "Sulaiman Khydyr",
         in_reply_to: str | None = None, references: str | None = None, extra_headers: dict | None = None) -> str:
    user, password = _smtp_creds()
    if not (user and password):
        raise SendBlocked("no mailbox credentials (RESUME_TAILOR_IMAP_PASSWORD)")
    msg = build_message(to_addr, subject, body, pdf, display_name, in_reply_to, references, extra_headers, user=user)
    host = os.environ.get("RESUME_TAILOR_SMTP_HOST", "smtp.gmail.com")
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=60) as s:
            s.login(user, password)
            refused = s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise SendBlocked(f"the mailbox refused the app password: {str(e)[:120]}") from e
    except smtplib.SMTPResponseException as e:
        if e.smtp_code in (550, 552, 554) and re.search(r"limit|quota|exceeded", str(e.smtp_error), re.I):
            raise SendBlocked(f"the account's sending limit: {str(e.smtp_error)[:120]}") from e
        raise
    if refused:
        raise RuntimeError(f"the server refused the recipient: {refused}")
    return msg["Message-ID"]


# ---------------------------------------------------------------------------
# The run

def refusal(cand: dict, to: dict, log: dict, results: dict, profile, own: set[str]) -> str:
    """Why this address at this company must not be written to, or "". Every
    rule here is one an unattended sender needs: the applicant's own address,
    a mailbox nobody reads, a person already written to under another company
    key, a bounced address or domain, someone who asked not to be contacted,
    a company in process or rejected, blacklisted, or held for approval."""
    a = to["address"].lower()
    dom = a.rsplit("@", 1)[-1]
    if a in own:
        return "the applicant's own address"
    if not _writable(a):
        return "not a mailbox a person reads"
    sent = (log.get("sent") or {}).values()
    for s_ in sent:
        if (s_.get("to") or "").lower() == a:
            return f"{a} already written to ({s_.get('company')})"
    if int(to.get("rank", 3)) > 1 and any((s_.get("to") or "").lower().endswith("@" + dom) for s_ in sent):
        return f"a mailbox at {dom} already written to"
    if a in {b.lower() for b in results.get("bounced") or []} or dom in {b.lower() for b in results.get("bounced_domains") or []}:
        return "bounced before"
    source = str(to.get("source") or "")
    if "unreachable" in source or _cited_broker(source):
        return "found on a page that could not be checked, or on a data broker"
    if source.startswith("web") and int(to.get("rank", 3)) <= 1 and not RECRUITING.search(str(to.get("title") or "") + " " + source):
        return "a named person from the web with no recruiting role beside the name (a supplier directory, a sales contact)"
    if a in {b.lower() for b in results.get("opt_out") or []} or dom in {b.lower() for b in results.get("opt_out_domains") or []}:
        return "asked not to be contacted"
    search = ((getattr(profile, "answers", None) or {}).get("search") or {}) if profile is not None else {}
    company = (cand.get("company") or "").lower()
    if any(b and b.lower() in company for b in (search.get("company_blacklist") or [])):
        return "company blacklisted"
    if any(b and re.search(r"(?<![a-z0-9])" + re.escape(b.lower()) + r"(?![a-z0-9])", company) for b in (search.get("approve_before_submit") or [])):
        return "company on approve_before_submit"
    return ""


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(out_dir: str | Path, dry_run: bool = True, max_send: int = DAILY_CAP, force: bool = False,
        use_web: bool = False, only: str | None = None, people_only: bool = False, test_to: str | None = None,
        quiet: bool = False) -> list[dict]:
    """One pass over the companies applied to, best fit first: find an
    address (from the cache and the inbox — never the web, unless `use_web`:
    the lookup lane does that), write the note, send it, log it. A dry run
    plans and sends nothing. `test_to` delivers the real note for one company
    to one of the applicant's OWN addresses (anything else is refused), with
    the intended recipient in the subject and a header, logged under
    `tests`, never under `sent` — the way the pipeline is proved. Every run
    writes output/outreach-plan.json: what went or would go, and why each
    other company was left out."""
    from .mailscan import load_results
    from .profile import Profile

    out_dir = Path(out_dir)
    log = load_log(out_dir)
    log["skipped"] = {}  # this run's reasons only; a reason left from an earlier run once read as a live failure
    results = load_results(out_dir)
    profile = Profile.load()
    linkedin = str(profile.career.get("personal_information", {}).get("linkedin") or "").strip()
    own = _own_addresses()
    say = (lambda m: None) if quiet else _progress
    if test_to and test_to.lower() not in own:
        raise ValueError(f"--to may only name one of the applicant's own addresses ({', '.join(sorted(own))}), never anyone else")
    if test_to and not only:
        raise ValueError("--to needs --only <company>: one company's note, delivered to you")
    enabled, why_off = outreach_enabled(profile)
    if not dry_run and not test_to and not enabled:
        say(f"outreach is switched off ({why_off}); nothing sent")
        _write_plan(out_dir, [], {"*": f"switched off: {why_off}"}, 0)
        return []
    if not dry_run and not force and not test_to and not in_window():
        say("outside the sending window (weekdays 8:00-18:00 Eastern); use --force to send now")
        _write_plan(out_dir, [], {"*": "outside the sending window"}, 0)
        return []
    if test_to:
        budget = 1
    elif force or dry_run:
        budget = max_send  # a plan shows the whole list the caps would be applied to
    else:
        budget = max(0, min(max_send, DAILY_CAP - sent_today(log), HOURLY_CAP - sent_since(log, 1)))
    done: list[dict] = []
    outages = 0  # companies lost in a row to the connection
    cands = candidates(out_dir)
    t0 = time.time()

    def lost(key: str, company: str, e: NetworkDown) -> bool:
        """Record a company lost to the connection; True when the run should stop."""
        nonlocal outages
        outages += 1
        log["skipped"][key] = f"network down: {e}"
        save_log(out_dir, log)
        say(f"  {company}: the connection failed through every wait ({e}); left for the next run")
        if outages >= OUTAGE_STOP:
            say(f"  the connection has failed for {outages} companies in a row; stopping here so the rest of the list "
                "is not marked skipped. Run again once the network is back.")
            return True
        return False

    for n, cand in enumerate(cands, 1):
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
        try:
            addrs = _through_outages(lambda: find_addresses(cand, results, log, use_web=use_web))  # no web → the cache and the inbox only
        except NetworkDown as e:
            if lost(key, cand["company"], e):
                break
            continue
        if use_web:
            save_log(out_dir, log)
        if not addrs:
            log["skipped"][key] = "no published recruiting address found" if key in log["lookups"] else "not looked up yet (the lookup lane will)"
            continue
        to = None
        for a in addrs:
            if people_only and int(a.get("rank", 3)) > 1:
                break  # the rest rank lower still
            why = refusal(cand, a, log, results, profile, own)
            if not why:
                to = a
                break
            log["skipped"][key] = f"{a['address']}: {why}"
        if to is None:
            if key not in log["skipped"]:
                log["skipped"][key] = f"no named recruiter found, only {addrs[0]['address']}"
            continue
        try:
            resume_text = _resume_text(cand["pdf"])
            subject, body = _through_outages(lambda: compose(cand, to, linkedin, resume_text))
        except NetworkDown as e:
            if lost(key, cand["company"], e):
                break
            continue
        except Exception as e:
            log["skipped"][key] = f"could not write the note: {str(e)[:100]}"
            continue
        outages = 0
        entry = {"company": cand["company"], "role": cand["role"], "to": to["address"], "name": to.get("name", ""),
                 "rank": int(to.get("rank", 3)), "source": to.get("source", ""), "subject": subject, "body": body,
                 "posting_id": cand["id"], "pdf": cand["pdf"], "alternatives": [a["address"] for a in addrs[1:4]]}
        say(f"  [{n}/{len(cands)} {int(time.time() - t0)}s] {cand['company'][:30]}: {'plan' if dry_run else 'send'} -> "
            f"{to['address']}" + (f" ({to['name']})" if to.get('name') else "") + f"  [{to.get('source', '')[:50]}]")
        if dry_run:
            done.append(entry)
            budget -= 1
            continue
        deliver_to = test_to or to["address"]
        headers = None
        if test_to:
            subject = f"[OUTREACH TEST -> {to['address']}] {subject}"
            headers = {"X-Resume-Tailor-Test": key, "X-Resume-Tailor-Intended-To": to["address"]}
        try:
            mid = _through_outages(lambda: send(deliver_to, subject, body, cand["pdf"], extra_headers=headers) if headers
                                   else send(deliver_to, subject, body, cand["pdf"]))
        except NetworkDown as e:
            if lost(key, cand["company"], e):
                break
            continue
        except SendBlocked as e:
            log["skipped"][key] = f"sending stopped: {str(e)[:120]}"
            save_log(out_dir, log)
            say(f"  sending stopped: {e}")
            break
        except Exception as e:
            log["skipped"][key] = f"send failed: {str(e)[:120]}"
            save_log(out_dir, log)
            say(f"  send failed to {deliver_to} ({cand['company']}): {str(e)[:120]}")
            continue
        entry.update({"sent_at": datetime.now(TZ).isoformat(timespec="seconds"), "message_id": mid})
        if test_to:
            entry.update({"delivered_to": deliver_to, "intended_to": to["address"], "subject": subject})
            log["tests"][mid] = entry
            log["skipped"].pop(key, None)
            save_log(out_dir, log)
            say(f"  sent (test): {cand['company']} -> {deliver_to} (intended {to['address']}) — {subject}")
            done.append(entry)
            break
        log["sent"][key] = entry
        log["skipped"].pop(key, None)
        save_log(out_dir, log)
        say(f"  sent: {cand['company']} <{to['address']}> — {subject}")
        done.append(entry)
        budget -= 1
        time.sleep(SEND_PAUSE)
    save_log(out_dir, log)
    _write_plan(out_dir, done, {k: v for k, v in log["skipped"].items() if k not in log["sent"]}, budget, dry_run=dry_run)
    return done


def _write_plan(out_dir: Path, done: list[dict], refused: dict, budget_left: int, dry_run: bool = True) -> None:
    """output/outreach-plan.json: what this run sent or would send, and why
    every other company was left out — the audit the owner reads before
    the next live run, and what the dashboard can show."""
    try:
        data = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "dry_run": dry_run, "budget_left": budget_left,
                "would_send" if dry_run else "sent": [{k: v for k, v in d.items() if k != "pdf"} for d in done],
                "refused": dict(sorted(refused.items()))}
        tmp = out_dir / (PLAN_NAME + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out_dir / PLAN_NAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Follow-ups: replying in-thread to the people who already wrote

FOLLOW_UP_TEMPLATES = {
    "interview": "Hi {first},\n\nThanks for setting this up, confirming I'll be there. Looking forward to it.\n\nSulaiman",
    "oa": "Hi {first},\n\nFinished the {company} assessment, thanks for the invite. Happy to talk more whenever useful.\n\nSulaiman",
}
_OA_DONE = re.compile(r"thanks for (taking|completing)|assessment (submitted|completed)|completed:|you have completed|results? (received|submitted)", re.I)
_INTERVIEW_MAIL = re.compile(r"interview", re.I)
_AUTOMATED_HEADERS = ("List-Unsubscribe", "Auto-Submitted", "Precedence")


def _imap_box(mailbox_name: str = "[Gmail]/All Mail"):
    """A read-only IMAP session on the applicant's mailbox."""
    import imaplib
    from .mailscan import _env
    env = _env()
    if not (env["user"] and env["password"]):
        raise SendBlocked("the mailbox is not configured (RESUME_TAILOR_IMAP_USER / _PASSWORD)")
    box = imaplib.IMAP4_SSL(env["host"], timeout=90)
    box.login(env["user"], env["password"])
    typ, _ = box.select(f'"{mailbox_name}"', readonly=True)
    if typ != "OK":
        box.select("INBOX", readonly=True)
    return box


def fetch_headers(box, uid: str) -> dict:
    """The headers a reply needs from the original, by uid: Message-ID,
    References, Reply-To, From, Subject, the Gmail thread id, and the
    automation tells (List-Unsubscribe, Auto-Submitted, Precedence)."""
    import email
    typ, data = box.uid("fetch", str(uid), "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES REPLY-TO FROM TO SUBJECT DATE LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return {}
    meta = data[0][0].decode(errors="replace") if isinstance(data[0][0], bytes) else str(data[0][0])
    msg = email.message_from_bytes(data[0][1])
    out = {k: (msg.get(k) or "").strip() for k in ("Message-ID", "References", "Reply-To", "From", "To", "Subject", "Date",
                                                     "List-Unsubscribe", "Precedence", "Auto-Submitted")}
    m = re.search(r"X-GM-THRID (\d+)", meta)
    out["thrid"] = m.group(1) if m else ""
    return out


def thread_replied_by(box, thrid: str, addrs: set[str]) -> bool:
    """Whether any of `addrs` (the applicant's own) already wrote in this
    Gmail thread — then the follow-up is not owed."""
    if not thrid:
        return False
    for a in addrs:
        try:
            typ, data = box.uid("search", None, "X-GM-THRID", thrid, "FROM", f'"{a}"')
            if typ == "OK" and data and data[0]:
                return True
        except Exception:
            continue
    return False


def _reply_target(hdrs: dict, company: str, own: set[str]) -> dict | None:
    """The person to reply to: the Reply-To when it is a writable human
    address, else the From; a team or system mailbox is nobody."""
    for raw in (hdrs.get("Reply-To") or "", hdrs.get("From") or ""):
        name, addr = parseaddr(raw)
        addr = _clean(addr)
        if not addr or addr in own or not _writable(addr):
            continue
        person = _person(name, company)
        guess = "" if person else _personal_local(addr)
        if person or guess:
            return {"address": addr, "name": name if person else guess, "source": "thread"}
    return None


def follow_up_candidates(results: dict, log: dict, own: set[str], max_age_days: int = FOLLOW_UP_MAX_AGE_DAYS) -> list[dict]:
    """The inbound messages that deserve a reply, newest first: an interview
    invitation or confirmation from a person, or an assessment invitation
    from a person once a later mail shows the assessment was taken. Never a
    company that rejected, never a thread already replied to."""
    out = []
    floor = datetime.now(TZ) - timedelta(days=max_age_days)
    replied_keys = {(r.get("key"), r.get("stage")) for r in (log.get("replies") or {}).values()}
    for key, co in (results.get("companies") or {}).items():
        if (co.get("stage") or "") == "rejected":
            continue
        tl = sorted(co.get("timeline") or [], key=lambda e: e.get("when") or "")
        done_oa = any(_OA_DONE.search(e.get("subject") or "") for e in tl)
        for e in tl:
            stage = e.get("stage") or ""
            if stage not in ("interview", "oa"):
                continue
            if stage == "oa" and not done_oa:
                continue
            if stage == "interview" and not _INTERVIEW_MAIL.search(e.get("subject") or ""):
                continue
            try:
                when = datetime.fromisoformat(str(e.get("when") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=TZ)
            if when < floor or (key, stage) in replied_keys:
                continue
            name, addr = parseaddr(e.get("from") or "")
            addr = _clean(addr)
            if not addr or addr in own or not _writable(addr) or not (_person(name, co.get("company") or "") or _personal_local(addr)):
                continue
            out.append({"key": key, "company": co.get("company") or key, "uid": str(e.get("uid")), "stage": stage,
                        "when": e.get("when") or "", "subject": e.get("subject") or "", "from": e.get("from") or ""})
    # newest first, one per company and stage
    seen: set[tuple] = set()
    uniq = []
    for c in sorted(out, key=lambda c: c["when"], reverse=True):
        if (c["key"], c["stage"]) in seen:
            continue
        seen.add((c["key"], c["stage"]))
        uniq.append(c)
    return uniq


def compose_follow_up(stage: str, to: dict, company: str) -> str:
    first = (to.get("name") or "").split()[0] if to.get("name") else "there"
    return FOLLOW_UP_TEMPLATES[stage].format(first=first, company=company)


def run_follow_ups(out_dir: str | Path, dry_run: bool = True, max_send: int = FOLLOW_UP_CAP, force: bool = False,
                   only: str | None = None, self_test: bool = False, quiet: bool = False) -> list[dict]:
    """Reply in-thread to the people who wrote: a short thank-you on an
    interview invitation, a short note once an assessment is done. Fixed
    templates in the applicant's voice; no model, no attachment, no dates.
    One reply per company and stage, ever (logged under `replies` by the
    original Message-ID). `self_test` replies to the newest mail the
    applicant sent himself with "[resume-tailor self-test]" in the subject
    — the whole path, nobody else touched."""
    from .mailscan import load_results
    from .profile import Profile

    out_dir = Path(out_dir)
    log = load_log(out_dir)
    results = load_results(out_dir)
    profile = Profile.load()
    own = _own_addresses()
    say = (lambda m: None) if quiet else _progress
    enabled, why_off = outreach_enabled(profile)
    if not dry_run and not self_test and not enabled:
        say(f"outreach is switched off ({why_off}); nothing sent")
        return []
    if not dry_run and not force and not self_test and not in_window():
        say("outside the sending window (weekdays 8:00-18:00 Eastern); use --force to send now")
        return []
    budget = 1 if self_test else max(0, min(max_send, FOLLOW_UP_CAP - sent_since(log, 24, "replies")))
    done: list[dict] = []
    box = _imap_box()
    try:
        if self_test:
            cands = []
            typ, data = box.uid("search", None, "SUBJECT", '"[resume-tailor self-test]"')
            uids = (data[0] or b"").split() if typ == "OK" else []
            for uid in reversed(uids[-10:]):
                hdrs = fetch_headers(box, uid.decode())
                _, addr = parseaddr(hdrs.get("From") or "")
                if _clean(addr) in own and hdrs.get("Message-ID") and hdrs["Message-ID"] not in log["replies"]:
                    cands.append({"key": "self-test", "company": "Self Test", "uid": uid.decode(), "stage": "interview",
                                  "when": hdrs.get("Date") or "", "subject": hdrs.get("Subject") or "", "from": hdrs.get("From") or "",
                                  "_hdrs": hdrs})
                    break
            if not cands:
                say("self-test: no unanswered mail from your own address with '[resume-tailor self-test]' in its subject")
        else:
            cands = follow_up_candidates(results, log, own)
        for c in cands:
            if budget <= 0:
                break
            if only and only.lower() not in (c["company"].lower() + " " + c["key"]):
                continue
            hdrs = c.get("_hdrs") or fetch_headers(box, c["uid"])
            mid = hdrs.get("Message-ID") or ""
            if not mid or mid in log["replies"]:
                continue
            if any(hdrs.get(h) for h in _AUTOMATED_HEADERS) and not self_test:
                log["skipped"][c["key"]] = "automated mail (bulk headers), not a person's"
                continue
            if self_test:
                _, a = parseaddr(hdrs.get("From") or "")
                to = {"address": _clean(a), "name": "Sulaiman Khydyr", "source": "self-test"}
            else:
                to = _reply_target(hdrs, c["company"], own)
            if to is None:
                log["skipped"][c["key"]] = "no person to reply to in that thread"
                continue
            if not self_test and thread_replied_by(box, hdrs.get("thrid", ""), own):
                log["skipped"][c["key"]] = "you already replied in that thread"
                continue
            body = compose_follow_up(c["stage"], to, c["company"])
            subject = hdrs.get("Subject") or c["subject"]
            subject = subject if re.match(r"\s*re:", subject, re.I) else "Re: " + " ".join(subject.split())
            entry = {"key": c["key"], "company": c["company"], "stage": c["stage"], "to": to["address"], "name": to.get("name", ""),
                     "subject": subject, "body": body, "in_reply_to": mid, "original_uid": c["uid"], "original_subject": c["subject"]}
            say(f"  {c['company'][:30]}: {'plan reply' if dry_run else 'reply'} -> {to['address']}" + (f" ({to['name']})" if to.get("name") else "")
                + f"  [{c['stage']}: {c['subject'][:50]}]")
            if dry_run:
                done.append(entry)
                budget -= 1
                continue
            try:
                new_mid = send(to["address"], subject, body, None, in_reply_to=mid, references=hdrs.get("References") or None,
                               extra_headers={"X-Resume-Tailor-Test": "self-test"} if self_test else None)
            except SendBlocked as e:
                say(f"  sending stopped: {e}")
                break
            except Exception as e:
                log["skipped"][c["key"]] = f"reply failed: {str(e)[:120]}"
                say(f"  reply failed to {to['address']}: {str(e)[:120]}")
                continue
            entry.update({"sent_at": datetime.now(TZ).isoformat(timespec="seconds"), "message_id": new_mid, "thrid": hdrs.get("thrid", "")})
            log["replies"][mid] = entry
            save_log(out_dir, log)
            say(f"  replied: {c['company']} <{to['address']}> — {subject}")
            done.append(entry)
            budget -= 1
            time.sleep(SEND_PAUSE)
    finally:
        try:
            box.logout()
        except Exception:
            pass
    save_log(out_dir, log)
    return done


# ---------------------------------------------------------------------------
# Proving a send: read the sent mail back from the mailbox

def verify_delivery(message_id: str, expect: dict | None = None, timeout_s: int = 180) -> list[tuple[str, bool, str]]:
    """Find a message the account sent, by Message-ID, in the mailbox: a copy
    in Sent Mail (the server accepted it), a delivered copy in the Inbox
    when it was addressed to the applicant, the attachment, the threading
    headers, and what Gmail's own authentication check says. Returns
    (check, passed, detail) rows."""
    import email
    rows: list[tuple[str, bool, str]] = []
    expect = expect or {}
    box = _imap_box()
    deadline = time.time() + timeout_s
    hits: list[tuple[str, str]] = []  # (uid, labels)
    try:
        while time.time() < deadline and not hits:
            typ, data = box.uid("search", None, "HEADER", "Message-ID", message_id)
            uids = (data[0] or b"").split() if typ == "OK" else []
            for uid in uids:
                typ2, d2 = box.uid("fetch", uid.decode(), "(X-GM-LABELS)")
                labels = d2[0].decode(errors="replace") if typ2 == "OK" and d2 and isinstance(d2[0], bytes) else str(d2)
                hits.append((uid.decode(), labels))
            if not hits:
                time.sleep(10)
        rows.append(("found in the mailbox by Message-ID", bool(hits), f"{len(hits)} copy/copies" if hits else f"nothing after {timeout_s}s"))
        if not hits:
            return rows
        labels_all = " ".join(l for _, l in hits)
        rows.append(("a copy in Sent Mail (the server accepted it)", "\\Sent" in labels_all, labels_all[:120]))
        if expect.get("delivered_to"):
            rows.append(("a delivered copy in the Inbox", "\\Inbox" in labels_all, labels_all[:120]))
        uid = hits[0][0]
        typ, data = box.uid("fetch", uid, "(BODYSTRUCTURE BODY.PEEK[HEADER])")
        raw = b"".join(part[1] for part in data if isinstance(part, tuple))
        # The BODYSTRUCTURE rides in the first element of the fetch tuple, before the literal.
        meta = " ".join((part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0])) if isinstance(part, tuple)
                        else (part.decode(errors="replace") if isinstance(part, bytes) else str(part)) for part in data)
        msg = email.message_from_bytes(raw)
        rows.append(("Message-ID on the account's domain, not the laptop's", bool(re.search(r"@[a-z0-9.-]+\.(com|edu|org|net)>?$", message_id.strip(), re.I)), message_id))
        rows.append(("a Date header", bool(msg.get("Date")), msg.get("Date") or ""))
        if expect.get("to"):
            rows.append(("To is the intended address", expect["to"].lower() in (msg.get("To") or "").lower(), msg.get("To") or ""))
        if expect.get("attachment", True):
            rows.append(("the résumé PDF is attached", bool(re.search(r'"APPLICATION" "PDF"', meta, re.I)) and "resume.pdf" in meta.lower(), meta[:160]))
        if expect.get("in_reply_to"):
            rows.append(("In-Reply-To is the original's Message-ID", (msg.get("In-Reply-To") or "").strip() == expect["in_reply_to"].strip(), msg.get("In-Reply-To") or ""))
            rows.append(("References ends with the original's Message-ID", (msg.get("References") or "").strip().endswith(expect["in_reply_to"].strip()), (msg.get("References") or "")[-80:]))
            rows.append(("Subject starts with Re:", (msg.get("Subject") or "").lower().startswith("re:"), msg.get("Subject") or ""))
        auth = msg.get_all("Authentication-Results") or []
        joined = " ".join(auth)
        if auth:
            rows.append(("Gmail's authentication check (dkim/spf/dmarc)", "dkim=pass" in joined and "spf=pass" in joined, joined[:220]))
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return rows


# ---------------------------------------------------------------------------
# The unattended lane

def _now(stage: str, **extra) -> None:
    """The dashboard's now-line for this lane (current-outreach.json, the
    same shape the workers write)."""
    try:
        home = Path.home() / ".resume-tailor"
        home.mkdir(parents=True, exist_ok=True)
        data = {"stage": stage, "pid": os.getpid(), "worker": "outreach", "at": datetime.now(TZ).isoformat(timespec="seconds"),
                "entry_id": "", "company": extra.get("company", ""), "role": extra.get("role", "")}
        (home / "current-outreach.json").write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _heartbeat(out_dir: Path, **info) -> None:
    try:
        data = {"at": datetime.now(TZ).isoformat(timespec="seconds"), "epoch": time.time(), "pid": os.getpid(), **info}
        (out_dir / HEARTBEAT_NAME).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def watch(out_dir: str | Path, interval_min: float = 15.0, lookup_interval_min: float = 180.0, lookup_batch: int = 20,
          dry_run: bool = False, max_ticks: int | None = None) -> None:
    """The outreach lane: forever, every `interval_min` minutes, inside the
    sending window and with the switch on, reply to the people who wrote
    (follow-ups), then write to named recruiters from the cache, then — when
    answers.yaml → outreach.fallback_team is on — to team mailboxes; every
    `lookup_interval_min` minutes, at any hour, a bounded web lookup pass
    fills the address cache for `lookup_batch` more companies. Nothing in a
    tick waits on the web. A heartbeat file says when it last ran."""
    from .profile import Profile

    import threading

    out_dir = Path(out_dir)
    home = Path.home() / ".resume-tailor"
    home.mkdir(parents=True, exist_ok=True)
    (home / "outreach.pid").write_text(str(os.getpid()), encoding="utf-8")
    last_lookup = 0.0
    ticks = 0
    lookup_thread: threading.Thread | None = None

    def lookup_pass() -> None:
        # In its own thread: a web pass is slow and rate-limited, and the
        # send tick and the heartbeat must never wait on it.
        try:
            tally = lookup_all(out_dir, use_web=True, workers=2, limit=lookup_batch)
            print(f"[{time.strftime('%Y-%m-%d %H:%M')}] outreach lookup: {tally}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M')}] outreach lookup failed: {str(e)[:160]}", file=sys.stderr, flush=True)

    while True:
        ticks += 1
        stamp = time.strftime("%Y-%m-%d %H:%M")
        sent_n = replied_n = 0
        try:
            cfg = Profile.load().answers.get("outreach") or {}
            per_tick = int(cfg.get("per_tick", 5) or 5)
            enabled, why_off = outreach_enabled(Profile.load())
            window = in_window()
            if (window or dry_run) and (enabled or dry_run):
                _now("replying to recruiters who wrote")
                replied_n = len(run_follow_ups(out_dir, dry_run=dry_run, quiet=True))
                _now("writing to named recruiters")
                sent_n = len(run(out_dir, dry_run=dry_run, max_send=per_tick, people_only=True, quiet=True))
                if cfg.get("fallback_team", False) and sent_n < per_tick:
                    _now("writing to recruiting mailboxes")
                    sent_n += len(run(out_dir, dry_run=dry_run, max_send=per_tick - sent_n, quiet=True))
                print(f"[{stamp}] outreach: {'planned' if dry_run else 'sent'} {sent_n}, replies {replied_n}, "
                      f"today {sent_today(load_log(out_dir))}/{DAILY_CAP}", file=sys.stderr, flush=True)
            else:
                print(f"[{stamp}] outreach: idle ({'outside the window' if not window else why_off})", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[{stamp}] outreach tick failed: {str(e)[:160]}", file=sys.stderr, flush=True)
        looking = bool(lookup_thread and lookup_thread.is_alive())
        _heartbeat(out_dir, in_window=in_window(), sent=sent_n, replies=replied_n, ticks=ticks, lookup_running=looking)
        if lookup_batch and not looking and time.time() - last_lookup >= lookup_interval_min * 60:
            _now("looking up recruiting addresses")
            lookup_thread = threading.Thread(target=lookup_pass, daemon=True)
            lookup_thread.start()
            last_lookup = time.time()
        _now("idle")
        if max_ticks and ticks >= max_ticks:
            if lookup_thread:
                lookup_thread.join(timeout=5)
            return
        time.sleep(max(60, int(interval_min * 60)))


def heartbeat_age_min(out_dir: str | Path) -> float | None:
    try:
        data = json.loads((Path(out_dir) / HEARTBEAT_NAME).read_text(encoding="utf-8"))
        return (time.time() - float(data.get("epoch") or 0)) / 60
    except Exception:
        return None


def lookup_all(out_dir: str | Path, use_web: bool = True, refresh: bool = False, only: str | None = None,
               workers: int = 3, people: bool = False, limit: int | None = None) -> dict:
    """Fill the address cache for every company applied to that is still
    worth writing to, composing and sending nothing. Companies whose cache
    already holds an address are left alone unless `refresh`; an empty cache
    entry is always retried, since it may date from a pass without the web.
    With `people`, every company whose cache holds no named person is
    searched again, whatever its age, and what is found joins the cache.
    The slow part, the web searches, runs a few companies at a time."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .mailscan import load_results

    out_dir = Path(out_dir)
    log = load_log(out_dir)
    results = load_results(out_dir)
    comps = results.get("companies") or {}
    todo = []
    for cand in candidates(out_dir):
        key = cand["key"]
        if only and only.lower() not in (cand["company"].lower() + " " + key):
            continue
        if key in log["sent"]:
            continue
        if ((comps.get(key) or {}).get("stage") or "") in ("rejected", "oa", "interview", "offer"):
            continue
        cached = log["lookups"].get(key)
        if people:
            if cached and _has_person(cached.get("addresses") or [], cand["company"]):
                continue
        elif cached and not refresh and cached.get("addresses") and time.time() - float(cached.get("at") or 0) < 14 * 86400:
            continue
        todo.append(cand)
    if limit:
        todo = todo[:limit]  # the lane does a bounded pass every few hours rather than the whole list at once
    tally = {"companies": len(todo), "inbox": 0, "pages": 0, "web": 0, "people": 0, "none": 0, "failed": 0}

    def look(cand: dict) -> tuple[dict, list[dict], str]:
        found = from_inbox(results, cand["key"])
        how = "inbox" if found else ""
        if not found:
            found = from_pages(cand["url"], _company_site(cand, results))
            how = "pages" if found else ""
        if not found and use_web:
            found = _through_outages(lambda: from_web(cand["company"], cand["url"]))
            how = "web" if found else ""
        if use_web and not _has_person(found, cand["company"]):
            more = [f for f in _through_outages(lambda: from_web(cand["company"], cand["url"], people=True))
                    if f["address"] not in {g["address"] for g in found}]
            if more:
                found, how = found + more, "people"
        if people:  # what was known stays; a person found now goes ahead of it
            old = (log["lookups"].get(cand["key"]) or {}).get("addresses") or []
            found = found + [f for f in old if f["address"] not in {g["address"] for g in found}]
        return cand, found, how or "none"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(look, c): c for c in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cand, found, how = fut.result()
            except Exception as e:  # one company's lookup must not end the pass, and a lost one is not cached as "nothing"
                tally["failed"] += 1
                print(f"  [{n}/{len(todo)}] {futures[fut]['company'][:30]:30s} lookup failed: {str(e)[:100]}", file=sys.stderr, flush=True)
                continue
            found = sorted((_ranked(f, cand["company"]) for f in found), key=lambda x: x.get("rank", 3))
            log["lookups"][cand["key"]] = {"at": time.time(), "addresses": found}
            tally[how] += 1
            save_log(out_dir, log)
            where = (f"{found[0]['address']}" + (f"  {found[0]['name']}" if found[0].get("name") else "")
                     + f"  ({found[0]['source'][:60]})") if found else "nothing published"
            print(f"  [{n}/{len(todo)}] {cand['company'][:30]:30s} {where}", file=sys.stderr, flush=True)
    save_log(out_dir, log)
    return tally


def run_cli(args) -> int:
    out = Path(args.out)
    if args.action == "lookup":
        tally = lookup_all(out, use_web=not args.no_web, refresh=bool(getattr(args, "refresh", False)), only=args.only,
                           workers=int(getattr(args, "workers", 3) or 3), people=bool(getattr(args, "people", False)))
        print(f"looked up {tally['companies']} companies: inbox {tally['inbox']}, pages {tally['pages']}, "
              f"web {tally['web']}, people {tally['people']}, nothing {tally['none']}, lost to the connection {tally['failed']}",
              file=sys.stderr)
        log = load_log(out)
        have = sum(1 for v in log["lookups"].values() if v.get("addresses"))
        persons = sum(1 for v in log["lookups"].values() if _has_person(v.get("addresses") or []))
        print(f"cache now holds an address for {have} of {len(log['lookups'])} companies looked up, a named person for {persons}",
              file=sys.stderr)
        return 0
    if args.action in ("plan", "send", "audit"):
        dry = args.action != "send"
        done = run(out, dry_run=dry, max_send=args.max, force=args.force, use_web=bool(getattr(args, "web", False)), only=args.only,
                   people_only=bool(getattr(args, "people", False)), test_to=getattr(args, "to", None))
        for d in done:
            print(f"\n=== {d['company']} — {d['role']}\nTo: {d['name'] + ' ' if d.get('name') else ''}<{d['to']}>   rank {d.get('rank')}  ({d['source']})"
                  + (f"\nalso found: {', '.join(d['alternatives'])}" if d.get("alternatives") else "")
                  + f"\nSubject: {d['subject']}\n\n{d.get('body', '(sent)')}")
        print(f"\n{'would send' if dry else 'sent'}: {len(done)}  (plan written to {out / PLAN_NAME})", file=sys.stderr)
        log = load_log(out)
        skipped = {k: v for k, v in log["skipped"].items() if k not in log["sent"]}
        if skipped:
            from collections import Counter
            why = Counter(re.sub(r"^[^:]+@[^:]+: ", "", v)[:50] for v in skipped.values())
            print("left out: " + "; ".join(f"{n}× {k}" for k, n in why.most_common(8)), file=sys.stderr)
        return 0
    if args.action in ("follow-plan", "follow-send"):
        done = run_follow_ups(out, dry_run=(args.action == "follow-plan"), max_send=args.max, force=args.force, only=args.only,
                              self_test=bool(getattr(args, "self_test", False)))
        for d in done:
            print(f"\n=== {d['company']} ({d['stage']}) — in reply to: {d['original_subject'][:70]}\nTo: {d['name']} <{d['to']}>\n"
                  f"Subject: {d['subject']}\n\n{d['body']}")
        print(f"\n{'would reply' if args.action == 'follow-plan' else 'replied'}: {len(done)}", file=sys.stderr)
        return 0
    if args.action == "verify":
        log = load_log(out)
        mid = getattr(args, "message_id", None)
        expect: dict = {}
        if not mid:
            tests = sorted(log.get("tests", {}).items(), key=lambda kv: kv[1].get("sent_at") or "")
            replies = sorted(log.get("replies", {}).items(), key=lambda kv: kv[1].get("sent_at") or "")
            latest = max(tests[-1:] + replies[-1:], key=lambda kv: kv[1].get("sent_at") or "", default=None)
            if latest is None:
                print("nothing to verify: no test send or reply in the log", file=sys.stderr)
                return 1
            mid, entry = latest[1].get("message_id"), latest[1]
            expect = {"to": entry.get("delivered_to") or entry.get("to"), "delivered_to": entry.get("delivered_to") or (entry.get("to") if entry.get("in_reply_to") else None),
                      "attachment": "in_reply_to" not in entry, "in_reply_to": entry.get("in_reply_to")}
        rows = verify_delivery(mid, expect, timeout_s=int(getattr(args, "wait", 180) or 180))
        bad = 0
        for name, ok, detail in rows:
            bad += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<58} {detail[:90]}")
        print(f"\n{len(rows) - bad}/{len(rows)} checks passed for {mid}", file=sys.stderr)
        return 1 if bad else 0
    if args.action == "watch":
        watch(out, interval_min=float(args.interval), lookup_interval_min=float(args.lookup_interval), lookup_batch=int(args.lookup_batch),
              dry_run=bool(getattr(args, "dry_run", False)), max_ticks=(int(args.ticks) if getattr(args, "ticks", None) else None))
        return 0
    if args.action == "status":
        age = heartbeat_age_min(out)
        log = load_log(out)
        pid_file = Path.home() / ".resume-tailor" / "outreach.pid"
        alive = False
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            alive = True
        except Exception:
            pass
        enabled, why_off = outreach_enabled()
        print(f"lane: {'running' if alive else 'not running'}; last tick {f'{age:.0f} min ago' if age is not None else 'never'}; "
              f"window {'open' if in_window() else 'closed'}; switch {'on' if enabled else 'OFF (' + why_off + ')'}; "
              f"sent {len(log['sent'])} (today {sent_today(log)}/{DAILY_CAP}), replies {len(log.get('replies', {}))}, tests {len(log.get('tests', {}))}")
        within = getattr(args, "within", None)
        if within:
            ok = age is not None and age <= float(within)
            print("PASS" if ok else "FAIL", f"ticked within {within} min")
            return 0 if ok else 1
        return 0
    if args.action == "log":
        log = load_log(out)
        for k, s in sorted(log["sent"].items(), key=lambda kv: kv[1].get("sent_at") or ""):
            print(f"{s.get('sent_at','')[:16]}  {s['company'][:28]:28s} {s['to']:40s} {s['subject'][:50]}")
        print(f"sent: {len(log['sent'])}, today: {sent_today(log)}")
        return 0
    return 1
