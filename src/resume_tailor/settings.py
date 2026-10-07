"""What the dashboard's Settings tab reads and writes.

Everything the loop is configured by lives in files under ~/.resume-tailor:
`env` (provider, keys, models), `answers.yaml` (the form answer bank and the
`search` block that steers discovery), `career.yaml` (the record every résumé
draws on) and `resume/base.yaml` (the skeleton every tailored résumé keeps).
This module is the one place those files are written by the tool itself, so
the rules are in one place:

- YAML is edited in place with ruamel.yaml's round-trip loader: the comments
  the owner wrote next to each key survive a save. A timestamped `.bak-` copy
  is written before every change, the convention already used in that directory.
- A string is written double-quoted whenever PyYAML — the reader everywhere
  else — would read the plain form as something else: "Yes", "No", "2028",
  "3.42" all stay strings.
- Secrets never travel to the page. A key is reported as set or unset with
  its last four characters; the page sends a new value or nothing.
- Nothing here restarts a worker on its own. Workers read these files once
  at start, so a save tells the page "restart to apply" and the restart is a
  separate, explicit action (`restart_workers`). The loop as a whole is
  switched off and on the same way (`stop_workers`, `start_workers`): the
  page's Stop button ends the supervisor, every worker and the fresh lane;
  Start launches the owner's supervisor script again, or `start` without one.
- The résumé intake (`draft_skeleton`) is the one model call: it transcribes
  an uploaded résumé into the skeleton's shape for the owner to check and
  save. Every number in the draft is verified against the uploaded text and
  the mismatches are flagged; nothing is written until the owner presses Save.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .profile import DEFAULT_PROFILE_DIR

# --- the env file --------------------------------------------------------------

ENV_FIELDS: list[dict] = [
    {"key": "RESUME_TAILOR_PROVIDER", "group": "provider", "label": "Provider", "kind": "choice", "options": ["anthropic", "openai"],
     "help": "Which model family reads postings, drafts bullets, audits them, judges the fit and answers form questions."},
    {"key": "ANTHROPIC_API_KEY", "group": "keys", "label": "Anthropic API key", "secret": True,
     "help": "Leave unset to use an `ant auth login` profile instead."},
    {"key": "OPENAI_API_KEY", "group": "keys", "label": "OpenAI API key", "secret": True,
     "help": "Needed for the OpenAI provider — and always for inbox scanning and recruiter search, whatever the provider."},
    {"key": "RESUME_TAILOR_MODEL", "group": "models", "label": "Main model",
     "help": "Reads the posting, plans and drafts. Blank uses the provider's default."},
    {"key": "RESUME_TAILOR_AUDIT_MODEL", "group": "models", "label": "Audit model",
     "help": "The many per-claim checks — two-thirds of a run's calls. A cheaper model with a roomier rate limit is the usual choice."},
    {"key": "RESUME_TAILOR_JUDGE_MODEL", "group": "models", "label": "Judge model",
     "help": "The two pre-submission match judges. Blank uses the main model."},
    {"key": "RESUME_TAILOR_MAIL_MODEL", "group": "models", "label": "Inbox model",
     "help": "Sorts replies into stages (assessment, interview, offer, rejection). Always OpenAI; default gpt-4o."},
    {"key": "RESUME_TAILOR_SEARCH_MODEL", "group": "models", "label": "Recruiter-search model",
     "help": "Finds recruiting addresses for outreach. Always OpenAI; default gpt-4o."},
    {"key": "RESUME_TAILOR_CONCURRENCY", "group": "models", "label": "Calls in flight", "kind": "number",
     "help": "Model calls at once per provider (default 4). Lower it on a tight rate-limit tier; a 429 already pauses every call."},
    {"key": "RESUME_TAILOR_IMAP_USER", "group": "mailbox", "label": "Mailbox address",
     "help": "The inbox read for one-time codes, confirmations and assessments, and that recruiter notes are sent from."},
    {"key": "RESUME_TAILOR_IMAP_PASSWORD", "group": "mailbox", "label": "Mailbox app password", "secret": True,
     "help": "Gmail: Google account → Security → App passwords."},
    {"key": "RESUME_TAILOR_IMAP_HOST", "group": "mailbox", "label": "IMAP host", "help": "Default imap.gmail.com."},
    {"key": "RESUME_TAILOR_SITE_PASSWORD", "group": "accounts", "label": "Portal account password", "secret": True,
     "help": "The one password for every account the tool creates on the hosts listed under Search → Accounts the tool may create."},
    {"key": "RESUME_TAILOR_HEADLESS", "group": "browser", "label": "Hide the browser", "kind": "bool01",
     "help": "On hides the workers' Chrome windows; off shows them (useful the first few times)."},
]
_SECRET_WORDS = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.I)
_PLACEHOLDERS = {"", "PASTE-YOUR-KEY-HERE", "YOUR_KEY_HERE"}
MODEL_SUGGESTIONS = {
    "anthropic": ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    "openai": ["gpt-5.2", "gpt-4o", "o3-mini"],
}


def env_path(profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    return Path(profile_dir) / "env"


def read_env(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict[str, str]:
    """The env file's active KEY=value lines, parsed the way llm._load_env_file
    parses them; untouched placeholders read as unset."""
    path = env_path(profile_dir)
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key.strip() and value not in _PLACEHOLDERS:
            out[key.strip()] = value
    return out


def _split_env_line(line: str) -> tuple[str, str, str]:
    """('KEY', 'value', '   # trailing comment') from an active line."""
    key, _, rest = line.partition("=")
    m = re.search(r"\s+#.*$", rest)
    comment = m.group(0) if m else ""
    value = rest[: m.start()] if m else rest
    return key.strip(), value.strip(), comment


def write_env(updates: dict[str, str | None], profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    """Set, replace or clear keys in the env file without touching anything
    else in it. An active line is edited in place (its trailing comment
    kept); a commented-out `# KEY=` line is brought back to life; a key the
    file never had is appended. None or "" comments the line out — the value
    is gone, the line stays as a reminder of the key. Mode 600 is kept."""
    path = env_path(profile_dir)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key or ""):
            raise ValueError(f"not an environment variable name: {key!r}")
        value = "" if value is None else str(value).strip()
        if "\n" in value or " #" in value:
            raise ValueError(f"{key}: a value cannot contain a newline or ' #'")
        active = next((i for i, l in enumerate(lines) if re.match(rf"^\s*{re.escape(key)}\s*=", l)), None)
        dormant = next((i for i, l in enumerate(lines) if re.match(rf"^\s*#\s*{re.escape(key)}\s*=", l)), None)
        if not value:
            if active is not None:
                lines[active] = "# " + lines[active].strip()
            continue
        if active is not None:
            _, _, comment = _split_env_line(lines[active])
            lines[active] = f"{key}={value}{comment}"
        elif dormant is not None:
            _, _, comment = _split_env_line(lines[dormant].lstrip("# ").lstrip("#"))
            lines[dormant] = f"{key}={value}{comment}"
        else:
            if not any(l.strip() == "# set from the dashboard" for l in lines):
                lines += ["", "# set from the dashboard"]
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def env_view(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """Every known key with its value — or, for a secret, only whether it is
    set and its last four characters — plus any other key the file holds."""
    raw = read_env(profile_dir)
    fields = []
    seen = set()
    for spec in ENV_FIELDS:
        key, secret = spec["key"], bool(spec.get("secret"))
        v = raw.get(key, "")
        seen.add(key)
        fields.append({**spec, "secret": secret, "set": bool(v), "value": "" if secret else v,
                       "hint": (v[-4:] if secret and len(v) >= 8 else "")})
    for key, v in sorted(raw.items()):
        if key in seen:
            continue
        secret = bool(_SECRET_WORDS.search(key))
        fields.append({"key": key, "group": "other", "label": key, "help": "", "secret": secret, "set": bool(v),
                       "value": "" if secret else v, "hint": (v[-4:] if secret and len(v) >= 8 else "")})
    from .llm import DEFAULT_MODELS

    return {"path": str(env_path(profile_dir)), "fields": fields, "defaults": DEFAULT_MODELS, "suggestions": MODEL_SUGGESTIONS}


# --- YAML, round-trip ------------------------------------------------------------

def _yaml():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 10000
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_rt(path: Path):
    """The file as ruamel's commented structures (an empty mapping when the
    file is missing), so a save keeps the owner's comments."""
    from ruamel.yaml.comments import CommentedMap

    if not Path(path).is_file():
        return CommentedMap()
    data = _yaml().load(Path(path).read_text(encoding="utf-8"))
    return data if data is not None else CommentedMap()


