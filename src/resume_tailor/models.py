"""Structured shapes the model is constrained to return.

The governing rule: **no schema field can carry a fact.** Employer, title,
dates, location, degree, institution, contact details and links are copied from
the career YAML by the renderer and never pass through a model call, so they
cannot be corrupted. The model contributes prose and citations only.

Kept free of `default` values and array-length constraints on purpose: those
schema keywords are not accepted by every provider's strict structured-output
mode, and the rules they would express (an outcome may be empty; a bullet must
cite something) are enforced in code instead, where they hold regardless of
which model answered.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- reading the posting --------------------------------------------------

class Requirement(BaseModel):
    text: str = Field(description="The requirement as the posting states it")
    kind: Literal["hard", "preferred"] = Field(description="hard = stated as required, must-have, or minimum")
    keywords: list[str] = Field(description="Exact terms an ATS matches on, spelled as the posting spells them")


class JobSpec(BaseModel):
    role_title: str
    company: str = Field(description="Empty string if the posting does not name one")
    seniority: str
    location: str
    requirements: list[Requirement]
    domain_context: str = Field(description="Industry and product context, one or two sentences")


# --- selecting evidence ---------------------------------------------------

class EvidenceMatch(BaseModel):
    requirement: str
    evidence_ids: list[str] = Field(description="Ids from the evidence index. Empty when nothing supports it.")
    strength: Literal["strong", "adjacent", "none"] = Field(
        description="adjacent = a neighbouring but different technology or domain; a real, reportable outcome"
    )
    reasoning: str


class RolePlan(BaseModel):
    evidence_id: str = Field(description="The exp id this plans for")
    bullet_count: int = Field(description="How many bullets this role earns, 0 to 5")
    focus: str = Field(description="What this role should demonstrate for this posting")


class Selection(BaseModel):
    matches: list[EvidenceMatch]
    role_plans: list[RolePlan] = Field(description="One per experience entry, in the order they should appear")
    include_project_ids: list[str] = Field(
        description="proj ids that earn space for this posting, most relevant first. Empty when none do."
    )
    gaps: list[str] = Field(description="Requirements nothing in the record supports. Report; never paper over.")
    positioning: str = Field(description="One sentence: the angle a reader should come away with")


# --- drafting -------------------------------------------------------------

class Bullet(BaseModel):
    """One resume bullet.

    `outcome` may be an empty string, and often should be: roughly half of real
    source bullets record no outcome, and asking for one anyway is an
    instruction to invent it.
    """

    action: str = Field(description="What you did, leading with the verb. No employer or date — those are rendered.")
    outcome: str = Field(description="The result, ONLY if the record states one. Empty string otherwise.")
    source_fact_ids: list[str] = Field(
        description="Evidence ids this rests on. A bullet that cites nothing is discarded."
    )
    numerals_used: list[str] = Field(
        description="Every number appearing in action+outcome, as written. Empty list if none."
    )
    derived_numerals: list[str] = Field(
        description=(
            "Any number you computed rather than copied (a percentage from two figures, a total from parts). "
            "For each, write 'VALUE from SOURCE ARITHMETIC', e.g. '4x from 200 to 800'. Empty list if none."
        )
    )


class RoleDraft(BaseModel):
    evidence_id: str
    bullets: list[Bullet]


class SkillGroup(BaseModel):
    label: str = Field(description="e.g. Languages, Infrastructure")
    terms: list[str] = Field(description="Only terms the record shows. Never a term merely asked for by the posting.")
    source_fact_ids: list[str]


class SkillsDraft(BaseModel):
    groups: list[SkillGroup]


# --- auditing -------------------------------------------------------------

class AuditVerdict(BaseModel):
    """One claim judged against its cited evidence and nothing else.

    The auditor never sees the job description, so it has no input on which to
    run the rationalisation 'this stretch is relevant'.
    """

    supported: bool = Field(description="Default to false. Only true if the cited text states or plainly entails it.")
    issue: str = Field(description="Empty when supported; otherwise what was asserted beyond the cited text")
    quote: str = Field(description="The words from the cited evidence that support it, verbatim. Empty if none do.")


# --- fit judging, before anything is submitted ------------------------------

class FitVerdict(BaseModel):
    """One independent reading of the finished resume against the posting."""

    score: int = Field(description="0-100: how strong a match this resume is for this posting")
    disqualified: bool = Field(
        description="True ONLY for an eligibility barrier no résumé rewrite could change: citizenship or security "
                    "clearance, work authorization, degree level (Master's/PhD required), or graduation window. "
                    "A missing skill, tool, or fewer years of experience than asked is a low score, never a disqualification."
    )
    unmet_hard_requirements: list[str] = Field(description="Stated must-haves the resume does not show. Empty if none.")
    strengths: list[str] = Field(description="The two or three strongest points of match")
    verdict: Literal["submit", "hold"] = Field(description="submit if you would send this resume to this posting as-is")
    reason: str = Field(description="One or two sentences")


class FreeTextAnswer(BaseModel):
    """An answer to a form's free-text question, with its sources."""

    answer: str = Field(description="First person, plain prose, no headings, within the length the prompt asks for")
    facts_used: list[str] = Field(description="Evidence ids from the index that the answer's factual statements rest on")


class ControlChoice(BaseModel):
    """Which control on a page does a named job — submits the application, or moves it to its next step."""

    id: str = Field(description="The id of the chosen control, or 'none' when no listed control does it")
    reason: str = Field(description="One short clause")


class QAResult(BaseModel):
    """An answer to a screening question, decided from the profile or not at all."""

    answer: str = Field(description="The answer to give — one of the options verbatim when options exist. Empty when unknown.")
    basis: list[str] = Field(description="The profile keys or evidence ids that decide it")
    unknown: bool = Field(description="True when the profile cannot decide this question")
    reasoning: str = Field(description="One sentence")


class FieldAnswer(BaseModel):
    """One planned answer to one question on an application form."""

    id: str = Field(description="The question id from the list, e.g. q7")
    answer: str = Field(description=(
        "What to enter. One option verbatim when options are listed (several, separated by ' | ', only for a "
        "select-all-that-apply list); the single entry a person would type for a picker (a school, a city, a "
        "degree); the value for a text field. Empty when skip or essay is true."))
    basis: list[str] = Field(description="Profile keys or record ids the answer rests on. Empty when skipping.")
    skip: bool = Field(description=(
        "True when the profile cannot honestly decide the question, when it asks about someone other than the "
        "candidate (a referrer, a reference, an emergency contact, an employer of record), or when it is optional "
        "and the profile has nothing for it."))
    essay: bool = Field(description=(
        "True when the question wants prose about the candidate — why this company, a project they are proud of, "
        "a cover letter, anything about them beyond a fact. The essay writer answers those from the record."))
    reason: str = Field(description="One short clause")


class FormPlan(BaseModel):
    """Every question on a form, decided together with the whole form in view."""

    answers: list[FieldAnswer]
