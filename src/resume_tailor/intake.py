"""The facts an application form can ask that only the applicant knows,
collected once, before any driving starts.

Why this exists: an agent filling forms in the user's Chrome (the
chrome-apply skill) stalled every few postings on a question like "zip
code?", "college within the university?", "date of birth?" or "related to
an employee?". Each stall cost the user a round trip and stopped the loop.
`resume-tailor intake` walks this list, skips every fact the record already
holds (answers.yaml, career.yaml, hand_answers, an earlier intake), asks
only the rest, and writes the answers to answers.yaml → facts. Profile.lookup
flattens answers.yaml, so the bank (`answer_form_question`) finds them by
their key words; the MCP tool `intake_facts` hands the agent the whole set
in one call before it starts a batch.

A fact is never guessed here. `--check` lists what is missing without
asking, so a session can tell the user what to answer before it drives.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass(frozen=True)
class Fact:
    key: str             # stored as answers.yaml → facts.<key>; words in it are what the bank matches on
    question: str        # what the user is asked
    paths: tuple = ()    # dotted paths in answers.yaml / career.yaml that already answer it
    example: str = ""
    group: str = "other"


FACTS: tuple[Fact, ...] = (
    # identity
    Fact("legal_first_name", "Legal first name", ("personal.first_name",), "Jane", "identity"),
    Fact("legal_last_name", "Legal last name (as on your ID)", ("personal.last_name",), "Doe", "identity"),
    Fact("preferred_first_name", "Preferred first name (blank if same)", (), "Jay", "identity"),
    Fact("email", "Email used on applications", ("personal.email",), "you@school.edu", "identity"),
    Fact("phone", "Phone number", ("personal.phone",), "9105550100", "identity"),
    Fact("linkedin_url", "LinkedIn URL", ("personal.linkedin",), "https://www.linkedin.com/in/you", "identity"),
    Fact("github_url", "GitHub URL", ("personal.github",), "https://github.com/you", "identity"),
    Fact("date_of_birth", "Date of birth (Month Day, Year)", (), "January 1, 2005", "identity"),
    Fact("signature_name", "Name to type where a form asks for a signature", (), "Jane Doe", "identity"),
    # addresses
    Fact("current_address_street", "Current street address", ("address.street",), "100 Main St", "address"),
    Fact("current_address_city", "Current city", ("address.city",), "Springfield", "address"),
    Fact("current_address_state", "Current state (2 letters)", ("address.state",), "IL", "address"),
    Fact("current_address_zip", "Current zip code", ("address.zip",), "62701", "address"),
    Fact("permanent_address_street", "Permanent home street address (where you live outside school)", (), "1 Elm St", "address"),
    Fact("permanent_address_city", "Permanent home city", (), "Springfield", "address"),
    Fact("permanent_address_state", "Permanent home state", (), "IL", "address"),
    Fact("permanent_address_zip", "Permanent home zip code", (), "62704", "address"),
    # education
    Fact("university", "University", ("education.school", "education.university"), "State University", "education"),
    Fact("college_within_university", "College / school within the university", (), "College of Arts and Sciences", "education"),
    Fact("degree", "Degree in progress", ("education.degree", "education.degree_level"), "Bachelor of Science", "education"),
    Fact("majors", "Major(s)", ("education.major", "education.discipline"), "Computer Science", "education"),
    Fact("gpa", "Cumulative GPA (as you report it)", ("education.gpa",), "3.5", "education"),
    Fact("education_start", "Start of the degree (Month Year)", ("education.start_date",), "August 2024", "education"),
    Fact("graduation_date", "Expected graduation (Month Year)", ("education.expected_graduation", "education.graduation_date"), "May 2028", "education"),
    Fact("high_school", "High school name and city", (), "Springfield High School, Springfield", "education"),
    Fact("high_school_graduation_year", "High school graduation year", ("education.high_school_graduation_year",), "2023", "education"),
    # work and status
    Fact("us_work_authorized", "Authorized to work in the US? (Yes/No)", ("work_authorization.us_work_authorization",), "Yes", "work"),
    Fact("needs_sponsorship", "Need visa sponsorship now or later? (Yes/No)", ("work_authorization.requires_us_sponsorship",), "No", "work"),
    Fact("citizenship_status", "Citizenship / status (citizen, permanent resident, visa type)", ("work_authorization.citizenship_status",), "US citizen", "work"),
    Fact("most_recent_employer", "Current or most recent employer", (), "Acme Corp", "work"),
    Fact("most_recent_job_title", "Current or most recent job title", (), "Software Engineer Intern", "work"),
    Fact("previous_internship_companies", "Companies you've interned at (comma-separated)", (), "Acme Corp, Campus IT", "work"),
    Fact("ever_worked_at_target_company", "Default answer to 'have you worked here before?'", (), "No", "work"),
    Fact("related_to_employees", "Related to anyone at the companies you apply to?", (), "No", "work"),
    Fact("offer_deadlines", "Any active offer deadlines? (No, or company + date)", (), "No", "work"),
    # availability
    Fact("internship_availability_dates", "Internship availability (start – end)", (), "May 2027 – Aug 2027", "availability"),
    Fact("can_start_within_3_weeks", "Can start within 3 weeks of an offer? (Yes/No)", (), "Yes", "availability"),
    Fact("full_time_start_date", "Earliest full-time start (Month Year)", ("availability.earliest_full_time_start_date",), "June 2028", "availability"),
    Fact("willing_to_relocate", "Willing to relocate / work on-site anywhere? (Yes/No)", (), "Yes", "availability"),
    Fact("need_housing", "Need housing for an out-of-town internship? (Yes/No)", (), "Yes", "availability"),
    Fact("hourly_compensation_expectation", "Hourly pay expectation (number only)", ("salary_expectations.target_compensation",), "35", "availability"),
    # preferences
    Fact("top_programming_languages", "Top 3 programming languages", (), "Python, TypeScript/JavaScript, C++", "preferences"),
    Fact("areas_of_interest", "Areas of interest (up to 3)", (), "Backend, Full-stack, Distributed systems", "preferences"),
    Fact("self_rating_programming", "Self-rating for programming / DSA (Limited, Demonstrated, Extensive, Expert)", (), "Extensive", "preferences"),
    Fact("clubs_and_organizations", "Clubs / organizations", (), "ACM chapter", "preferences"),
    Fact("how_did_you_hear_default", "Default 'how did you hear about us' (Job board, Recruiter, Career fair…)", (), "Job board", "preferences"),
    # demographics (voluntary on every form; the default is to decline)
    Fact("gender", "Gender for EEO questions (or 'Decline')", (), "Decline", "demographics"),
    Fact("race_ethnicity", "Race / ethnicity for EEO questions (or 'Decline')", (), "Decline", "demographics"),
    Fact("veteran_status", "Veteran status (or 'Decline')", (), "Decline", "demographics"),
    Fact("disability_status", "Disability status (or 'Decline')", (), "Decline", "demographics"),
)

_EMPTY = (None, "", [], {})


def _get(data: Any, dotted: str) -> Any:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _hand_answer(answers: dict, fact: Fact) -> str | None:
    """An answer the user gave in an earlier session (hand_answers) whose
    question shares the fact's key words."""
    words = set(fact.key.split("_")) - {"and", "or", "to", "at", "the", "default"}
    for item in answers.get("hand_answers") or []:
        if not isinstance(item, dict):
            continue
        q = set(re.sub(r"[^a-z0-9 ]+", " ", str(item.get("question", "")).lower()).split())
        if words and len(words & q) >= max(2, len(words) - 1) and str(item.get("answer", "")).strip():
            return str(item["answer"])
    return None


