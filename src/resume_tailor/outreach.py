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

`resume-tailor outreach lookup` fills the address cache for every company
first — inbox, then the posting's pages, then a web search whose every
address is checked against the page it cites — so a send never waits on
searches and the cache can be read before anything goes out.
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
                        r"university|campus|people|jobs?|noreply|no-reply|system|portal|candidates?|applications?|program|"
                        r"internships?|engineering|group|inc|llc|corp|company)\b", re.I)


# The applicant-tracking system's own senders and HR service desks: an
# account-verification sender, an HR support line, a servicing mailbox.
SYSTEM_BOX = re.compile(r"workday|oracle|icims|greenhouse|lever\.co|ashby|smartrecruiters|taleo|"
                        r"successfactors|brassring|hr[._-]?support|hrsupport|servicing|operations|seeyourself|default|system|^contact@|^hello@|"
                        r"wotc|^hr@|^jobs-|candidate[._-]?(care|experience|support)", re.I)
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
                        r"ziprecruiter\.com$|linkedin\.com$", re.I)
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
    if RELAY_HOST.search(host) or TOKEN_LOCAL.search(local):
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
        rank = 0 if (person and a.get("source") == "reply-to") else 1 if person else 2 if RECRUITING.search(addr) else 3
        out.append({"address": addr, "name": a.get("name") or "", "source": "inbox: " + (a.get("subject") or a.get("source") or ""), "rank": rank})
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
    candidates_ = [m.group(0) for m in EMAIL.finditer(html)] + re.findall(r"mailto:([^\"'?#<>\s]+)", html, re.I)
    for raw in candidates_:
        addr = _clean(raw)
        if not EMAIL.fullmatch(addr) or not _writable(addr) or addr.endswith((".png", ".jpg", ".gif", ".svg")) or "example.com" in addr:
            continue
        at = text.find(addr)
        around = text[max(0, at - 160): at + 80] if at >= 0 else ""
        if campus_page or RECRUITING.search(addr.split("@")[0]) or RECRUITING.search(around):
            if not any(f["address"] == addr for f in found):
                found.append({"address": addr, "name": "", "source": "page: " + page[:80], "rank": 2})


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
                         r"emailfinder|findymail|datanyze|crunchbase|theorg\.com", re.I)


def _broker_page(url: str) -> bool:
    return bool(BROKER_HOST.search(urlsplit(url).netloc or ""))


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


def from_web(company: str, url: str) -> list[dict]:
    """A web search (RESUME_TAILOR_SEARCH_MODEL, gpt-4o by default) for
    the company's campus or university recruiting addresses printed on a
    page it can cite. Each address is then looked for on that page: printed
    there, it is used; absent from a page that loads, it is dropped; on a
    page that will not load, it is kept only when its domain is the
    company's own, and at the lowest rank. A guess from a naming pattern is
    reported as such and never used."""
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
            rank = (1 if _person(name, company) else 2) if printed else 3
            out.append({"address": addr, "name": name, "title": str(a.get("title") or "")[:60],
                        "source": ("web: " if printed else "web, page unreachable: ") + page[:100], "rank": rank})
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
        found = from_inbox(results, key) + from_pages(cand["url"], _company_site(cand, results))
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
    client = OpenAI()
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


def compose(cand: dict, to: dict, profile_linkedin: str, resume_text: str) -> tuple[str, str]:
    name = (to.get("name") or "").strip()
    first = name.split()[0] if _person(name, cand.get("company") or "") else ""
    greeting = first if first else f"{cand['company']} recruiting team"
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
    log["skipped"] = {}  # this run's reasons only; a reason left from an earlier run once read as a live failure
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


def lookup_all(out_dir: str | Path, use_web: bool = True, refresh: bool = False, only: str | None = None,
               workers: int = 3) -> dict:
    """Fill the address cache for every company applied to that is still
    worth writing to, composing and sending nothing. Companies whose cache
    already holds an address are left alone unless `refresh`; an empty cache
    entry is always retried, since it may date from a pass without the web.
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
        if cached and not refresh and cached.get("addresses") and time.time() - float(cached.get("at") or 0) < 14 * 86400:
            continue
        todo.append(cand)
    tally = {"companies": len(todo), "inbox": 0, "pages": 0, "web": 0, "none": 0}

    def look(cand: dict) -> tuple[dict, list[dict], str]:
        found = from_inbox(results, cand["key"])
        how = "inbox" if found else ""
        if not found:
            found = from_pages(cand["url"], _company_site(cand, results))
            how = "pages" if found else ""
        if not found and use_web:
            found = from_web(cand["company"], cand["url"])
            how = "web" if found else ""
        return cand, found, how or "none"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(look, c) for c in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cand, found, how = fut.result()
            except Exception as e:  # one company's lookup must not end the pass
                print(f"  lookup failed: {str(e)[:100]}", file=sys.stderr, flush=True)
                continue
            found = sorted(found, key=lambda x: x.get("rank", 3))
            log["lookups"][cand["key"]] = {"at": time.time(), "addresses": found}
            tally[how] += 1
            save_log(out_dir, log)
            where = f"{found[0]['address']}  ({found[0]['source'][:60]})" if found else "nothing published"
            print(f"  [{n}/{len(todo)}] {cand['company'][:30]:30s} {where}", file=sys.stderr, flush=True)
    save_log(out_dir, log)
    return tally


def run_cli(args) -> int:
    out = Path(args.out)
    if args.action == "lookup":
        tally = lookup_all(out, use_web=not args.no_web, refresh=bool(getattr(args, "refresh", False)), only=args.only,
                           workers=int(getattr(args, "workers", 3) or 3))
        print(f"looked up {tally['companies']} companies: inbox {tally['inbox']}, pages {tally['pages']}, "
              f"web {tally['web']}, nothing {tally['none']}", file=sys.stderr)
        log = load_log(out)
        have = sum(1 for v in log["lookups"].values() if v.get("addresses"))
        print(f"cache now holds an address for {have} of {len(log['lookups'])} companies looked up", file=sys.stderr)
        return 0
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
