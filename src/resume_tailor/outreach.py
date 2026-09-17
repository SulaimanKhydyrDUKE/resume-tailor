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
SEND_PAUSE = 20  # seconds between two e-mails: a person does not send fifteen in a second
NETWORK_WAITS = (30, 60, 120)  # seconds to wait after a connection failure before trying the same company again
OUTAGE_STOP = 2  # companies lost in a row to the connection before the run stops rather than walking the list
TZ = ZoneInfo("America/New_York")

SUBJECT = "{role}, Summer 2027 - quick hello"
BODY = """Hi {greeting},

This is Sulaiman, Duke CS and Math junior. I just applied for the {role} role and wanted to put a name to the application. {project}

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
                        r"hrsystem|orc|recruiting|recruitment|bounces?|replies?|mailer|mkt|news|links?|click|em|e)\.|"
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
    for w in m.groups():
        if w in NOT_A_NAME or TEAM_WORDS.search(w) or RECRUITING.search(w) or NOREPLY.search(w + "@") or SYSTEM_BOX.search(w + "@"):
            return ""
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
    return {"sent": {}, "lookups": {}, "skipped": {}}


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
            for section in ("sent", "lookups"):
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


def from_inbox(results: dict, key: str) -> list[dict]:
    co = (results.get("companies") or {}).get(key) or {}
    out = []
    for a in co.get("addresses") or []:
        addr = _clean(a.get("address") or "")
        subject = a.get("subject") or ""
        if not _writable(addr) or MECHANICAL_SUBJECT.search(subject) or not _about_company(addr, subject, co.get("company") or ""):
            continue
        person = _person(a.get("name") or "", co.get("company") or "")
        guess = "" if person else _personal_local(addr)
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
BROKER_HOST = re.compile(r"signalhire|rocketreach|zoominfo|contactout|apollo\.io|growjo|lusha|hunter\.io|kaspr|wiza|"
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


def find_addresses(cand: dict, results: dict, log: dict, use_web: bool = True) -> list[dict]:
    key = cand["key"]
    cached = log["lookups"].get(key)
    if cached and time.time() - float(cached.get("at") or 0) < 14 * 86400:
        found = list(cached.get("addresses") or [])
    else:
        found = search(cand, results, use_web=use_web)
        log["lookups"][key] = {"at": time.time(), "addresses": found}
    bounced = set(results.get("bounced") or [])
    own = (_smtp_creds()[0] or "").lower()
    seen: set[str] = set()
    out = []
    for f in found:
        f = _ranked(f, cand.get("company") or "")  # the cache holds the rank an earlier, looser pass gave
        a = f["address"]
        if a in seen or a in bounced or a == own or not _writable(a):
            continue  # the cache may hold what an earlier, looser filter let through
        if MECHANICAL_SUBJECT.search(str(f.get("source") or "")) or _cited_broker(str(f.get("source") or "")):
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
    if isinstance(e, (socket.gaierror, socket.timeout, TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected,
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
        use_web: bool = True, only: str | None = None, people_only: bool = False) -> list[dict]:
    from .mailscan import load_results
    from .profile import Profile

    out_dir = Path(out_dir)
    log = load_log(out_dir)
    log["skipped"] = {}  # this run's reasons only; a reason left from an earlier run once read as a live failure
    results = load_results(out_dir)
    profile = Profile.load()
    linkedin = str(profile.career.get("personal_information", {}).get("linkedin") or "").strip()
    if not dry_run and not force and not in_window():
        print("outside the sending window (weekdays 8:00-18:00 Eastern); use --force to send now", file=sys.stderr)
        return []
    budget = max(0, min(max_send, DAILY_CAP - sent_today(log))) if not force else max_send
    done: list[dict] = []
    outages = 0  # companies lost in a row to the connection

    def lost(key: str, company: str, e: NetworkDown) -> bool:
        """Record a company lost to the connection; True when the run should stop."""
        nonlocal outages
        outages += 1
        log["skipped"][key] = f"network down: {e}"
        save_log(out_dir, log)
        print(f"  {company}: the connection failed through every wait ({e}); left for the next run", file=sys.stderr, flush=True)
        if outages >= OUTAGE_STOP:
            print(f"  the connection has failed for {outages} companies in a row; stopping here so the rest of the list "
                  "is not marked skipped. Run again once the network is back.", file=sys.stderr, flush=True)
            return True
        return False

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
        if people_only and int(addrs[0].get("rank", 3)) > 1:
            log["skipped"][key] = f"no named recruiter found, only {addrs[0]['address']}"
            continue
        to = addrs[0]
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
                 "source": to.get("source", ""), "subject": subject, "body": body, "posting_id": cand["id"], "pdf": cand["pdf"],
                 "alternatives": [a["address"] for a in addrs[1:4]]}
        if dry_run:
            done.append(entry)
            budget -= 1
            continue
        try:
            mid = _through_outages(lambda: send(to["address"], subject, body, cand["pdf"]))
        except NetworkDown as e:
            if lost(key, cand["company"], e):
                break
            continue
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
        time.sleep(SEND_PAUSE)
    save_log(out_dir, log)
    return done


def lookup_all(out_dir: str | Path, use_web: bool = True, refresh: bool = False, only: str | None = None,
               workers: int = 3, people: bool = False) -> dict:
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
    tally = {"companies": len(todo), "inbox": 0, "pages": 0, "web": 0, "people": 0, "none": 0}

    def look(cand: dict) -> tuple[dict, list[dict], str]:
        found = from_inbox(results, cand["key"])
        how = "inbox" if found else ""
        if not found:
            found = from_pages(cand["url"], _company_site(cand, results))
            how = "pages" if found else ""
        if not found and use_web:
            found = from_web(cand["company"], cand["url"])
            how = "web" if found else ""
        if use_web and not _has_person(found, cand["company"]):
            more = [f for f in from_web(cand["company"], cand["url"], people=True) if f["address"] not in {g["address"] for g in found}]
            if more:
                found, how = found + more, "people"
        if people:  # what was known stays; a person found now goes ahead of it
            old = (log["lookups"].get(cand["key"]) or {}).get("addresses") or []
            found = found + [f for f in old if f["address"] not in {g["address"] for g in found}]
        return cand, found, how or "none"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(look, c) for c in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cand, found, how = fut.result()
            except Exception as e:  # one company's lookup must not end the pass
                print(f"  lookup failed: {str(e)[:100]}", file=sys.stderr, flush=True)
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
              f"web {tally['web']}, people {tally['people']}, nothing {tally['none']}", file=sys.stderr)
        log = load_log(out)
        have = sum(1 for v in log["lookups"].values() if v.get("addresses"))
        persons = sum(1 for v in log["lookups"].values() if _has_person(v.get("addresses") or []))
        print(f"cache now holds an address for {have} of {len(log['lookups'])} companies looked up, a named person for {persons}",
              file=sys.stderr)
        return 0
    if args.action in ("plan", "send"):
        done = run(out, dry_run=(args.action == "plan"), max_send=args.max, force=args.force, use_web=not args.no_web, only=args.only,
                   people_only=bool(getattr(args, "people", False)))
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
