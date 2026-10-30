"""Which submitted answers deserve a second look.

Every attempt records the answers it gave (batch-state.json → done[id].answers:
question, answer, source). This reads them back against the facts that cost
an application when wrong — work authorization, sponsorship, citizenship,
relocation, start dates, GPA, degree level, years of experience, "how did you
hear" — and lists each answer that looks wrong or unsupported, one row per
answer, so the owner can open them on the dashboard and fix the rule that
produced them. It reads the record and never changes it; the loop's own rules
(sponsorship_answer, gpa_answer, relocation_answer, the planner's rails) are
where a finding gets fixed.

The checks are deliberately literal: the question's words and the answer's,
with the bank's own values where a fact has one (the GPA, whether the
candidate is a citizen). A row is a flag, not a verdict — "Bachelor's" to
"highest degree completed" is a misstatement on a form that verifies and the
expected answer on one that means "pursuing"."""
from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Callable

from .planner import NAMED_SOURCE

_AUTH_Q = re.compile(r"authori[sz]ed|legally (able|permitted|eligible)|eligible to work|right to work")
_AUTH_NOT = re.compile(r"only temporarily|require sponsorship|need (the company|us|sponsorship)|will you (now|require)|do you need|sponsor a|"
                       r"if you are not a")  # "if you are not a U.S. citizen…, are you authorized": conditional, not a claim
_SPONSOR_Q = re.compile(r"require\b[^?]{0,60}sponsor|need\b[^?]{0,60}sponsor|sponsor[^?]{0,40}\b(now|future)")
_START_Q = re.compile(r"desired start|available to start|date available|earliest (start|date)|available to begin|when (can|could) you start")
_YEARS_Q = re.compile(r"years? of (industry|professional|relevant|work|related|full-time)")
_DEGREE_DONE_Q = re.compile(r"(achieved|completed|obtained|attained|earned)")
_TERM = re.compile(r"\b(spring|summer|fall|autumn|winter)\s+((?:19|20)\d\d)\b", re.I)
_TERM_END = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 2}
_NO = ("no", "no.", "nope")


def _year_of(text: str) -> int | None:
    m = re.search(r"\b((?:19|20)\d\d)\b", text)
    return int(m.group(1)) if m else None


def _years(text: str) -> float | None:
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    return min(nums) if nums else None


def _past_term(question: str, today: date) -> bool:
    """"Summer 2026" confirmed in October 2026: a term already over."""
    m = _TERM.search(question)
    if not m:
        return False
    year, month = int(m.group(2)), _TERM_END[m.group(1).lower()]
    return date(year, month, 28) < today


def facts_from(profile) -> dict:
    """What the bank says, for the checks that compare against it."""
    flat = profile.flat_answers() if profile is not None else {}
    status = str(flat.get("work_authorization.citizenship_status") or "").lower()
    m = re.search(r"\d\.\d+", str(flat.get("education.gpa") or flat.get("education.cumulative_gpa") or ""))
    return {"gpa": float(m.group(0)) if m else None,
            "citizen": bool(re.search(r"\bcitizen\b", status)) and "not" not in status,
            "visa_holder": bool(re.search(r"temporary|\b(f-?1|opt|cpt|h-?1b|j-?1|tn)\b", status))}


def checks(facts: dict, today: date) -> list[tuple[str, Callable[[str, str], bool]]]:
    """(kind, flagged?(question lower, answer)) — each the shape of one mistake
    the record has shown, in the order the owner should read them."""
    gpa = facts.get("gpa")
    return [
        ("authorized answered No", lambda q, a: bool(_AUTH_Q.search(q)) and not _AUTH_NOT.search(q) and a.lower() in _NO),
        ("sponsorship answered Yes", lambda q, a: bool(_SPONSOR_Q.search(q)) and not _AUTH_Q.search(q) and a.lower().rstrip(".") == "yes"),
        ("a visa-holder's status", lambda q, a: bool(re.search(r"temporary visa|i (hold|have|am on) (a |an |my )?(f-?1|opt|cpt|h-?1b|visa)|\b(f-?1|cpt|h-?1b)\b", a.lower()))
         and not facts.get("visa_holder")),
        ("citizenship claimed", lambda q, a: "citizen" in q and "countr" in q
         and bool(re.search(r"united states|\bu\.?s\.?a?\b", a.lower())) and not facts.get("citizen")),
        ("relocation answered No", lambda q, a: "relocat" in q and not re.search(r"(require|need)\b[^?]{0,30}relocation (assist|support|package)", q)
         and a.lower() in _NO),
        ("start date in the past", lambda q, a: bool(_START_Q.search(q)) and "education" not in q
         and (_year_of(a) or today.year) < today.year),
        ("GPA above the bank's", lambda q, a: bool(re.search(r"\bgpa\b|grade point", q)) and gpa is not None
         and re.fullmatch(r"\d\.\d+", a.strip()) is not None and float(a) > gpa + 0.001),
        ("degree completed = a degree", lambda q, a: bool(_DEGREE_DONE_Q.search(q)) and bool(re.search(r"education|degree", q))
         and "pursu" not in q and bool(re.search(r"bachelor|master|doctor|phd", a.lower()))),
        ("years of experience above a student's", lambda q, a: bool(_YEARS_Q.search(q)) and (_years(a) or 0) > 2),
        ("required field left unanswered", lambda q, a: a.strip().lower() in ("-- no answer --", "no answer", "--select--", "select one", "select...")),
        ("how did you hear: a named source", lambda q, a: "hear" in q and bool(NAMED_SOURCE.search(re.sub(r"\([^)]*\)", " ", a)))),
        ("confirmed a term already over", lambda q, a: a.lower().rstrip(".") == "yes" and _past_term(q, today)),
    ]


def audit_answers(done: dict, profile=None, today: date | None = None) -> list[dict]:
    """One row per flagged answer across every submitted application, oldest
    first: kind, date, company, record id, the answer's source, question,
    answer, and the dashboard link that opens the application."""
    today = today or date.today()
    rules = checks(facts_from(profile), today)
    rows: list[dict] = []
    for eid, rec in done.items():
        if rec.get("status") != "applied":
            continue
        for a in rec.get("answers") or []:
            q = " ".join(str(a.get("question") or "").split())
            ans = " ".join(str(a.get("answer") or "").split())
            if not q or not ans:
                continue
            ql = q.lower()
            for kind, flagged in rules:
                if flagged(ql, ans):
                    rows.append({"kind": kind, "date": str(rec.get("when") or "")[:10], "company": rec.get("company") or "",
                                 "record": eid, "source": str(a.get("source") or "?"), "question": q, "answer": ans,
                                 "dashboard": f"http://127.0.0.1:8765/#{eid}"})
                    break
    rows.sort(key=lambda r: (r["date"], r["company"], r["kind"]))
    return rows


def write_csv(rows: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "date", "company", "record", "source", "question", "answer", "dashboard"])
        w.writeheader()
        w.writerows(rows)
    return path


def summary(rows: list[dict], applied: int) -> str:
    kinds = Counter(r["kind"] for r in rows)
    apps = len({r["record"] for r in rows})
    lines = [f"{len(rows)} answers across {apps} of {applied} submitted applications deserve a look"]
    width = max((len(k) for k in kinds), default=0)
    for kind, n in kinds.most_common():
        last = max(r["date"] for r in rows if r["kind"] == kind)
        lines.append(f"  {n:4}  {kind:<{width}}  last {last}")
    return "\n".join(lines)
