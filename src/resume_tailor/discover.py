"""Discovery: the SimplifyJobs Summer2027-Internships list.

The repo's README table is generated from `.github/scripts/listings.json`, so
the JSON is the source of truth — one object per posting with company, title,
locations, term, category, an `active` flag, and the direct ATS URL. This module
fetches it, keeps the postings worth applying to for this profile, and hands
the rest of the pipeline QueueEntry objects.

"Worth applying to" is deliberately mechanical here: right term, software
category, a title that reads as an engineering internship, a US location, not
PhD-only, not already attempted, not a blacklisted company. Whether the resume
*fits* a posting is the tailoring stage's judgement, made from the posting
text — this stage only decides what gets read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ats
from .queue import QueueEntry, RunState

DEFAULT_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
)

# The other GitHub internship lists, kept as README tables rather than JSON:
# one row per posting with the company, the title, a location, and a link.
# `search.extra_sources` in answers.yaml replaces this list.
TABLE_SOURCES = [
    ("jobright-swe", "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-Internship/master/README.md"),
    ("jobright-ba", "https://raw.githubusercontent.com/jobright-ai/2026-Business-Analyst-Internship/master/README.md"),
    ("speedyapply", "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/README.md"),
    ("vanshb03", "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/main/README.md"),
]
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_LINK_MD = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_LINK_HTML = re.compile(r'href="(https?://[^"]+)"')
_TAGS = re.compile(r"<[^>]+>")

_US_STATE = re.compile(
    r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b"
)
_US_ALIASES = {
    "nyc", "sf", "la", "usa", "us", "united states", "remote", "remote in usa", "remote - usa",
    "remote (us)", "remote, usa", "bay area", "silicon valley", "dc", "washington dc", "washington, dc",
}
_NON_US = re.compile(
    r"\b(uk|united kingdom|london|canada|toronto|vancouver|montreal|ottawa|india|bangalore|bengaluru|"
    r"hyderabad|pune|singapore|ireland|dublin|germany|berlin|munich|france|paris|netherlands|amsterdam|"
    r"israel|tel aviv|japan|tokyo|china|shanghai|beijing|shenzhen|hangzhou|australia|sydney|melbourne|"
    r"poland|warsaw|spain|madrid|barcelona|sweden|stockholm|switzerland|zurich|mexico|brazil|"
    r"europe|emea|apac|latam)\b",
    re.I,
)

_TITLE_YES = re.compile(
    r"software|\bswe\b|developer|engineer|backend|back-end|platform|infrastructure|full.?stack|"
    r"front.?end|\bweb\b|cloud|devops|site reliability|\bsre\b|systems|programm",
    re.I,
)
# The analyst lists the user added: business, technology, systems, data
# analyst internships — never financial or quantitative ones (_TITLE_NO).
_TITLE_ANALYST = re.compile(r"business analy|technology analy|systems? analy|data analy|it analy|product analy|operations analy|analytics", re.I)
# The AI/ML/Data category's engineering-flavoured titles: the ones a record
# of LLM services, embedding retrieval and on-device inference speaks to.
# Pure research and quant titles stay out (_TITLE_NO).
_TITLE_AI = re.compile(r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|computer vision|\bnlp\b|\bllm|"
                       r"data scien|applied scien|research engineer|data engineer|mlops|generative", re.I)
# Titles the Software category itself vouches for: the list's curators put
# "Technology Intern", "Computer Science Intern" and "Application Development
# Intern" under Software, and the judges still decide the fit.
_TITLE_SOFT = re.compile(r"technolog|application|computer|\bit\b|mobile|python|java|automation|\bai\b|digital|"
                         r"r&d|research and development|^\s*(summer |\d{4} )?intern(ship)?\b", re.I)
# The Product category's own titles: product management and product interns.
_TITLE_PRODUCT = re.compile(r"product", re.I)
_TITLE_NO = re.compile(
    r"job listings?( page)?|careers? page|all (open )?(jobs|positions|roles)|open positions$|talent (community|network)|"
    r"hardware|electrical|mechanical|civil|chemical|aerospace|manufacturing|process engineer|"
    r"product specialist|program manager|marketing|sales|recruit|talent|"
    r"human resources|\bhr\b|finance|financial|accountant|data entry|quantitative|\bquant\b|"
    r"trader|trading|graphic|technician|it support|help ?desk|information technology|"
    r"network engineer|field engineer|test technician|\bux\b|ui/ux|ux/ui",
    re.I,
)


_SEASON = r"(?:summer|fall|autumn|spring|winter)"
_TERM_IN_TITLE = re.compile(
    r"\b(" + _SEASON + r"(?:\s*[/&+-]\s*" + _SEASON + r")*)\s*[-/]?\s*'?(?:20)?(2[5-9])\b", re.I)


_YEAR_FIRST = re.compile(r"\b(?:20)?(2[5-9])\s+(" + _SEASON + r")\b", re.I)
_SEASON_ONLY = re.compile(r"\b(fall|autumn|winter|spring)\b", re.I)


def other_season(title: str) -> str:
    """A season the title names with no year at all — "Winter Co-Op",
    "Spring Term Co-op" — when it never says summer. Empty when nothing."""
    if re.search(r"\bsummer\b", title or "", re.I):
        return ""
    m = _SEASON_ONLY.search(title or "")
    return ("Fall" if m and m.group(1).lower() == "autumn" else m.group(1).capitalize()) if m else ""


def title_terms(title: str) -> set[str]:
    """The terms a title names outright — {"Fall 2026"} for "SWE Intern -
    Fall 2026", both halves of "Summer/Fall 2027", nothing for a title that
    only says "2026 Intern". The list repos hard-code every row's term, so the
    title is the only place a wrong-term row shows itself."""
    out: set[str] = set()
    for seasons, yy in _TERM_IN_TITLE.findall(title or ""):
        for season in re.split(r"\s*[/&+-]\s*", seasons):
            season = season.lower()
            out.add(("Fall" if season == "autumn" else season.capitalize()) + " 20" + yy)
    for yy, season in _YEAR_FIRST.findall(title or ""):  # "2027 Spring Term Co-op"
        season = season.lower()
        out.add(("Fall" if season == "autumn" else season.capitalize()) + " 20" + yy)
    return out


DEFAULT_CATEGORIES = ["software", "analyst", "ai/ml", "data", "product"]


@dataclass
class Prefs:
    terms: list[str] = field(default_factory=lambda: ["Summer 2027"])
    title_blacklist: list[str] = field(default_factory=list)
    company_blacklist: list[str] = field(default_factory=list)
    location_blacklist: list[str] = field(default_factory=list)
    require_us: bool = True
    exclude_phd_only: bool = True
    apply_once_at_company: bool = True
    positions: list[str] = field(default_factory=list)
    jobright_account: bool = False  # the tool's browser is signed in to jobright.ai, so its links resolve
    # Listing categories worth reading (substrings of the source's category):
    # software, the analyst lists, AI/ML/Data and Product — the title gate
    # keeps out what no category makes ours (quant, hardware, sales, HR) and
    # the judges still decide the fit.
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))

    @classmethod
    def from_profile(cls, profile) -> "Prefs":
        from .profile import apply_once_at_company as _apply_once

        s_all = profile.answers
        s = s_all.get("search", {}) or {}
        return cls(
            positions=list(s.get("positions") or []),
            categories=[str(c).lower() for c in (s.get("categories") or DEFAULT_CATEGORIES)],
            terms=list(s.get("terms") or ["Summer 2027"]),
            title_blacklist=list(s.get("title_blacklist") or []),
            company_blacklist=list(s.get("company_blacklist") or []),
            location_blacklist=list(s.get("location_blacklist") or []),
            require_us=bool(s.get("require_us", True)),
            exclude_phd_only=bool(s.get("exclude_phd_only", True)),
            apply_once_at_company=_apply_once(s_all),
            jobright_account=bool(s.get("jobright_account", False)),
        )


# --- fetching ----------------------------------------------------------------

def fetch(url: str = DEFAULT_URL, etag: str | None = None, timeout: float = 60.0) -> tuple[list[dict] | None, str | None]:
    """Returns (listings, etag). listings is None when the server says nothing
    has changed since `etag` — the file is 11 MB and the repo updates a few
    times a day, so most polls should cost one 304."""
    req = urllib.request.Request(url, headers={"User-Agent": "resume-tailor/0.1"})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def _cell_text(cell: str) -> str:
    """A table cell as plain words: markdown links and HTML stripped, bold
    markers and flag emoji dropped."""
    s = _LINK_MD.sub(r"\1", cell)
    s = _TAGS.sub(" ", s)
    s = re.sub(r"[*_`]+", "", s)
    s = re.sub(r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", s)
    return " ".join(s.split()).strip(" -")


def _cell_links(cell: str) -> list[str]:
    return _LINK_MD.findall(cell) and [u for _, u in _LINK_MD.findall(cell)] or _LINK_HTML.findall(cell)


def _date_epoch(text: str) -> int:
    """'Sep 03' → that day this year (or last year if it lies ahead of today);
    '3d' → three days ago; anything else → now."""
    now = time.time()
    text = (text or "").strip()
    m = re.fullmatch(r"(\d+)\s*d", text, re.I)
    if m:
        return int(now - int(m.group(1)) * 86400)
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", text)
    if m and m.group(1).lower() in _MONTHS:
        year = int(m.group(3)) if m.group(3) else time.localtime(now).tm_year
        try:
            stamp = time.mktime((year, _MONTHS[m.group(1).lower()], int(m.group(2)), 12, 0, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return int(now)
        if stamp > now + 86400 and not m.group(3):
            stamp -= 365 * 86400
        return int(stamp)
    return int(now)


def parse_table(markdown: str, source: str) -> list[dict]:
    """Postings from a README table: the company from the first cell, the
    title from the second, a location from the third, the posting link from
    the first link that is not the company's own site, and the date from the
    last cell. A "↳" company cell repeats the row above it."""
    out: list[dict] = []
    last_company = ""
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= set("-: ") or cells[0].lower() in ("company",):
            continue
        company = _cell_text(cells[0])
        if company in ("↳", "") or company.startswith("↳"):
            company = last_company
        last_company = company
        title = _cell_text(cells[1])
        location = _cell_text(cells[2])
        company_sites = set(_cell_links(cells[0]))
        links = [u for c in cells[1:] for u in _cell_links(c) if u not in company_sites]
        links = [u for u in links if not re.search(r"imgur\.com|discord|speedyapply\.com$|jobright\.ai/?$", u)]
        if not company or not title or not links:
            continue
        url = links[0]
        out.append({
            "id": "gh:" + hashlib.sha1(url.split("?utm")[0].encode("utf-8")).hexdigest()[:16],
            "url": url, "company_name": company, "title": title,
            "locations": [location] if location else [], "date_posted": _date_epoch(cells[-1]),
            "source": source, "active": True, "is_visible": True,
            "terms": sorted(title_terms(title)) or ["Summer 2027"], "degrees": [],
            "category": "Software Engineering" if source != "jobright-ba" else "Business Analyst",
        })
    return out


def fetch_tables(sources: list[tuple[str, str]] | None = None, timeout: float = 30.0) -> list[dict]:
    """Every table source, fetched fresh (they are small); a source that fails
    is skipped, never fatal."""
    out: list[dict] = []
    for name, url in (sources if sources is not None else TABLE_SOURCES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "resume-tailor/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out.extend(parse_table(resp.read().decode("utf-8", errors="replace"), name))
        except Exception:
            continue
    return out


def load_cached(out_dir: Path) -> tuple[dict, list[dict] | None]:
    """The last fetch's metadata and listings. A file another worker is
    replacing this instant, or one left half-written, reads as no cache."""
    meta_path, cache_path = out_dir / "discover-state.json", out_dir / "listings-cache.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    except (json.JSONDecodeError, OSError):
        meta = {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else None
    except (json.JSONDecodeError, OSError):
        cache = None
    return meta, cache


def _write_atomic(path: Path, text: str) -> None:
    """Written whole or not at all: four workers refresh the same cache, and a
    reader must never see one of them halfway through."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def refresh(out_dir: str | Path, url: str = DEFAULT_URL,
            table_sources: list[tuple[str, str]] | None = None) -> tuple[list[dict], bool]:
    """Fetch if changed, else reuse the cached copy; the README-table sources
    are re-read every time and merged in (a posting already known from the
    JSON feed keeps that record). Returns (listings, changed)."""
    import fcntl

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # One worker fetches at a time; the next finds the fresh etag and gets a 304.
    with open(out_dir / "discover.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _refresh_locked(out_dir, url, table_sources)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh_locked(out_dir: Path, url: str, table_sources: list[tuple[str, str]] | None) -> tuple[list[dict], bool]:
    meta, cache = load_cached(out_dir)
    cached_simplify = [l for l in (cache or []) if not str(l.get("id", "")).startswith("gh:")]
    listings, etag = fetch(url, etag=meta.get("etag") if cache is not None else None)
    changed = listings is not None
    if listings is None:
        listings = cached_simplify
    tables = fetch_tables(table_sources)
    known = {(l.get("url") or "").split("?")[0] for l in listings}
    extra = [l for l in tables if l["url"].split("?")[0] not in known]
    # A jobright row links to jobright.ai, which shows the employer's page
    # only to a signed-in member; when the same company and role are listed
    # elsewhere with a real link, that copy wins and the jobright row goes.
    from .queue import company_key

    def role_key(l: dict) -> tuple[str, str]:
        return (company_key(l.get("company_name") or ""), re.sub(r"[^a-z0-9]+", " ", (l.get("title") or "").lower()).strip()[:60])
    direct = {role_key(l) for l in listings + extra if "jobright.ai/" not in (l.get("url") or "")}
    extra = [l for l in extra if "jobright.ai/" not in l["url"] or role_key(l) not in direct]
    if not tables:  # every source down: keep what the cache had
        extra = [l for l in (cache or []) if str(l.get("id", "")).startswith("gh:")]
    before = {l.get("id") for l in (cache or [])}
    merged = listings + extra
    changed = changed or any(l["id"] not in before for l in extra)
    _write_atomic(out_dir / "listings-cache.json", json.dumps(merged))
    meta.update({"etag": etag, "last_fetch": int(time.time()), "count": len(merged), "source": url,
                 "table_sources": {n: sum(1 for l in tables if l["source"] == n) for n, _ in (table_sources if table_sources is not None else TABLE_SOURCES)}})
    _write_atomic(out_dir / "discover-state.json", json.dumps(meta, indent=2))
    return merged, changed


# --- filtering ---------------------------------------------------------------

def is_us(locations: list[str]) -> bool | None:
    """True if any location is in the US, False if all are elsewhere, None if
    the listing does not say — which is treated as worth a look."""
    if not locations:
        return None
    verdicts = []
    for loc in locations:
        low = loc.strip().lower()
        if _NON_US.search(low):
            verdicts.append(False)
        elif _US_STATE.search(loc) or low in _US_ALIASES or "usa" in low or "united states" in low or "remote" in low:
            verdicts.append(True)
    if True in verdicts:
        return True
    if verdicts and all(v is False for v in verdicts):
        return False
    return None


def evaluate(listing: dict, prefs: Prefs) -> str:
    """Empty string when the listing is worth applying to; otherwise why not."""
    if not listing.get("active", True):
        return "inactive"
    if not listing.get("is_visible", True):
        return "hidden"
    if prefs.terms and not set(listing.get("terms") or []) & set(prefs.terms):
        return "other term"
    named = title_terms(listing.get("title") or "")
    if prefs.terms and named and not named & set(prefs.terms):
        return "other term: " + ", ".join(sorted(named))
    season = other_season(listing.get("title") or "")
    if prefs.terms and season and not any(t.lower().startswith(season.lower()) for t in prefs.terms):
        return "other term: " + season
    category = (listing.get("category") or "").lower()
    if not any(c in category for c in prefs.categories):
        return f"category: {listing.get('category') or '?'}"
    title = listing.get("title") or ""
    if listing.get("source") and not re.search(r"intern|co-?op", title, re.I):
        return "title: not an internship"  # the table lists carry new-grad rows too
    if _TITLE_NO.search(title):
        return "title: not an engineering role"
    ai_category = "ai" in category or "ml" in category or "data" in category
    if not (_TITLE_YES.search(title) or ("analyst" in category and _TITLE_ANALYST.search(title))
            or ai_category
            or ("software" in category and _TITLE_SOFT.search(title))
            or ("product" in category and _TITLE_PRODUCT.search(title))):
        return "title: not software"
    if any(b.lower() in title.lower() for b in prefs.title_blacklist):
        return "title blacklist"
    company = (listing.get("company_name") or "").lower()
    if any(b.lower() in company for b in prefs.company_blacklist):
        return "company blacklist"
    locations = listing.get("locations") or []
    if prefs.require_us and is_us(locations) is False:
        return "outside US"
    if any(b.lower() in loc.lower() for b in prefs.location_blacklist for loc in locations):
        return "location blacklist"
    degrees = [d.lower() for d in (listing.get("degrees") or [])]
    if prefs.exclude_phd_only and degrees and all("phd" in d or "doctor" in d for d in degrees):
        return "PhD-only"
    if not (listing.get("url") or "").startswith("http"):
        return "no url"
    if "jobright.ai/" in listing["url"] and not prefs.jobright_account:
        return "link goes through jobright.ai (needs a jobright sign-in)"
    return ""


def _posted_day(listing: dict) -> int:
    return int(listing.get("date_posted") or 0) // 86400


def _fit_rank(listing: dict) -> int:
    title = listing.get("title") or ""
    category = (listing.get("category") or "").lower()
    if _TITLE_YES.search(title) or "software" in category:
        return 0
    if _TITLE_ANALYST.search(title) or "analyst" in category:
        return 1
    if "product" in category:
        return 3
    return 2


def _rank(url: str) -> int:
    kind = ats.host_kind(url)
    if kind in ats.DIRECT_FORM:
        return 0
    if kind in ats.NEEDS_ACCOUNT:
        return 2
    return 1


_GENERIC = {"engineer", "engineering", "intern", "internship", "developer", "co-op", "coop", "summer", "2027"}


def _position_keywords(positions: list[str]) -> list[str]:
    """'Backend Engineer Intern' -> 'backend': the word that tells positions
    apart, in the order the user listed them."""
    out = []
    for pos in positions:
        words = [w for w in re.findall(r"[a-z0-9-]+", pos.lower()) if w not in _GENERIC]
        out.append(" ".join(words) if words else pos.lower())
    return out


_UNSPECIFIC = {"software", "software engineer", "swe"}


def position_score(title: str, positions: list[str]) -> int:
    """Index of the best preferred position the title matches; lower is
    better; a title matching none scores past the end of the list.

    A generic preference like "Software Engineer Intern" matches every title,
    so it only counts when nothing specific does — otherwise listing it first
    would make Backend and Mobile tie, and the choice would fall to the date."""
    t = title.lower().replace("full stack", "full-stack").replace("fullstack", "full-stack")
    keys = _position_keywords(positions)
    matches = [i for i, k in enumerate(keys) if k and k in t]
    specific = [i for i in matches if keys[i] not in _UNSPECIFIC]
    if specific:
        return min(specific)
    # Generic-only matches rank after every specific position, wherever the
    # generic entry sits in the user's list; no match at all ranks last.
    return len(keys) + min(matches) if matches else 2 * len(keys)


def one_per_company(listings: list[dict], prefs: Prefs) -> list[dict]:
    """With apply-once-per-company on, tailoring four Verkada postings to
    apply to one is three wasted resumes. Keep the best-fitting title per
    company — by the user's position order, newest on ties — and let the
    others wait for a later pass, where the company rule will settle them."""
    best: dict[str, dict] = {}
    for l in listings:
        key = (l.get("company_name") or "").strip().lower() or l.get("id", "")
        score = (position_score(l.get("title") or "", prefs.positions), -(l.get("date_posted") or 0))
        cur = best.get(key)
        if cur is None or score < cur[0]:
            best[key] = (score, l)
    return [v[1] for v in best.values()]


def to_entry(listing: dict) -> QueueEntry:
    url = listing["url"]
    return QueueEntry(
        id=listing.get("id") or url, url=url, apply_url=ats.apply_url_for(url),
        company_hint=listing.get("company_name") or "", title=listing.get("title") or "",
        location=", ".join(str(l) for l in (listing.get("locations") or [])),
    )


RETRY_CAP = 3
LOGIN_RETRY_CAP = 2


def shard_of(listing: dict, workers: int) -> int:
    """Which of `workers` parallel loops a listing belongs to. Every posting
    of a company lands on the same worker, so apply-once-per-company holds
    across workers without any of them asking the others."""
    from .queue import company_key

    key = company_key(listing.get("company_name") or "") or str(listing.get("id") or listing.get("url") or "")
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % max(1, workers)


def _worn_out(rec: dict, inbox: bool | None = None) -> bool:
    """Three attempts that all ended in needs_review or blocked, or two that
    ended at a login wall — none of them a hand re-queue. A wall only a
    person can pass (a sign-in, an e-mailed link) costs minutes of browser
    time per retry and does not change on its own; `review` re-queues it.

    A wall that only wants the applicant's inbox is worn out for exactly as
    long as the inbox is unconfigured (`inbox`, read once by the caller): the
    moment RESUME_TAILOR_IMAP_PASSWORD is set, every such posting is worth a
    try, whatever its count says — the passes it sat out were not attempts."""
    from . import mailbox

    if (rec.get("detail") or "").startswith("re-queued"):
        return False
    attempts = int(rec.get("attempts") or 0)
    if rec.get("status") == "needs_login":
        if mailbox.waiting_for_inbox(rec):
            return not (mailbox.configured() if inbox is None else inbox)
        return attempts >= LOGIN_RETRY_CAP
    return rec.get("status") in ("needs_review", "blocked") and attempts >= RETRY_CAP


def select(listings: list[dict], prefs: Prefs, state: RunState | None = None,
           limit: int | None = None, shard: tuple[int, int] | None = None,
           retries_first: bool = False) -> tuple[list[QueueEntry], dict[str, int]]:
    """Listings worth attempting, in the order to attempt them: direct-form ATS
    hosts first, then unknown hosts, then the ones that will want an account —
    newest first within each. Also returns a count of why the rest were left
    out, so a quiet run can be told apart from a broken filter.

    `shard` = (k, n) keeps only the k-th of n workers' companies (see
    shard_of); `retries_first` deals every retry before any new posting —
    for a first pass after a fix to the form layer."""
    from . import mailbox
    from .batch import RETRYABLE
    from .queue import company_key

    excluded: dict[str, int] = {}
    kept: list[dict] = []
    # The same role posted twice (one listing per location, say) gets one
    # verdict, not one tailoring per listing.
    import html

    def role_key_of(company: str, title: str) -> tuple[str, str]:
        return (company_key(company), " ".join(html.unescape(title or "").lower().split()))

    judged_roles: set[tuple[str, str]] = set()
    # A company whose postings the judges have already held twice: their
    # verdict is about depth of fit with that company's work, which does not
    # change from one of its titles to the next.
    holds_at: dict[str, int] = {}
    # The same link can be listed twice with two ids (one row per location):
    # one attempt covers both, whatever the per-company rule says.
    attempted_urls: set[str] = set()
    if state is not None:
        by_listing_id = {l.get("id") or l.get("url"): l for l in listings}
        attempted_urls = {(by_listing_id.get(rid) or {}).get("url") or "" for rid, rec in state.done.items()
                          if rec.get("status") not in RETRYABLE} - {""}
        for rec_id, rec in state.done.items():
            if rec.get("status") in RETRYABLE:
                continue
            if rec.get("status") == "skipped" and (rec.get("detail") or "").startswith("posting text too short"):
                continue  # a page that showed nothing is no verdict on the role; another link to it may work
            # The listing's own title, when the record's id is a listing id —
            # the title the model read off the posting can differ by a word.
            src = by_listing_id.get(rec_id) or {}
            company = src.get("company_name") or rec.get("company") or ""
            title = src.get("title") or rec.get("role") or ""
            if company and title:
                judged_roles.add(role_key_of(company, title))
            if rec.get("role"):
                judged_roles.add(role_key_of(rec.get("company") or company, rec["role"]))
            if rec.get("status") == "fit_rejected" and company:
                holds_at[company_key(company)] = holds_at.get(company_key(company), 0) + 1
    for l in listings:
        reason = evaluate(l, prefs)
        if not reason and state is not None:
            role_key = role_key_of(l.get("company_name") or "", l.get("title") or "")
            if state.already_attempted(l.get("id") or l.get("url", ""), RETRYABLE):
                reason = "already attempted"
            elif l.get("url") in attempted_urls and (l.get("id") or l.get("url")) not in state.done:
                reason = "same link already attempted"
            elif prefs.apply_once_at_company and state.has_applied(l.get("company_name") or ""):
                reason = "already applied at company"
            elif prefs.apply_once_at_company and role_key in judged_roles:
                reason = "same role already judged"
            elif prefs.apply_once_at_company and holds_at.get(role_key[0], 0) >= 2:
                reason = "company held twice by the judges"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            kept.append(l)
    if shard is not None:
        k, n = shard
        before = len(kept)
        kept = [l for l in kept if shard_of(l, n) == k]
        if before - len(kept):
            excluded["other workers' companies"] = before - len(kept)
    if prefs.apply_once_at_company:
        before = len(kept)
        kept = one_per_company(kept, prefs)
        if before - len(kept):
            excluded["same company, later pass"] = before - len(kept)
    # Order of attempt: postings a dry run already filled completely (resume
    # and verdicts cached — the cheapest, surest submissions) first; then
    # never-attempted ones, so retries of review-needed ones don't crowd new
    # postings out of a capped pass; then the retries.
    attempted = state.done if state is not None else {}

    def stage(l: dict) -> int:
        prior = attempted.get(l.get("id") or l.get("url"))
        if prior is None:
            return 1
        return 0 if prior.get("status") == "ready_not_submitted" else 2

    # Within a stage, the newest postings first, by the day they went up:
    # an application in a posting's first days is read, one in its third
    # week often is not. Within a day, the roles the user is actually after
    # first (software before analyst before AI/ML/Data before product), then
    # the hosts with a direct form.
    kept.sort(key=lambda l: (stage(l), -_posted_day(l), _fit_rank(l), _rank(l["url"]), -(l.get("date_posted") or 0)))
    # Retries are cheap (resume and verdicts cached) and usually follow a fix
    # to the form layer, so they are dealt in — one after every three new
    # postings — rather than left behind the whole never-attempted pool.
    ready = [l for l in kept if stage(l) == 0]
    new = [l for l in kept if stage(l) == 1]
    # A posting that has needed review three times running is waiting for a
    # person (a pledge to sign, a transcript, an Apply that leads nowhere),
    # not for another pass; it stays on the dashboard and out of the deal.
    # Errors and login walls keep retrying: a network blip or a login by
    # the user changes them without any code change.
    inbox = mailbox.configured()  # once per pass, not once per record: it reads the env file
    retries = [l for l in kept if stage(l) == 2 and not _worn_out(attempted.get(l.get("id") or l.get("url")) or {}, inbox)]
    # The least-tried first: a posting never retried since the last fix to
    # the form layer goes before one that just failed again a pass ago.
    retries.sort(key=lambda l: int((attempted.get(l.get("id") or l.get("url")) or {}).get("attempts") or 0))
    for l in kept:
        rec = attempted.get(l.get("id") or l.get("url")) or {}
        if stage(l) == 2 and _worn_out(rec, inbox):
            why = ("waiting for the inbox (set RESUME_TAILOR_IMAP_PASSWORD)" if mailbox.waiting_for_inbox(rec)
                   else "needs review, three attempts")
            excluded[why] = excluded.get(why, 0) + 1
    kept = ready
    if retries_first:
        kept += retries
        retries = []
    while new or retries:
        kept += new[:3]
        del new[:3]
        if retries:
            kept.append(retries.pop(0))
    if limit is not None:
        kept = kept[:limit]
    return [to_entry(l) for l in kept], excluded


def describe(entry: QueueEntry, listing: dict | None = None) -> str:
    kind = ats.host_kind(entry.url)
    tag = "form" if kind in ats.DIRECT_FORM else ("account" if kind in ats.NEEDS_ACCOUNT else "?")
    when = ""
    if listing and listing.get("date_posted"):
        when = time.strftime("%m-%d", time.localtime(listing["date_posted"]))
    loc = ", ".join((listing or {}).get("locations") or [])[:28]
    return f"[{tag:7}] {when:5} {entry.company_hint[:24]:24} {entry.title[:44]:44} {loc}"