def dump_rt(data) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def save_rt(path: Path, data) -> bool:
    """Write the structure back; a .bak- copy first when the file existed and
    the text actually changes. Returns whether anything was written."""
    path = Path(path)
    text = dump_rt(data)
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old == text:
        return False
    if old is not None:
        path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}").write_text(old, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def plain(obj: Any) -> Any:
    """ruamel's commented structures and scalar subclasses as plain Python."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    return obj


_YAML11_WORDS = re.compile(r"(?i)^(y|yes|n|no|on|off|true|false|null|~)$")
_NUMBERISH = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$|^0x[0-9a-fA-F]+$|^0o?[0-7]+$|^[-+]?\.(inf|Inf|INF|nan|NaN|NAN)$")
_PLAIN_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 ._/()+,'&$%;-]*$")


def scalar(value: Any) -> Any:
    """A value as it should be stored: strings that PyYAML would read as a
    bool, a number or null — or that are not a simple phrase — go in double
    quotes; everything else stays plain."""
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    s = str(value)
    if s == "" or _YAML11_WORDS.match(s.strip()) or _NUMBERISH.match(s.strip()) or s != s.strip() \
            or not _PLAIN_OK.match(s) or ": " in s or " #" in s or s.endswith(":"):
        return DoubleQuotedScalarString(s)
    return s


def to_node(value: Any, like: Any = None):
    """Plain JSON data as ruamel structures, keeping the flow style of the
    list or map it replaces (`[a, b]` stays on one line)."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[str(k)] = to_node(v, like.get(k) if isinstance(like, dict) else None)
        if isinstance(like, CommentedMap) and like.fa.flow_style():
            m.fa.set_flow_style()
        return m
    if isinstance(value, list):
        seq = CommentedSeq([to_node(v) for v in value])
        if isinstance(like, CommentedSeq) and like.fa.flow_style():
            seq.fa.set_flow_style()
        return seq
    return scalar(value)


def merge_into(target, data: dict, removed: list[str] | None = None) -> None:
    """Set every key in `data` on the commented mapping (nested maps merge,
    so a key's comment survives a changed value); delete the dotted paths in
    `removed`. Keys the page did not send are left alone."""
    from ruamel.yaml.comments import CommentedMap

    for k, v in (data or {}).items():
        k = str(k)
        if isinstance(v, dict) and isinstance(target.get(k), CommentedMap):
            merge_into(target[k], v)
            for gone in [x for x in list(target[k].keys()) if str(x) not in v]:
                del target[k][gone]
        else:
            target[k] = to_node(v, target.get(k))
    for path in removed or []:
        parts = [p for p in str(path).split(".") if p]
        node = target
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and parts and parts[-1] in node:
            del node[parts[-1]]


# --- the answer bank ----------------------------------------------------------

