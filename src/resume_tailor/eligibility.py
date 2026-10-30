"""Deterministic reads of a posting before any model call: the hard
eligibility bars, and the signals of a company too small to apply to.

The two match judges decide fit with model calls. Two kinds of verdict do
not need them, and the sibling kit (sikai1228/internship-autopilot-kit)
settles both by grep: a posting closed to the applicant by an enumerated
bar — "must be a U.S. citizen", "an active security clearance is required",
"graduating between December 2026 and June 2027" — and a company that is a
seed-stage team of four. Here they cost nothing and run first, so the
judges, the résumé and the form are spent only on postings that are open.

Three rules carried over from the kit, each paid for there:

- A bar is a status the applicant must HOLD, never a process the employer
  can RUN: ITAR "must be a U.S. person" bars a visa holder; EAR "a deemed
  export licence may be required" does not. A rule fires only on the
  wording of a requirement ("must", "required", "only") and never on a
  softened one ("may require", "preferred").
- A bar is relative to the applicant's own facts, read from answers.yaml
  (citizenship status, whether sponsorship is needed, graduation date,
  degree level): "OPT holders are not eligible" bars nobody who holds a
  green card. A fact the bank does not state makes the rule silent, and the
  judges decide.
- Patterns are anchored and case-insensitive, and the text is never split
  on "." before matching: "U.S. person" straddles a period, and "ITAR"
  sits inside "mil-ITAR-y" on every form's veteran question.

`fetch_text` reads a posting from its vendor's public API (Greenhouse,
Lever, Ashby, Workable, SmartRecruiters) so the agent driver's handoff and
the queue sweep can judge a posting without opening a browser; the loop
itself scans the page text it already read.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import ats


@dataclass
class Facts:
    citizen: bool | None = None             # a U.S. citizen
    permanent_resident: bool | None = None  # a lawful permanent resident (green card)
    needs_sponsorship: bool | None = None   # needs employer visa sponsorship
    grad_year: int | None = None
    grad_month: int | None = None
    degree_level: str | None = None         # "bachelor" | "master" | "phd"


_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}
_SEASON_MONTH = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}


def facts_from_profile(profile) -> Facts:
    """The applicant's facts the bars are judged against, from answers.yaml
    (work_authorization, education) with career.yaml's education as the
    fallback for the degree and the graduation year."""
    f = Facts()
    try:
        flat = profile.flat_answers()
    except Exception:
        flat = {}
    status = str(flat.get("work_authorization.citizenship_status") or "").lower()
    if re.search(r"permanent resident|green card", status):
        f.permanent_resident, f.citizen = True, False
    elif re.search(r"\bcitizen\b", status) and not re.search(r"non-?citizen|not a citizen", status):
        f.citizen, f.permanent_resident = True, False
    needs = str(flat.get("work_authorization.requires_us_sponsorship") or "").strip().lower()
    if needs in ("yes", "no"):
        f.needs_sponsorship = needs == "yes"
    for key in ("education.graduation_date", "education.graduation_month_and_year", "education.expected_graduation", "education.graduation_year"):
        v = str(flat.get(key) or "")
        m = re.search(r"\b(20\d\d)\b", v)
        if m:
            f.grad_year = int(m.group(1))
            mm = re.search(r"\b(" + "|".join(_MONTHS) + r")\b", v.lower())
            f.grad_month = _MONTHS[mm.group(1)] if mm else None
            break
    lvl = str(flat.get("education.degree_level") or "").lower()
    if not lvl or f.grad_year is None:
        try:
            edu = (profile.career.get("education_details") or [{}])[0] or {}
        except Exception:
            edu = {}
        lvl = lvl or str(edu.get("education_level") or edu.get("degree") or "").lower()
        if f.grad_year is None:
            m = re.search(r"\b(20\d\d)\b", str(edu.get("year_of_completion") or edu.get("graduation_year") or edu.get("end_date") or ""))
            if m:
                f.grad_year = int(m.group(1))
    if re.search(r"ph\.?d|doctor", lvl):
        f.degree_level = "phd"
    elif re.search(r"master|\bm\.?s\.?\b|\bmba\b|\bm\.?eng\b", lvl):
        f.degree_level = "master"
    elif re.search(r"bachelor|\bb\.?s\.?\b|\bb\.?a\.?\b|undergrad", lvl):
        f.degree_level = "bachelor"
    return f


# The requirement wording a bar needs, and the softeners that unmake one.
_REQ = r"(?:must|required|requires?|requirement|need to|needs to|has to|have to|only|mandatory|shall)"
_SOFT = re.compile(r"\b(may require|might require|preferred|a plus|nice to have|not required|is not a requirement|encouraged|ideally|"
                   r"or (?:be )?(?:currently )?pursuing|in progress|or (?:an? )?equivalent|or later|or earlier|through|at least|typically)\b", re.I)

_CITIZEN = re.compile(
    r"(?:" + _REQ + r")\s+(?:be\s+)?(?:an?\s+)?(?:u\.?s\.?|united states|us)\s+citizen|"
    r"(?:u\.?s\.?|united states)\s+citizenship\s+(?:is\s+)?(?:required|mandatory|a requirement|necessary)|"
    r"open\s+(?:only\s+)?to\s+(?:u\.?s\.?|united states)\s+citizens|(?:u\.?s\.?|united states)\s+citizens?\s+only|"
    r"citizenship\s*:\s*(?:u\.?s\.?|united states)", re.I)
# The list forms that open a citizenship clause to a green-card holder:
# "citizen or permanent resident", "citizen or national, U.S. permanent
# resident, or protected individual", "green card". A bare mention of
# permanent residents is not one ("we cannot consider permanent residents").
_CITIZEN_OR_PR = re.compile(r"(?:\bor|,)\s*(?:a\s+)?(?:lawful\s+)?(?:u\.?s\.?\s+)?permanent\s+resident|green\s+card|or\s+(?:a\s+)?u\.?s\.?\s+person|"
                            r"protected\s+individual|permanent\s+residents?\s+(?:are|is)\s+(?:also\s+)?(?:eligible|welcome)", re.I)
_CLEARANCE = re.compile(
    r"(?:active|current|existing)\s+(?:secret|top[- ]secret|ts/?sci|public trust|security|dod)\s+clearance|"
    r"(?:" + _REQ + r")\s+(?:be\s+)?(?:able\s+to\s+)?(?:hold|have|possess|obtain|maintain|be eligible (?:for|to obtain))[^.]{0,40}?(?:security|secret|top[- ]secret|dod)?\s*clearance|"
    r"(?:ability|eligib(?:le|ility))\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?(?:u\.?s\.?\s+)?(?:security|secret|top[- ]secret|dod)?\s*clearance(?:\s+is)?\s+required|"
    r"clearance\s+(?:is\s+)?required", re.I)
_US_PERSON = re.compile(
    r"(?:" + _REQ + r")\s+(?:be\s+)?(?:an?\s+)?[\"“']?u\.?s\.?\s+person|u\.?s\.?\s+persons?\s+(?:only|required)|"
    r"\bITAR\b[^.]{0,160}?(?:must|required)", re.I)
_SPONSOR_BAR = re.compile(
    r"not\s+eligible\s+for\s+(?:cpt|opt|f-?1|j-?1)|no\s+(?:opt|cpt)\b|\b(?:cpt|opt|f-?1)\b[^.]{0,80}?not\s+(?:be\s+)?(?:eligible|considered)|"
    r"without\s+(?:limitation\s+or\s+)?sponsorship|(?:will|do|does)\s+not\s+sponsor|unable\s+to\s+sponsor|not\s+(?:able|willing)\s+to\s+sponsor|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|provided|offered)|cannot\s+(?:offer|provide)\s+(?:visa\s+)?sponsorship|"
    r"(?:visas?|visa\s+status|work\s+authori[sz]ation)\b[^.]{0,90}?\bnot\s+(?:be\s+)?eligible", re.I)
_GRAD_ONLY = re.compile(
    r"(?:" + _REQ + r")\s+(?:be\s+)?(?:currently\s+)?(?:enrolled|pursuing|working\s+towards?)(?:\s+in)?\s+(?:an?\s+)?(?:ms|m\.s\.|master'?s|masters|mba|ph\.?d\.?|doctoral|graduate)[- ]?(?:degree|program|student|candidate)|"
    r"(?:ms|master'?s|ph\.?d\.?|doctoral|graduate)[- ](?:students?|candidates?)\s+only|open\s+(?:only\s+)?to\s+(?:ms|master'?s|ph\.?d\.?|graduate)\s+students|"
    r"(?:" + _REQ + r")\s+(?:have|hold)\s+(?:completed\s+)?(?:an?\s+)?(?:bachelor'?s|undergraduate)\s+degree", re.I)
_GRAD_WINDOW = re.compile(
    r"graduat\w*[^.]{0,80}?\b(?:between|from)\s+(?:(?P<m1>[a-z]+)\s+)?(?P<y1>20\d\d)\s*(?:and|to|-|–|—|through)\s*(?:(?P<m2>[a-z]+)\s+)?(?P<y2>20\d\d)", re.I)
_GRAD_BY = re.compile(r"graduat\w*[^.]{0,60}?\b(?:by|before|no later than|prior to|on or before)\s+(?:(?P<m>[a-z]+)\s+)?(?P<y>20\d\d)", re.I)
_GRAD_IN = re.compile(r"(?:graduat\w*[^.]{0,40}?\b(?:in|date(?: of)?|date:)\s*(?:(?P<m>[a-z]+)\s+)?(?P<y>20\d\d)|class\s+of\s+(?P<y2>20\d\d))(?!\s*(?:-|–|—|to|or|and|through|\d))", re.I)


# A sentence boundary: a period and a space before a capital — but not the
# period of "U.S.", "e.g.", "i.e.", "Ph.D." or any single capital initial,
# which is how "citizen or national, U.S. permanent resident" was once cut
# in half and read as a citizenship-only bar (Xcimer, 2026-10-18).
_BOUNDARY = re.compile(r"(?<![A-Z])(?<!U\.S)(?<!e\.g)(?<!i\.e)(?<!\bvs)(?<!\bInc)(?<!\bLtd)\.\s+(?=[A-Z\"“'(])")


def _quote(text: str, start: int, end: int) -> str:
    """The sentence around a match, trimmed at real sentence boundaries: for
    the record and for the softener check, never for the match itself."""
    lo = max(0, start - 200)
    hi = min(len(text), end + 200)
    frag = text[lo:hi]
    s = 0
    for b in _BOUNDARY.finditer(frag, 0, max(0, start - lo)):
        s = b.end()
    nl = frag.rfind("\n", 0, start - lo)
    s = max(s, nl + 1)
    e = len(frag)
    m = _BOUNDARY.search(frag, end - lo)
    if m:
        e = m.start() + 1
    nl2 = frag.find("\n", end - lo)
    if nl2 != -1:
        e = min(e, nl2)
    return " ".join(frag[s:e].split())[:240]


def _after(text: str, end: int, n: int = 200) -> str:
    """The raw text right after a match — where a continuation such as
    ", U.S. permanent resident, or protected individual" lives."""
    return text[end:end + n]


def _month(name: str | None, default: int) -> int:
    if not name:
        return default
    n = name.lower()
    return _MONTHS.get(n) or _SEASON_MONTH.get(n) or default


def scan(text: str, facts: Facts) -> tuple[str, str]:
    """("bar", why) when the posting is closed to the applicant by an
    enumerated requirement they cannot meet, else ("ok", ""). Silent on any
    fact the bank does not state."""
    t = " ".join((text or "").split())
    if not t:
        return "ok", ""

    def soft(m) -> bool:
        return bool(_SOFT.search(_quote(t, m.start(), m.end())))

    if facts.citizen is False:
        for m in _CITIZEN.finditer(t):
            q = _quote(t, m.start(), m.end())
            if soft(m):
                continue
            if facts.permanent_resident and (_CITIZEN_OR_PR.search(q) or _CITIZEN_OR_PR.search(_after(t, m.end()))):
                continue  # citizens or permanent residents: open to a green-card holder
            return "bar", f"U.S. citizenship required: {q!r}"
        for m in _CLEARANCE.finditer(t):
            if soft(m):
                continue
            return "bar", f"a security clearance (which needs citizenship) is required: {_quote(t, m.start(), m.end())!r}"
        if not facts.permanent_resident:
            for m in _US_PERSON.finditer(t):
                if soft(m):
                    continue
                return "bar", f"U.S.-person status under ITAR is required: {_quote(t, m.start(), m.end())!r}"
    if facts.needs_sponsorship:
        for m in _SPONSOR_BAR.finditer(t):
            if soft(m):
                continue
            return "bar", f"no sponsorship / temporary status not eligible: {_quote(t, m.start(), m.end())!r}"
    if facts.degree_level == "bachelor":
        for m in _GRAD_ONLY.finditer(t):
            q = _quote(t, m.start(), m.end())
            if soft(m) or (re.search(r"bachelor|undergraduate|\bb\.?s\.?\b", q, re.I) and not re.search(r"hold|completed", m.group(0), re.I)):
                continue
            return "bar", f"graduate students only: {q!r}"
    if facts.grad_year:
        gm = facts.grad_month or 6
        for m in _GRAD_WINDOW.finditer(t):
            q = _quote(t, m.start(), m.end())
            if _SOFT.search(q) and not re.search(r"\b(?:must|required|eligib|expected|anticipated)\w*", q, re.I):
                continue
            y1, y2 = int(m.group("y1")), int(m.group("y2"))
            m1, m2 = _month(m.group("m1"), 1), _month(m.group("m2"), 12)
            if (facts.grad_year, gm) > (y2, m2) or (facts.grad_year, gm) < (y1, m1):
                return "bar", f"graduation window {y1}–{y2} excludes {facts.grad_year}: {q!r}"
        for m in _GRAD_BY.finditer(t):
            q = _quote(t, m.start(), m.end())
            if _SOFT.search(q):
                continue
            y, mo = int(m.group("y")), _month(m.group("m"), 12)
            if (facts.grad_year, gm) > (y, mo):
                return "bar", f"must graduate by {y}, before {facts.grad_year}: {q!r}"
        for m in _GRAD_IN.finditer(t):
            q = _quote(t, m.start(), m.end())
            if not re.search(r"\b(?:must|required|only|eligib|expected|anticipated)\w*", q, re.I) or _SOFT.search(q):
                continue
            y = int(m.group("y") or m.group("y2"))
            if y != facts.grad_year:
                return "bar", f"for students graduating in {y}, not {facts.grad_year}: {q!r}"
    return "ok", ""


# --- company size -----------------------------------------------------------

# Tier 4 in the kit's rubric: seed-stage, tiny teams, no external signal — do
# not apply. The signals that need no guessing, from the posting's own words.
_TIER4 = re.compile(
    r"\bpre-?seed\b|\bseed[- ]stage\b|\bseed round\b|raised (?:a |our )?seed\b|\bstealth(?:-mode| mode)?\b|"
    r"founding (?:engineer|team member|intern|hire|engineering team)|\bfirst (?:engineering |technical |full-time )?hire\b|"
    r"team size:?\s*(?:[1-9]|10)\b|\bteam of (?:two|three|four|five|six|seven|eight|nine|ten|[2-9]|10)\b|"
    r"\b(?:[1-9]|10|two|three|four|five|six|seven|eight|nine|ten)[- ]person (?:team|startup|company)\b|we are a (?:tiny|small) team of", re.I)
# A funding or size signal that outranks a seed word: a company that mentions
# its seed round on the way to its Series B is not a seed-stage company.
_FUNDED = re.compile(r"\bseries [a-f]\b|\$\s?\d+(?:\.\d+)?\s?(?:m|mm|million|b|billion)\b|\b\d{3,}\+?\s+(?:employees|people|engineers|team members)\b|"
                     r"\b(?:fortune|s&p) 500\b|publicly traded|\bnyse\b|\bnasdaq\b|headcount of \d{2,}", re.I)


def tier4_signal(text: str) -> str:
    """The posting's own words that make the company tier 4 (seed-stage or a
    tiny team), or '' — '' also when a stronger funding or size signal is
    present."""
    t = " ".join((text or "").split())
    if not t or _FUNDED.search(t):
        return ""
    m = _TIER4.search(t)
    return _quote(t, m.start(), m.end()) if m else ""


def skip_tier4(profile) -> bool:
    """`search.skip_tier4` (default yes): a tier-4 signal in the posting ends
    the attempt as skipped, in the loop and for the driver."""
    try:
        return bool((profile.answers.get("search", {}) or {}).get("skip_tier4", True))
    except Exception:
        return True


# --- the vendors' APIs -------------------------------------------------------

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
CACHE_TTL_S = 7 * 86400
_TAGS = re.compile(r"<[^>]+>")


def _strip(html_text: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", html.unescape(html_text or ""))).split())


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def api_url(url: str) -> str:
    """The vendor API address that carries a posting's text, or ''."""
    parts = urlsplit(url)
    host, path = (parts.netloc or "").lower(), parts.path.rstrip("/")
    kind = ats.host_kind(url)
    segs = [s for s in path.split("/") if s]
    if kind == "greenhouse" and "greenhouse.io" in host:
        m = re.search(r"/([^/]+)/jobs/(\d+)", path)
        if m:
            return f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}"
        m = re.search(r"^([a-z0-9-]+)\.greenhouse\.io$", host)
        j = re.search(r"/jobs/(\d+)", path)
        if m and j and m.group(1) not in ("boards", "job-boards", "boards-api"):
            return f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{j.group(1)}"
    if kind == "lever" and len(segs) >= 2:
        return f"https://api.lever.co/v0/postings/{segs[0]}/{segs[1]}"
    if kind == "ashby" and len(segs) >= 2:
        return f"https://api.ashbyhq.com/posting-api/job-board/{segs[0]}#job={segs[1]}"
    if kind == "workable":
        m = re.search(r"^/([^/]+)/j/([^/]+)", path)
        if m:
            return f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}"
    if kind == "smartrecruiters":
        m = re.search(r"^/([^/]+)/(\d+)", path)
        if m:
            return f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}"
    return ""


