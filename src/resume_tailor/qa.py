"""Answers to screening questions the answer bank has no exact entry for —
"Are you based in the NYC tri-state area?", "Do you have experience with
JUnit?", "Can you attend onsite orientation in Reston?" — decided from the
profile, or not at all.

Two sources, in order. A question about the candidate's own experience with a
named thing is settled by looking for that thing in the career record: the
record is complete by construction, so its silence on JUnit is a "No", not an
"unknown". Everything else goes to a model that sees the whole profile as a
flat key -> value list plus the record's roles, skills and education, must name
the keys or ids each answer rests on, and may say unknown. An answer whose
basis is not a real key is discarded. What the profile cannot decide — a street
address, an obligation next summer, a reference — stays unanswered and the
posting goes to review, because the alternative is a guess on a form that
becomes a fact about you the moment it is submitted.
"""
from __future__ import annotations

import re

from .apply import _pick_option
from .models import QAResult
from .profile import Profile

_YES_NO = {"yes", "no", "true", "false"}
_PLACEHOLDERS = {"", "select", "select…", "select...", "choose", "please select"}

_EXPERIENCE_Q = re.compile(
    r"^\s*(?:do you|have you|are you)\b.*?\b(?:experience|familiar(?:ity)?|proficien\w*|worked|used|comfortable)\b"
    r".*?\b(?:with|using|in|of)\b\s+(.+?)\s*[?*\s]*$",
    re.I,
)
_SPLIT = re.compile(r",|\bor\b|\band\b|such as|\be\.g\.|/|\(|\)", re.I)

_SYSTEM = (
    "You answer application screening questions on a candidate's behalf, strictly from "
    "their profile. Rules: answer only what the profile decides, and list in `basis` the "
    "profile keys or record ids that decide it. For preferences — onsite, relocation, "
    "travel, hours, start date — use work_preferences, availability and location. For a "
    "question about the candidate's own experience or skills, the record is complete: if "
    "it does not show the thing, the answer is No. The same holds for any past or present "
    "tie to the hiring company — former employee, intern, contractor or team member, a "
    "referral or a relative there: absent from the record means No. For a preference among "
    "offered office locations or teams, the candidate's own city (address, location) comes "
    "first; when work_preferences.open_to_relocation is Yes, every offered location is "
    "acceptable, so a second or alternate preference is any other offered location, and an "
    "N/A option only when nothing else applies. For anything the profile does not cover — a "
    "street address, an obligation, a reference, a document the candidate must produce, a "
    "grade or test score not listed — set unknown to true and leave the answer empty. Never "
    "invent. When options are given, the answer must be one of them, verbatim. When the "
    "field is a search box or picker, answer with the single entry a person would type — a "
    "school name, a city, a degree — not a sentence."
)
ATTEMPTS = 2  # a second sample when the first says unknown to a multiple-choice question


def experience_answer(question: str, record_text: str) -> str | None:
    """'Do you have experience with X?' settled by whether X is in the record."""
    m = _EXPERIENCE_Q.search(question)
    if not m:
        return None
    subject = m.group(1)
    terms = [t.strip(" .,;:'\"") for t in _SPLIT.split(subject)]
    terms = [t for t in terms if len(t) > 1 and t.lower() not in {"a", "an", "the", "any"}]
    if not terms:
        return None
    rec = record_text.lower()
    return "Yes" if any(t.lower() in rec for t in terms) else "No"


def _yes_no_question(options: list[str]) -> bool:
    real = [o.strip().lower() for o in options if o.strip().lower() not in _PLACEHOLDERS]
    return not real or all(o in _YES_NO for o in real)


def _record_summary(profile: Profile) -> str:
    idx = profile.evidence_index()
    lines = [f"{k}: {v}" for k, v in idx.items()
             if k.startswith(("skill", "edu")) or (k.startswith("exp") and "." not in k)]
    return "\n".join(lines)


async def answer(question: str, options: list[str], profile: Profile, picker: bool = False) -> str | None:
    from .tailor import _parse

    index = profile.evidence_index()
    if _yes_no_question(options):
        settled = experience_answer(question, " ".join(index.values()))
        if settled is not None:
            if options:
                i = _pick_option(settled, [{"label": o, "value": o} for o in options])
                return options[i] if i is not None else None
            return settled

    flat = profile.flat_answers()
    if options:
        kind = "multiple choice — the answer must be one option, verbatim"
    elif picker:
        kind = "a search box / picker — answer with the single entry a person would type, not a sentence"
    else:
        kind = "free text"
    prompt = f"""QUESTION
{question}

FIELD
{kind}

OPTIONS
{chr(10).join('- ' + o for o in options) if options else '(none listed)'}

PROFILE (key: value)
{chr(10).join(f'{k}: {v}' for k, v in flat.items())}

RECORD (roles, skills, education)
{_record_summary(profile)}"""
    for attempt in range(ATTEMPTS):
        out = await _parse([{"type": "text", "text": _SYSTEM}], prompt, QAResult, effort="low")
        if out.unknown or not out.answer.strip():
            if options and attempt + 1 < ATTEMPTS:
                continue  # one more sample: the model is stochastic and the choice is closed
            return None
        if not any(b in flat or b in index for b in out.basis):
            return None
        if options:
            i = _pick_option(out.answer, [{"label": o, "value": o} for o in options])
            return options[i] if i is not None else None
        return out.answer.strip()
    return None