def known(fact: Fact, answers: dict, career: dict) -> str | None:
    """What the record already says for this fact, or None."""
    v = _get(answers, f"facts.{fact.key}")
    if v not in _EMPTY:
        return str(v)
    for p in fact.paths:
        for src in (answers, career):
            v = _get(src, p)
            if v not in _EMPTY:
                return str(v) if not isinstance(v, (list, dict)) else yaml.safe_dump(v).strip()
    return _hand_answer(answers, fact)


def _load(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}


def status(root: str | Path) -> dict[str, Any]:
    """Every fact with its value (or None), and the keys still missing."""
    root = Path(root).expanduser()
    answers, career = _load(root / "answers.yaml"), _load(root / "career.yaml")
    rows = [{"key": f.key, "group": f.group, "question": f.question, "value": known(f, answers, career)} for f in FACTS]
    return {"facts": rows, "missing": [r["key"] for r in rows if r["value"] is None]}


def facts_map(root: str | Path) -> dict[str, str]:
    """key → value for every fact the record holds (for the agent)."""
    return {r["key"]: r["value"] for r in status(root)["facts"] if r["value"] is not None}


def save(root: str | Path, values: dict[str, str]) -> Path:
    """Write answers to answers.yaml → facts, comments kept (a .bak- first)."""
    from .settings import load_rt, save_rt, scalar

    path = Path(root).expanduser() / "answers.yaml"
    data = load_rt(path)
    facts = data.get("facts")
    if facts is None:
        from ruamel.yaml.comments import CommentedMap

        facts = CommentedMap()
        data["facts"] = facts
        data.yaml_set_comment_before_after_key("facts", before=(
            "Facts only you know, collected by `resume-tailor intake` before any driving,\n"
            "so the agent never stops a run to ask. The bank matches form labels against these keys."))
    for k, v in values.items():
        facts[k] = scalar(str(v))
    facts["updated"] = time.strftime("%Y-%m-%d")
    save_rt(path, data)
    return path


def run(root: str | Path, ask: Callable[[str], str] = input, only_missing: bool = True) -> dict[str, str]:
    """Ask for every missing fact (Enter keeps a fact blank for later) and
    save the answers. Returns what was saved."""
    st = status(root)
    todo = [f for f in FACTS if (not only_missing) or f.key in st["missing"]]
    got: dict[str, str] = {}
    group = None
    for f in todo:
        if f.group != group:
            group = f.group
            print(f"\n— {group} —")
        ans = ask(f"{f.question}" + (f"  e.g. {f.example}" if f.example else "") + ": ").strip()
        if ans:
            got[f.key] = ans
    if got:
        save(root, got)
    return got