def _fetch(url: str) -> str | None:
    api = api_url(url)
    if not api:
        return None
    kind = ats.host_kind(url)
    try:
        if kind == "ashby":
            base, _, want = api.partition("#job=")
            data = _get_json(base + "?includeCompensation=false")
            for j in data.get("jobs") or []:
                if str(j.get("id")) == want or str(j.get("jobUrl") or "").rstrip("/").endswith(want):
                    return " ".join(x for x in (j.get("title"), j.get("descriptionPlain") or _strip(j.get("descriptionHtml") or "")) if x)
            return None
        data = _get_json(api)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError, TimeoutError):
        return None
    if kind == "greenhouse":
        return " ".join(x for x in (data.get("title"), _strip(data.get("content") or "")) if x) or None
    if kind == "lever":
        bits = [data.get("text"), data.get("descriptionPlain") or _strip(data.get("description") or "")]
        for lst in data.get("lists") or []:
            bits.append(str(lst.get("text") or ""))
            bits.append(_strip(lst.get("content") or ""))
        bits.append(data.get("additionalPlain") or _strip(data.get("additional") or ""))
        return " ".join(str(b) for b in bits if b) or None
    if kind == "workable":
        return " ".join(x for x in (data.get("title"), _strip(data.get("description") or ""), _strip(data.get("requirements") or ""),
                                    _strip(data.get("benefits") or "")) if x) or None
    if kind == "smartrecruiters":
        secs = ((data.get("jobAd") or {}).get("sections") or {})
        bits = [data.get("name")] + [_strip((secs.get(k) or {}).get("text") or "") for k in
                                     ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
        return " ".join(str(b) for b in bits if b) or None
    return None


def fetch_text(url: str, cache_dir: str | Path | None = None) -> str | None:
    """The posting's text from its vendor's public API, or None when the
    host has none or the call failed. Cached in <cache_dir>/eligibility-cache.json
    for a week, so a queue sweep and the handoff never fetch twice."""
    if not url or not api_url(url):
        return None
    cache_path = Path(cache_dir) / "eligibility-cache.json" if cache_dir else None
    cache: dict = {}
    if cache_path and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    hit = cache.get(url)
    if hit and time.time() - float(hit.get("at") or 0) < CACHE_TTL_S:
        return hit.get("text") or None
    text = _fetch(url)
    if cache_path:
        cache[url] = {"at": time.time(), "text": text or ""}
        if len(cache) > 3000:
            for k in sorted(cache, key=lambda k: float(cache[k].get("at") or 0))[: len(cache) - 3000]:
                cache.pop(k, None)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(cache_path)
        except OSError:
            pass
    return text
