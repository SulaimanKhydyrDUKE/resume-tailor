"""Two independent readings of the finished resume against the posting, both
of which must pass before anything is submitted.

Each judge sees only the posting and the resume text as a parser would extract
it — not the career record, not the other judge's verdict — and the two are
asked different questions. One reads as the hiring manager deciding whether to
give a first interview; the other as the screener checking every stated
requirement literally. A resume that satisfies the screener but not the manager
is keyword-stuffed; one that satisfies the manager but not the screener is
missing a stated must-have. Either way it is held, with the reasons, not sent.
"""
from __future__ import annotations

import asyncio
import re

from .models import FitVerdict

# Both judges must reach this before anything is submitted. `search.min_judge_score`
# in answers.yaml overrides it; a résumé that falls short is revised from the
# judges' notes and judged again (see batch._process_one) before it is held.
PASS_SCORE = 90

_STRICT = (
    " Score strictly, on a scale where 90 and above means every stated must-have is plainly "
    "evidenced in the résumé text and the résumé reads as written for this role; 80-89 is a solid "
    "but generic fit; below 70 means a stated must-have is missing. Never round up, and give no "
    "credit for what a résumé implies but does not show. Name each unmet must-have precisely — "
    "the résumé will be revised from your notes. One exception: a requirement phrased as the "
    "ability or willingness to learn, be trained on, or pick up a named tool (\"ability to quickly "
    "learn Power BI\") is met by any candidate whose résumé shows several technologies learned "
    "across roles; it is never an unmet must-have and never a reason to hold. Mark disqualified ONLY "
    "for an eligibility barrier — citizenship or clearance, work authorization, degree level, graduation "
    "window; a missing skill or tool, or fewer years than asked, lowers the score and nothing more."
)

LENSES = {
    "hiring manager": (
        "You are the engineering manager who opened this internship posting, reading a resume "
        "to decide whether to give the candidate a first-round interview. Judge substance and "
        "relevance: does the experience shown actually bear on the work described? Ignore "
        "keyword density. Be honest about weak fits — an interview slot is a real cost." + _STRICT
    ),
    "screener": (
        "You are a recruiter screening resumes against this posting's stated requirements, "
        "one by one and literally. For each must-have — degree and level, graduation window, "
        "citizenship or clearance, required skills, required years — say whether the resume "
        "shows it. A requirement the resume cannot meet at all is disqualifying. Do not "
        "reward enthusiasm or writing quality; this pass is about stated requirements only." + _STRICT
    ),
}


async def judge(posting_text: str, resume_text: str, lens: str, facts: str = "") -> FitVerdict:
    from .tailor import _parse

    return await _parse(
        [{"type": "text", "text": LENSES[lens]}],
        f"""JOB POSTING
-----------
{posting_text[:12000]}

RESUME (text as extracted from the PDF)
-----------------------------------
{resume_text[:12000]}

APPLICANT FACTS (supplied on the application form; a resume does not carry these)
-----------------------------------
{facts or '(none supplied)'}

Treat the applicant facts as established. A location requirement is met by an \
applicant willing to relocate there, or to work remotely where the posting allows \
it; hold for location only when the posting requires current residence and the \
facts do not satisfy it. Do not hold for work authorization, sponsorship, or \
graduation timing when the facts resolve them.

Judge the match. Score 0-100, list unmet stated must-haves, mark disqualified only \
for a requirement the resume and facts together clearly cannot satisfy, and give \
a verdict.""",
        FitVerdict, effort="medium", role="judge",
    )


Bar = int | dict[str, int]  # one score for both judges, or one per judge by name


def bar_for(min_score: Bar, name: str) -> int:
    """The score a given judge must reach. `search.min_judge_score` may be
    one number or a mapping like {hiring manager: 85, screener: 90}."""
    if isinstance(min_score, dict):
        for key, val in min_score.items():
            if str(key).strip().lower() == name.lower():
                return int(val)
        return int(min_score.get("default", PASS_SCORE))
    return int(min_score)


def passes(v: FitVerdict, min_score: Bar = PASS_SCORE, name: str = "") -> bool:
    """A judge's pass: nothing the candidate clearly cannot meet, and a score
    at or above that judge's bar. The judge's own submit/hold recommendation
    is advisory — the user decided that a solid score applies."""
    return not v.disqualified and v.score >= bar_for(min_score, name)


async def two_independent(posting_text: str, resume_text: str, facts: str = "",
                          min_score: Bar = PASS_SCORE) -> tuple[bool, dict[str, FitVerdict]]:
    names = list(LENSES)
    verdicts = await asyncio.gather(*(judge(posting_text, resume_text, n, facts) for n in names))
    by_name = dict(zip(names, verdicts))
    return all(passes(v, min_score, n) for n, v in by_name.items()), by_name


def summarize(verdicts: dict[str, FitVerdict], min_score: Bar = PASS_SCORE) -> str:
    return " | ".join(f"{n} {v.score}/100 {'pass' if passes(v, min_score, n) else 'HOLD'}" for n, v in verdicts.items())


def reasons(verdicts: dict[str, FitVerdict], min_score: Bar = PASS_SCORE) -> str:
    out = []
    for n, v in verdicts.items():
        if not passes(v, min_score, n):
            why = v.reason.strip()
            if v.unmet_hard_requirements:
                why += " Unmet: " + "; ".join(v.unmet_hard_requirements[:4])
            out.append(f"{n}: {why}")
    return " || ".join(out)


def weakest(verdicts: dict[str, FitVerdict]) -> int:
    return min((v.score for v in verdicts.values()), default=0)


_ELIGIBILITY = re.compile(r"citizen|clearance|authori[sz]|sponsor|visa|degree|master|ph\.?d|doctora|graduat|enrol|"
                          r"\bterm\b|semester|fall 20|spring 20|winter 20|availab|location|reside|relocat", re.I)


def disqualified(verdicts: dict[str, FitVerdict]) -> bool:
    """A requirement no rewrite can meet — citizenship, degree level, a
    graduation window. Revising the résumé does not help; stop. The flag
    alone is not enough: a judge once set it under a verdict that called the
    candidate a solid fit, so it counts only when the same judge names an
    eligibility requirement as unmet."""
    return any(v.disqualified and any(_ELIGIBILITY.search(u) for u in (v.unmet_hard_requirements or []))
               for v in verdicts.values())


def revision_notes(verdicts: dict[str, FitVerdict]) -> str:
    """What both judges want changed, for the next tailoring pass: every
    unmet must-have and each judge's reason, from the passing judge too."""
    out = []
    for n, v in verdicts.items():
        note = f"{n} scored {v.score}/100: {v.reason.strip()}"
        if v.unmet_hard_requirements:
            note += " Unmet: " + "; ".join(v.unmet_hard_requirements[:5])
        out.append(note)
    return "\n".join(out)


def cached_scores_ok(fit: str, min_score: Bar = PASS_SCORE) -> bool:
    """Whether a recorded verdict line ("hiring manager 92/100 pass | screener
    85/100 HOLD") meets today's bar, judge by judge — the bar may have moved
    since it was written, so the scores decide, not the pass/HOLD word."""
    import re

    pairs = re.findall(r"([a-z ]+?)\s+(\d+)/100", (fit or "").lower())
    return bool(pairs) and all(int(score) >= bar_for(min_score, name.strip()) for name, score in pairs)