SECTION_HELP: dict[str, tuple[str, str]] = {
    "work_authorization": ("Work authorization", "Yes/No answers to the authorization and sponsorship questions, per region. `default_region` says which region a bare \"require sponsorship?\" means."),
    "work_preferences": ("Work preferences", "Remote, hybrid, relocation, assessments, background checks."),
    "availability": ("Availability", "Start dates, notice period, the term you are applying for, hours per week."),
    "salary_expectations": ("Compensation", "Several spellings of the same answer so every wording of the question finds it. Internship pay is hourly."),
    "self_identification": ("Self-identification", "All voluntary. A decline lands on whatever decline option the form offers."),
    "address": ("Address", "The default address, and alternates used when the posting's location matches their terms."),
    "education": ("Education answers", "Graduation, enrollment, GPA and test scores as forms ask them. School, degree and major come from the career record."),
    "consents": ("Consents", "The boxes forms ask you to tick on the way to Submit."),
    "documents": ("Extra documents", "Files some forms ask for besides the résumé, matched by the upload field's label. Blank means the form goes to review instead of guessing."),
    "about": ("About you", "Small personal fields: name pronunciation, contact preference, school e-mail, what you have read lately."),
    "immigration": ("Immigration", "Questions that assume a visa holder, answered in your own words."),
    "search": ("Search", "How postings are chosen and what happens before a submission."),
}
SEARCH_HELP: dict[str, str] = {
    "terms": "Terms to apply for; a title naming another term outright is left out.",
    "positions": "Title words worth applying to.",
    "categories": "Listing categories worth reading: software, analyst, ai/ml, data, product.",
    "require_us": "Only postings with a US location (or none stated).",
    "exclude_phd_only": "Skip postings that want a PhD.",
    "max_posting_age_days": "A posting older than this by its listed date is not attempted (0 = no cap).",
    "company_blacklist": "Never apply here.",
    "title_blacklist": "Skip titles containing any of these.",
    "location_blacklist": "Skip these locations.",
    "apply_once_at_company": "One application per company, ever.",
    "company_cooldown_days": "With apply-once off: days a company rests after an application.",
    "approve_before_submit": "Companies whose forms are filled but held for your Approve button on the Applications tab.",
    "create_accounts_on": "Hosts where the tool may create an account with your e-mail and the portal password from AI & keys.",
    "min_judge_score": "Both judges must score at least this, out of 100, before a submission.",
    "max_revisions": "How many times a résumé under the bar is revised from the judges' notes.",
    "apply_below_bar": "After the revisions, submit anyway unless an eligibility rule is unmet. The workers' environment may override this (RESUME_TAILOR_APPLY_BELOW_BAR).",
    "jobright_account": "The tool's browser is signed in to jobright.ai, so its links resolve.",
    "sources": "Which lists discovery reads (see Job sources).",
    "extra_sources": "Your own README-table lists (see Job sources).",
}


def answers_path(profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    return Path(profile_dir) / "answers.yaml"


def answers_view(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    data = plain(load_rt(answers_path(profile_dir)))
    sections = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        label, help_ = SECTION_HELP.get(key, (key.replace("_", " ").capitalize(), ""))
        sections.append({"key": key, "label": label, "help": help_, "data": value})
    return {"path": str(answers_path(profile_dir)), "sections": sections, "search_help": SEARCH_HELP,
            "known_sections": [{"key": k, "label": v[0], "help": v[1]} for k, v in SECTION_HELP.items()]}


def save_answers_section(section: str, data: dict, removed: list[str] | None = None,
                         profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """Replace one top-level section of answers.yaml with what the page holds,
    key by key, keeping the comments; `removed` lists dotted paths deleted
    on the page."""
    from ruamel.yaml.comments import CommentedMap

    if not re.fullmatch(r"[a-z][a-z0-9_]*", section or ""):
        raise ValueError(f"not a section name: {section!r}")
    if not isinstance(data, dict):
        raise ValueError("a section is a mapping")
    root = load_rt(answers_path(profile_dir))
    if not isinstance(root.get(section), CommentedMap):
        root[section] = CommentedMap()
    merge_into(root[section], data, [f"{p}" for p in (removed or [])])
    for gone in [k for k in list(root[section].keys()) if str(k) not in data]:
        del root[section][gone]
    changed = save_rt(answers_path(profile_dir), root)
    return {"saved": changed, "section": section}


# --- discovery sources --------------------------------------------------------

def sources_view(answers: dict, out_dir: Path) -> list[dict]:
    """Each list discovery can read — the built-ins and the owner's own —
    with whether it is on and how many postings the last fetch took from it."""
    from .discover import DEFAULT_URL, TABLE_SOURCES

    s = (answers or {}).get("search") or {}
    flags = s.get("sources") if isinstance(s.get("sources"), dict) else {}
    meta: dict = {}
    try:
        meta = json.loads((Path(out_dir) / "discover-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    table_counts = meta.get("table_sources") or {}
    total = int(meta.get("count") or 0)
    simplify_count = max(0, total - sum(int(v) for v in table_counts.values())) if total else 0
    labels = {"simplify": "SimplifyJobs · Summer 2027 Internships", "jobright-swe": "jobright-ai · Software Engineer internships",
              "jobright-ba": "jobright-ai · Business Analyst internships", "speedyapply": "speedyapply · SWE college jobs",
              "speedyapply-ai": "speedyapply · AI college jobs", "vanshb03": "vanshb03 · Summer internships",
              "sndsh404": "sndsh404 · Summer 2027 internships"}
    out = [{"name": "simplify", "label": labels["simplify"], "url": DEFAULT_URL, "builtin": True, "kind": "json",
            "enabled": bool(flags.get("simplify", True)), "count": simplify_count}]
    for name, url in TABLE_SOURCES:
        out.append({"name": name, "label": labels.get(name, name), "url": url, "builtin": True, "kind": "table",
                    "enabled": bool(flags.get(name, True)), "count": int(table_counts.get(name) or 0)})
    for item in s.get("extra_sources") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, url = str(item[0]), str(item[1])
        elif isinstance(item, dict) and item.get("url"):
            name, url = str(item.get("name") or item["url"]), str(item["url"])
        else:
            continue
        out.append({"name": name, "label": name, "url": url, "builtin": False, "kind": "table", "enabled": True,
                    "count": int(table_counts.get(name) or 0)})
    return out


# --- the basics: who you are, on every résumé and form -------------------------------

def career_path(profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    return Path(profile_dir) / "career.yaml"


def base_path(profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    return Path(profile_dir) / "resume" / "base.yaml"


BASIC_PERSONAL = ["name", "surname", "email", "phone_prefix", "phone", "city", "country", "linkedin", "github", "website", "work_authorization"]
BASIC_EDU = ["institution", "education_level", "field_of_study", "location", "start_date", "year_of_completion"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def graduation_parts(text: str) -> tuple[str, str]:
    """("May", "2028") from "May 2028", "May, 2028", "05/2028" or "2028";
    empty strings for what the text does not say."""
    t = (text or "").strip()
    m = re.search(r"([A-Za-z]{3,})\.?,?\s+(\d{4})", t)
    if m:
        mon = next((x for x in _MONTHS if x.lower().startswith(m.group(1).lower()[:3])), m.group(1).capitalize())
        return mon, m.group(2)
    m = re.search(r"(\d{1,2})\s*/\s*(\d{4})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return _MONTHS[int(m.group(1)) - 1], m.group(2)
    m = re.search(r"\b(\d{4})\b", t)
    return "", (m.group(1) if m else "")


def basics_view(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    career = plain(load_rt(career_path(profile_dir)))
    answers = plain(load_rt(answers_path(profile_dir)))
    base = plain(load_rt(base_path(profile_dir)))
    pi = career.get("personal_information") or {}
    edus = career.get("education_details") or []
    ed = edus[0] if edus and isinstance(edus[0], dict) else {}
    edu_answers = answers.get("education") or {}
    return {
        "personal": {k: str(pi.get(k) or "") for k in BASIC_PERSONAL},
        "education": {**{k: str(ed.get(k) or "") for k in BASIC_EDU}, "coursework": [str(c) for c in (ed.get("coursework") or [])]},
        "gpa": str(edu_answers.get("gpa") or edu_answers.get("cumulative_gpa") or ""),
        "show_gpa_on_resume": bool(str((base.get("education") or {}).get("gpa") or "").strip()) if isinstance(base.get("education"), dict) else False,
        "paths": {"career": str(career_path(profile_dir)), "answers": str(answers_path(profile_dir)), "base": str(base_path(profile_dir))},
    }


def save_basics(data: dict, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """One form, three files: the record's personal block and first education
    entry; the answer bank's GPA and every graduation spelling forms ask for;
    the skeleton's graduation line and whether the GPA prints on the résumé."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    written = []
    personal = data.get("personal") or {}
    education = data.get("education") or {}
    gpa = str(data.get("gpa") or "").strip()
    show_gpa = bool(data.get("show_gpa_on_resume"))

    career = load_rt(career_path(profile_dir))
    if not isinstance(career.get("personal_information"), CommentedMap):
        career["personal_information"] = CommentedMap()
    for k in BASIC_PERSONAL:
        if k in personal:
            v = str(personal.get(k) or "").strip()
            if v or k in career["personal_information"]:
                career["personal_information"][k] = scalar(v)
    if not isinstance(career.get("education_details"), CommentedSeq) or not career["education_details"]:
        career["education_details"] = CommentedSeq([CommentedMap()])
    ed = career["education_details"][0]
    for k in BASIC_EDU:
        if k in education:
            v = str(education.get(k) or "").strip()
            if v or k in ed:
                ed[k] = scalar(v)
    if "coursework" in education:
        ed["coursework"] = to_node([str(c).strip() for c in (education.get("coursework") or []) if str(c).strip()], ed.get("coursework"))
    if save_rt(career_path(profile_dir), career):
        written.append("career.yaml")

    answers = load_rt(answers_path(profile_dir))
    if not isinstance(answers.get("education"), CommentedMap):
        answers["education"] = CommentedMap()
    edu = answers["education"]
    if "gpa" in data:
        for k in ("gpa", "cumulative_gpa"):
            if gpa:
                edu[k] = scalar(gpa)
            elif k in edu:
                del edu[k]
    grad = str(education.get("year_of_completion") or "").strip()
    month_year = grad
    if grad:
        month, year = graduation_parts(grad)
        month_year = f"{month} {year}".strip() if year else grad
        for k in ("graduation_date", "expected_graduation", "graduation_month_and_year"):
            edu[k] = scalar(month_year)
        if year:
            edu["graduation_year"] = scalar(year)
            if "end_date_year" in edu:
                edu["end_date_year"] = scalar(year)
        if month:
            for k in ("graduation_month", "end_date_month"):
                if k in edu:
                    edu[k] = scalar(month)
    if save_rt(answers_path(profile_dir), answers):
        written.append("answers.yaml")

    if base_path(profile_dir).is_file():
        base = load_rt(base_path(profile_dir))
        if not isinstance(base.get("education"), CommentedMap):
            base["education"] = CommentedMap()
        if grad:
            base["education"]["graduation"] = scalar(month_year)  # the skeleton's line follows the record
        if show_gpa and gpa:
            base["education"]["gpa"] = scalar(gpa)
        elif "gpa" in base["education"]:
            del base["education"]["gpa"]
        if save_rt(base_path(profile_dir), base):
            written.append("resume/base.yaml")
    return {"saved": written}


# --- the résumé skeleton ---------------------------------------------------------

SKELETON_KEYS = ["file_name", "style", "max_pages", "header", "education", "roles", "projects", "skills", "honors"]


def career_roles(career: dict) -> list[dict]:
    out = []
    for i, exp in enumerate(career.get("experience_details") or []):
        if isinstance(exp, dict):
            out.append({"id": f"exp{i}", "label": f"{exp.get('position') or '?'} — {exp.get('company') or '?'} ({exp.get('employment_period') or '?'})",
                        "position": str(exp.get("position") or ""), "company": str(exp.get("company") or ""),
                        "bullets": [str(r.get("responsibility") if isinstance(r, dict) else r) for r in (exp.get("key_responsibilities") or [])]})
    return out


def empty_skeleton() -> dict:
    return {"file_name": "", "style": "jake", "max_pages": 1,
            "header": {"phone": "", "email": "", "location": "", "linkedin": "", "github": "", "website": "", "work_authorization": ""},
            "education": {"degree": "", "graduation": "", "gpa": "", "coursework": "", "lines": []},
            "roles": [], "projects": [], "skills": [], "honors": []}


def skeleton_view(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    from .render import available_styles

    base = plain(load_rt(base_path(profile_dir))) if base_path(profile_dir).is_file() else {}
    career = plain(load_rt(career_path(profile_dir)))
    skeleton = empty_skeleton()
    for k in SKELETON_KEYS:
        if k in base:
            skeleton[k] = base[k]
    if isinstance(skeleton.get("education"), dict):
        skeleton["education"] = {**empty_skeleton()["education"], **skeleton["education"]}
    if isinstance(skeleton.get("header"), dict):
        skeleton["header"] = {**empty_skeleton()["header"], **skeleton["header"]}
    uploads = sorted((Path(profile_dir) / "resume" / "uploads").glob("*")) if (Path(profile_dir) / "resume" / "uploads").is_dir() else []
    return {"path": str(base_path(profile_dir)), "exists": base_path(profile_dir).is_file(), "skeleton": skeleton,
            "career_roles": career_roles(career), "styles": sorted(available_styles()),
            "uploads": [{"name": p.name, "when": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))} for p in uploads if p.is_file()]}


def _clean_list(items: Any) -> list[str]:
    return [str(x).strip() for x in (items or []) if str(x).strip()]


def save_skeleton(skeleton: dict, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """Write resume/base.yaml from the page's skeleton. A role with no record
    entry behind it (id empty or "new") is first appended to career.yaml as
    an experience entry — the skeleton's bullets become its responsibilities —
    and given the exp id the evidence index will assign it, so every bullet on
    the résumé still cites something in the record."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if not isinstance(skeleton, dict):
        raise ValueError("the skeleton is a mapping")
    written = []
    career = load_rt(career_path(profile_dir))
    exps = career.get("experience_details")
    if not isinstance(exps, CommentedSeq):
        exps = CommentedSeq()
        career["experience_details"] = exps
    roles_out = []
    added = []
    for role in skeleton.get("roles") or []:
        if not isinstance(role, dict):
            continue
        bullets = _clean_list(role.get("base"))
        rid = str(role.get("id") or "").strip()
        if not re.fullmatch(r"exp\d+", rid) or int(rid[3:]) >= len(exps):
            entry = CommentedMap()
            entry["position"] = scalar(str(role.get("title") or "").strip())
            entry["company"] = scalar(str(role.get("org") or "").strip())
            entry["employment_period"] = scalar(str(role.get("dates") or "").strip())
            entry["location"] = scalar(str(role.get("location") or "").strip())
            entry["key_responsibilities"] = CommentedSeq([CommentedMap([("responsibility", scalar(b))]) for b in bullets])
            entry["skills_acquired"] = CommentedSeq()
            exps.append(entry)
            rid = f"exp{len(exps) - 1}"
            added.append(rid)
        r = {"id": rid}
        for k in ("title", "org", "org_link", "location", "dates"):
            v = str(role.get(k) or "").strip()
            if v:
                r[k] = v
        r["base"] = bullets
        roles_out.append(r)
    if added and save_rt(career_path(profile_dir), career):
        written.append("career.yaml")

    base = load_rt(base_path(profile_dir))
    fn = str(skeleton.get("file_name") or "").strip()
    if fn:
        base["file_name"] = scalar(fn)
    elif "file_name" in base:
        del base["file_name"]
    base["style"] = scalar(str(skeleton.get("style") or "jake").strip() or "jake")
    try:
        base["max_pages"] = max(1, int(skeleton.get("max_pages") or 1))
    except (TypeError, ValueError):
        base["max_pages"] = 1
    header = {k: str(v).strip() for k, v in (skeleton.get("header") or {}).items() if str(v or "").strip()}
    if header:
        base["header"] = to_node(header, base.get("header"))
    elif "header" in base:
        del base["header"]
    edu_in = skeleton.get("education") or {}
    edu = {}
    for k in ("degree", "graduation", "gpa", "coursework"):
        v = str(edu_in.get(k) or "").strip()
        if v:
            edu[k] = v
    lines = _clean_list(edu_in.get("lines"))
    if lines:
        edu["lines"] = lines
    if not isinstance(base.get("education"), CommentedMap):
        base["education"] = CommentedMap()
    merge_into(base["education"], edu)
    for gone in [k for k in list(base["education"].keys()) if str(k) not in edu]:
        del base["education"][gone]
    base["roles"] = to_node(roles_out, base.get("roles"))
    projects = []
    for p in skeleton.get("projects") or []:
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue
        q = {"name": str(p["name"]).strip()}
        for k in ("link", "tech", "dates"):
            if str(p.get(k) or "").strip():
                q[k] = str(p[k]).strip()
        q["bullets"] = _clean_list(p.get("bullets"))
        projects.append(q)
    base["projects"] = to_node(projects, base.get("projects"))
    skills = [{"label": str(s.get("label")).strip(), "terms": str(s.get("terms")).strip()}
              for s in (skeleton.get("skills") or []) if isinstance(s, dict) and str(s.get("label") or "").strip() and str(s.get("terms") or "").strip()]
    base["skills"] = to_node(skills, base.get("skills"))
    base["honors"] = to_node(_clean_list(skeleton.get("honors")), base.get("honors"))
    if save_rt(base_path(profile_dir), base):
        written.append("resume/base.yaml")
    return {"saved": written, "added_roles": added}


# --- résumé intake: an uploaded file to a draft skeleton ------------------------------

UPLOAD_MAX = 15 * 1024 * 1024
UPLOAD_KINDS = {".pdf", ".docx", ".tex", ".txt", ".md"}


def extract_text(name: str, data: bytes) -> str:
    """The words of an uploaded résumé: the PDF's text layer (what an ATS
    sees), a .docx's paragraphs, a .tex file with its commands stripped, or
    plain text."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))
    text = data.decode("utf-8", "replace")
    if ext == ".tex":
        text = re.sub(r"(?m)(?<!\\)%.*$", "", text)
        text = re.sub(r"\\item\b", "\n- ", text)
        text = re.sub(r"\\(begin|end)\{[^}]*\}", "\n", text)
        text = re.sub(r"\\[A-Za-z]+\*?(\[[^\]]*\])?", " ", text)
        text = re.sub(r"[{}]", "", text)
        text = re.sub(r"\\[&%$#_]", lambda m: m.group(0)[1], text)
    return text


def save_upload(name: str, data: bytes, profile_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip("-.") or "resume"
    if Path(safe).suffix.lower() not in UPLOAD_KINDS:
        raise ValueError("upload a .pdf, .docx, .tex, .txt or .md résumé")
    if len(data) > UPLOAD_MAX:
        raise ValueError("that file is over 15 MB")
    folder = Path(profile_dir) / "resume" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / safe
    dest.write_bytes(data)
    return dest


class DraftHeader(BaseModel):
    name: str = Field(description="The full name as printed. Empty string when absent.")
    phone: str = Field(description="As printed, or empty string")
    email: str = Field(description="As printed, or empty string")
    location: str = Field(description="City and state/country in the header, or empty string")
    linkedin: str = Field(description="The LinkedIn URL as printed, or empty string")
    github: str = Field(description="The GitHub URL as printed, or empty string")
    website: str = Field(description="Any other personal URL, or empty string")


class DraftEducation(BaseModel):
    school: str = Field(description="Institution name, or empty string")
    degree: str = Field(description="Degree and field exactly as printed, e.g. 'B.S. in Computer Science', or empty string")
    location: str = Field(description="School location, or empty string")
    graduation: str = Field(description="Graduation month and year as printed, e.g. 'May 2028', or empty string")
    gpa: str = Field(description="GPA exactly as printed, e.g. '3.42/4.0', or empty string when the résumé does not print one")
    coursework: str = Field(description="The coursework line as printed, comma-separated, or empty string")
    lines: list[str] = Field(description="Other lines under education (teaching, honors listed there), verbatim. Empty when none.")


class DraftRole(BaseModel):
    title: str = Field(description="Job title as printed")
    org: str = Field(description="Employer or lab as printed")
    location: str = Field(description="As printed, or empty string")
    dates: str = Field(description="The date range as printed, or empty string")
    bullets: list[str] = Field(description="Every bullet under this role, verbatim, in order")


class DraftProject(BaseModel):
    name: str = Field(description="Project name as printed")
    tech: str = Field(description="The technologies line as printed, or empty string")
    dates: str = Field(description="As printed, or empty string")
    link: str = Field(description="A URL printed with the project, or empty string")
    bullets: list[str] = Field(description="Every bullet under this project, verbatim, in order")


class DraftSkill(BaseModel):
    label: str = Field(description="The skills line's label, e.g. 'Languages'")
    terms: str = Field(description="The rest of that line, verbatim")


class SkeletonDraft(BaseModel):
    header: DraftHeader
    education: DraftEducation
    roles: list[DraftRole] = Field(description="Work, research and internship entries in the résumé's order")
    projects: list[DraftProject] = Field(description="Project entries in the résumé's order. Empty when the résumé has no projects section.")
    skills: list[DraftSkill] = Field(description="One per skills line. Empty when there is no skills section.")
    honors: list[str] = Field(description="Honors and awards lines, verbatim. Empty when none.")


_INTAKE_SYSTEM = (
    "You transcribe a résumé into a fixed structure. Copy every phrase exactly as the résumé prints it: "
    "do not improve, shorten, merge, reorder or add anything, and never invent a number, a date, a name or a link. "
    "Text that the résumé does not contain is an empty string or an empty list. Bullets are copied whole, one per "
    "list item, in the order printed. Where extraction broke a line in two, join it back into one bullet."
)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def numeral_flags(draft: dict, source_text: str) -> list[str]:
    """Bullets and lines in the draft carrying a number the uploaded text
    does not: the one kind of transcription slip that changes a claim."""
    have = _numbers(source_text)
    flags = []

    def look(label: str, text: str) -> None:
        missing = sorted(n for n in _numbers(text) if n not in have)
        if missing:
            flags.append(f"{label}: {', '.join(missing)} not in the uploaded text — «{text[:90]}»")

    for i, role in enumerate(draft.get("roles") or []):
        for j, b in enumerate(role.get("bullets") or []):
            look(f"role {i + 1} bullet {j + 1}", b)
        look(f"role {i + 1} dates", role.get("dates") or "")
    for i, p in enumerate(draft.get("projects") or []):
        for j, b in enumerate(p.get("bullets") or []):
            look(f"project {i + 1} bullet {j + 1}", b)
    for i, h in enumerate(draft.get("honors") or []):
        look(f"honor {i + 1}", h)
    edu = draft.get("education") or {}
    look("education gpa", edu.get("gpa") or "")
    look("education graduation", edu.get("graduation") or "")
    return flags


def match_roles(draft_roles: list[dict], career: dict) -> list[str]:
    """For each drafted role, the record's exp id whose employer (or title)
    it names, else "" — the owner confirms on the page."""
    from .queue import company_key

    known = career_roles(career)
    out = []
    for role in draft_roles:
        org_words = set(company_key(role.get("org") or "").split())
        title = (role.get("title") or "").strip().lower()
        best, best_n = "", 0
        for k in known:
            words = set(company_key(k["company"]).split())
            n = len(org_words & words - {"university", "lab", "inc", "institute", "of", "the", "and"})
            if title and title == k["position"].strip().lower():
                n += 2
            if n > best_n:
                best, best_n = k["id"], n
        out.append(best)
    return out


def draft_to_skeleton(draft: dict, career: dict) -> dict:
    """The model's transcription in base.yaml's shape, roles matched to the record."""
    ids = match_roles(draft.get("roles") or [], career)
    h = draft.get("header") or {}
    e = draft.get("education") or {}
    return {
        "file_name": "", "style": "jake", "max_pages": 1,
        "header": {"phone": h.get("phone") or "", "email": h.get("email") or "", "location": h.get("location") or "",
                   "linkedin": h.get("linkedin") or "", "github": h.get("github") or "", "website": h.get("website") or "", "work_authorization": ""},
        "education": {"degree": e.get("degree") or "", "graduation": e.get("graduation") or "", "gpa": e.get("gpa") or "",
                      "coursework": e.get("coursework") or "", "lines": list(e.get("lines") or [])},
        "roles": [{"id": rid, "title": r.get("title") or "", "org": r.get("org") or "", "org_link": "", "location": r.get("location") or "",
                   "dates": r.get("dates") or "", "base": list(r.get("bullets") or [])} for r, rid in zip(draft.get("roles") or [], ids)],
        "projects": [{"name": p.get("name") or "", "link": p.get("link") or "", "tech": p.get("tech") or "", "dates": p.get("dates") or "",
                      "bullets": list(p.get("bullets") or [])} for p in (draft.get("projects") or [])],
        "skills": [{"label": s.get("label") or "", "terms": s.get("terms") or ""} for s in (draft.get("skills") or [])],
        "honors": list(draft.get("honors") or []),
        "name": h.get("name") or "", "school": e.get("school") or "", "school_location": e.get("location") or "",
    }


async def draft_skeleton(text: str, career: dict) -> dict:
    """One model call: the uploaded text to a SkeletonDraft, then to the
    skeleton's shape with the record's exp ids and the numeral flags."""
    from .llm import get_llm

    if len(text.strip()) < 80:
        raise ValueError("that file has almost no text — a scanned image PDF needs OCR first")
    llm = get_llm("main")
    draft = await llm.parse([{"type": "text", "text": _INTAKE_SYSTEM}], "RÉSUMÉ TEXT:\n\n" + text[:40000], SkeletonDraft, effort="medium")
    d = draft.model_dump()
    return {"skeleton": draft_to_skeleton(d, career), "flags": numeral_flags(d, text), "chars": len(text), "model": f"{llm.name}:{llm.model}"}


async def draft_from_file(path: Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """`resume-tailor skeleton draft <file>`: the draft for a résumé on disk."""
    path = Path(path).expanduser()
    text = extract_text(path.name, path.read_bytes())
    career = plain(load_rt(career_path(profile_dir)))
    return await draft_skeleton(text, career)


# --- processes -----------------------------------------------------------------

_INHERITED = {"RESUME_TAILOR_PROFILE", "RESUME_TAILOR_OUTPUT"}


def child_env() -> dict[str, str]:
    """The environment a process started from the dashboard should get: this
    process's, minus every tool variable and API key it read from the env
    file at its own start — the child reads the file afresh, so a key or
    model changed on the page reaches it. Only the two variables that say
    where the files are pass through."""
    return {k: v for k, v in os.environ.items()
            if not ((k.startswith("RESUME_TAILOR_") and k not in _INHERITED) or k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"))}


def _pid_alive(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def workers_snapshot(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """How the loop is running now, and whether the config files changed
    since its workers started (in which case they run on the old values)."""
    pid_files = sorted(Path(profile_dir).glob("watch-w*.pid"))
    if not pid_files and (Path(profile_dir) / "watch.pid").is_file():
        pid_files = [Path(profile_dir) / "watch.pid"]
    alive = [p for p in pid_files if _pid_alive(p)]
    started = min((p.stat().st_mtime for p in pid_files), default=None)
    watched = {"env": env_path(profile_dir), "answers.yaml": answers_path(profile_dir), "career.yaml": career_path(profile_dir),
               "resume/base.yaml": base_path(profile_dir)}
    stale = [name for name, p in watched.items() if started and p.is_file() and p.stat().st_mtime > started]
    fresh = _pid_alive(Path(profile_dir) / "fresh.pid") if (Path(profile_dir) / "fresh.pid").is_file() else None
    supervisor = _pid_alive(Path(profile_dir) / "overnight.pid") if (Path(profile_dir) / "overnight.pid").is_file() else None
    return {"workers": len(pid_files), "alive": len(alive), "fresh": bool(fresh), "supervisor_pid": supervisor,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)) if started else None, "stale": stale}


def restart_workers(out_dir: Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """Stop every worker and the fresh lane so they come back on the files as
    they are now. With the overnight supervisor running, that is all: it
    relaunches whatever is missing within five minutes. Without it, the same
    number of workers (and the fresh lane, if it ran) are started here."""
    import argparse

    from . import cli

    snap = workers_snapshot(profile_dir)
    cli.cmd_stop(argparse.Namespace())
    if snap["supervisor_pid"]:
        return {"stopped": snap["alive"], "relaunch": "supervisor",
                "note": "The overnight supervisor brings the workers back within five minutes, on the new settings."}
    n = max(1, snap["workers"] or 1)
    cmd = [sys.executable, "-m", "resume_tailor.cli", "start", "--workers", str(n), "--out", str(out_dir)]
    if snap["fresh"]:
        cmd.append("--fresh")
    log = open(Path(profile_dir) / "watch.log", "a", buffering=1)
    proc = subprocess.run(cmd, cwd=Path(out_dir).parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          env=child_env(), timeout=120)
    return {"stopped": snap["alive"], "relaunch": "started", "workers": n, "fresh": snap["fresh"], "exit": proc.returncode,
            "note": f"Started {n} worker{'s' if n > 1 else ''}{' and the fresh lane' if snap['fresh'] else ''} on the new settings."}


def _kill_pid_file(path: Path, sig: int = signal.SIGTERM) -> int | None:
    """Signal the process a pid file names, if it still lives, and drop the
    file either way: left behind, a stale one reads as "running"."""
    pid = _pid_alive(path)
    if pid:
        try:
            os.kill(pid, sig)
        except OSError:
            pid = None
    path.unlink(missing_ok=True)
    return pid


def _gone(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # reap it when it was started from this process
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


def stop_workers(profile_dir: Path = DEFAULT_PROFILE_DIR, stop_loop=None) -> dict:
    """The Stop button: stop applying. The overnight supervisor goes first and
    is waited for (alive, it relaunches within five minutes whatever is
    stopped next, and mid-`start` it could spawn a worker after the stop),
    then every worker and the fresh lane through `resume-tailor stop`, then
    the caffeinate that kept the machine awake for it. The mail and Instagram
    lanes only read the inbox and the stories; they are left running.
    `stop_loop` is the worker stop itself, replaceable by a test so no test
    ever touches the real loop."""
    import argparse

    from . import cli

    snap = workers_snapshot(profile_dir)
    if launch_agent_path().is_file():
        # Under launchd: boot the agent out (that ends the supervisor) and
        # disable it, or KeepAlive relaunches within the minute and the next
        # login starts it again. Start enables it back.
        _launchctl("bootout", f"{_domain()}/{LAUNCHD_LABEL}")
        _launchctl("disable", f"{_domain()}/{LAUNCHD_LABEL}")
    supervisor = _kill_pid_file(Path(profile_dir) / "overnight.pid")
    for _ in range(20):
        if not supervisor or _gone(supervisor):
            break
        time.sleep(0.25)
    _kill_pid_file(Path(profile_dir) / "caffeinate.pid")
    (stop_loop or (lambda: cli.cmd_stop(argparse.Namespace())))()
    what = [f"{snap['alive']} worker{'' if snap['alive'] == 1 else 's'}"]
    if snap["fresh"]:
        what.append("the fresh lane")
    if supervisor:
        what.append("the supervisor")
    return {"stopped": snap["alive"], "fresh": snap["fresh"], "supervisor": bool(supervisor),
            "note": "Stopped " + ", ".join(what) + ". Nothing applies until Start."}


SUPERVISOR_SCRIPT = "overnight.sh"   # the owner's supervisor, beside the profile files, when there is one
DEFAULT_WORKERS = 4
LAUNCHD_LABEL = "com.resume-tailor.loop"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _plist(script: Path, out_dir: Path, profile_dir: Path) -> str:
    log = Path(profile_dir) / "overnight.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>{script}</string></array>
  <key>WorkingDirectory</key><string>{Path(out_dir).parent}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>{Path.home()}</string>
  </dict>
</dict>
</plist>
"""


def autostart(on: bool, out_dir: Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """The loop as a launchd agent: `on` installs ~/Library/LaunchAgents/
    com.resume-tailor.loop.plist, which runs the supervisor script at every
    login and brings it back within a minute whenever it dies, and starts it
    now; `off` stops it and removes the file. Stop and Start on the dashboard
    stay in charge: Stop boots the agent out and disables it until Start
    enables and bootstraps it again, so a stopped loop stays stopped across
    a logout. Idle sleep is held off by the script's own caffeinate; a closed
    lid or a power cut is not, so the machine stays plugged in and open."""
    plist = launch_agent_path()
    if not on:
        if plist.is_file():
            _launchctl("bootout", f"{_domain()}/{LAUNCHD_LABEL}")
            plist.unlink()
        return {"autostart": False, "note": "The launchd agent is removed. Start on the dashboard still runs the supervisor by hand."}
    script = Path(profile_dir) / SUPERVISOR_SCRIPT
    if not script.is_file():
        raise ValueError(f"no supervisor script at {script}: autostart runs that script")
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(_plist(script, Path(out_dir), Path(profile_dir)))
    _launchctl("enable", f"{_domain()}/{LAUNCHD_LABEL}")
    r = _launchctl("bootstrap", _domain(), str(plist))
    if r.returncode != 0:
        r = _launchctl("kickstart", "-k", f"{_domain()}/{LAUNCHD_LABEL}")  # already loaded: restart it on the new file
    return {"autostart": True, "plist": str(plist), "started": r.returncode == 0,
            "note": "The loop now starts at every login and comes back on its own if it dies; Stop on the dashboard holds it off until Start."
                    if r.returncode == 0 else f"the agent is installed but launchctl said: {(r.stderr or r.stdout).strip()[:160]}"}


def autostart_status() -> dict:
    plist = launch_agent_path()
    loaded = plist.is_file() and _launchctl("print", f"{_domain()}/{LAUNCHD_LABEL}").returncode == 0
    return {"installed": plist.is_file(), "loaded": loaded, "plist": str(plist)}


def start_workers(out_dir: Path, profile_dir: Path = DEFAULT_PROFILE_DIR, workers: int | None = None) -> dict:
    """The Start button. With the owner's supervisor script beside the profile
    files (`overnight.sh`: the workers, the fresh lane, the mail and Instagram
    lanes, each relaunched when it dies) it is started detached, logging to
    `overnight.log`, and does the rest; without one, `resume-tailor start`
    runs the given number of workers and the fresh lane. Pressed while the
    loop runs it changes nothing: the supervisor's first act is to stop the
    workers, which would abandon an application in progress."""
    snap = workers_snapshot(profile_dir)
    if snap["supervisor_pid"] or snap["alive"]:
        return {"started": False, "note": f"Already running: {snap['alive']} worker{'' if snap['alive'] == 1 else 's'}"
                + (" and the fresh lane" if snap["fresh"] else "") + (" under the supervisor" if snap["supervisor_pid"] else "") + "."}
    script = Path(profile_dir) / SUPERVISOR_SCRIPT
    if launch_agent_path().is_file() and script.is_file():
        _launchctl("enable", f"{_domain()}/{LAUNCHD_LABEL}")
        r = _launchctl("bootstrap", _domain(), str(launch_agent_path()))
        if r.returncode != 0:
            r = _launchctl("kickstart", f"{_domain()}/{LAUNCHD_LABEL}")
        return {"started": r.returncode == 0, "launcher": "launchd",
                "note": "The supervisor is up under launchd; the workers and the fresh lane follow within a moment, and it comes back on its own if it dies."
                        if r.returncode == 0 else f"launchctl said: {(r.stderr or r.stdout).strip()[:160]}"}
    if script.is_file():
        with open(Path(profile_dir) / "overnight.log", "a", buffering=1) as log:
            proc = subprocess.Popen(["bash", str(script)], cwd=Path(out_dir).parent, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=child_env(), start_new_session=True)
        return {"started": True, "launcher": "supervisor", "pid": proc.pid,
                "note": "The supervisor is up; the workers and the fresh lane follow within a moment."}
    n = max(1, workers or DEFAULT_WORKERS)
    cmd = [sys.executable, "-m", "resume_tailor.cli", "start", "--workers", str(n), "--fresh", "--out", str(out_dir)]
    with open(Path(profile_dir) / "watch.log", "a", buffering=1) as log:
        proc = subprocess.run(cmd, cwd=Path(out_dir).parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              env=child_env(), timeout=120)
    ok = proc.returncode == 0
    return {"started": ok, "launcher": "start", "workers": n, "exit": proc.returncode,
            "note": f"Started {n} worker{'s' if n > 1 else ''} and the fresh lane." if ok else f"start exited {proc.returncode}; see watch.log."}


def run_intake(upload: Path, out_dir: Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """The intake in a child process with a clean environment, so the model
    and key just saved on the page are the ones used."""
    cmd = [sys.executable, "-m", "resume_tailor.cli", "skeleton", "draft", str(upload), "--json"]
    env = child_env()
    if str(profile_dir) != str(DEFAULT_PROFILE_DIR):
        env["RESUME_TAILOR_PROFILE"] = str(profile_dir)
    proc = subprocess.run(cmd, cwd=Path(out_dir).parent, capture_output=True, text=True, env=env, timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("the intake failed: " + (" ".join(tail) or f"exit {proc.returncode}"))
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        raise RuntimeError(f"the intake returned no draft: {(proc.stdout or '')[-300:]}") from e


# --- the whole view -------------------------------------------------------------

def settings_view(out_dir: Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    answers = plain(load_rt(answers_path(profile_dir)))
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile_dir": str(profile_dir),
        "env": env_view(profile_dir),
        "sources": sources_view(answers, Path(out_dir)),
        "answers": answers_view(profile_dir),
        "basics": basics_view(profile_dir),
        "resume": skeleton_view(profile_dir),
        "workers": workers_snapshot(profile_dir),
    }
