"""The tailoring chain.

  1. read_posting      model, structured   what is actually being asked for
  2. select_evidence   model, structured   which facts answer it, and what does not, and how many
                                            bullets each role earns (cardinality decided here, not
                                            left to the writer)
  3. draft              model, parallel     one call per role + one for skills, each citing evidence
                                            ids and declaring its own numbers
  4. deterministic gates  code, zero tokens   citation existence, numeral provenance scoped to the
                                            bullet's own citations, named-entity allowlist
  5. audit              model, JD-blind     an isolated critic that sees ONE claim plus the verbatim
                                            text of the facts it cites — nothing else. It cannot
                                            rationalise a stretch as "relevant" because it has no
                                            input on which relevance could be computed.
  6. repair              code                a claim that fails is dropped or reduced, never
                                            replaced with a different unverified fact
  7. render + re-verify  code                render to PDF, extract the actual text layer, and run
                                            the same checks again against what a parser would see —
                                            catching anything the render itself corrupted

Two ideas carried over from an independent four-design review, each converged
on from more than one angle: cardinality must come from data rather than a
fixed-slot template, because a template with three job-slots and one real job
is itself an instruction to invent two more; and the auditor's power comes
specifically from not being shown the job description, because relevance is
the excuse fabrication needs and an auditor with no notion of relevance cannot
grant it.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gates
from .compose import document
from .llm import get_llm
from .models import (
    Bullet, JobSpec, RoleDraft, RolePlan, Selection, SkillGroup, SkillsDraft,
)
from .profile import Profile
from .render import extract_pdf_text, page_count, render_pdf_async, render_to_fit_async  # noqa: F401

MAX_REPAIR_ROUNDS = 1


@dataclass
class TailorResult:
    pdf_path: Path
    html: str
    job: JobSpec
    selection: Selection
    coverage: dict[str, Any]
    pages: int
    roles_included: int = 0
    dropped_claims: list[str] = field(default_factory=list)
    render_violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pdf_path": str(self.pdf_path),
            "role": self.job.role_title,
            "company": self.job.company,
            "positioning": self.selection.positioning,
            "pages": self.pages,
            "roles_included": self.roles_included,
            "keyword_coverage": self.coverage,
            "gaps": self.selection.gaps,
            "claims_dropped_by_audit": self.dropped_claims,
            "render_check_failures": self.render_violations,
            "warnings": self.warnings,
        }


_SYSTEM_ROLE = """You are an expert resume writer working strictly from a career \
record. You never assert anything the record does not contain — you rephrase, \
compress, combine, and re-emphasise what is there, and nothing else."""


def _career_system(profile: Profile) -> list[dict]:
    """Stable prefix — identical across every stage of every job, so after the
    first call the career record is served from cache rather than re-read."""
    evidence = "\n".join(f"{k}: {v}" for k, v in profile.evidence_index().items())
    return [
        {"type": "text", "text": _SYSTEM_ROLE},
        {
            "type": "text",
            "text": f"# EVIDENCE INDEX — cite these ids, and only these\n\n{evidence}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


async def _parse(system: list[dict], prompt: str, schema, effort: str = "high", role: str = "main"):
    """One structured call. Which model answers is the provider layer's decision
    (RESUME_TAILOR_PROVIDER, and RESUME_TAILOR_AUDIT_MODEL for role="audit");
    the chain is identical either way."""
    return await get_llm(role).parse(system, prompt, schema, effort)


# --- stage 1 ---------------------------------------------------------------

async def read_posting(jd: str) -> JobSpec:
    return await _parse(
        [{"type": "text", "text": "You extract structured requirements from job postings. "
                                   "Ignore boilerplate: benefits, EEO statements, company blurb."}],
        f"""Extract what this posting actually asks for. Separate hard requirements \
(stated as required, must-have, minimum) from preferred ones. Record the exact \
terms an applicant tracking system would keyword-match on, spelled the way the \
posting spells them — "Kubernetes" stays "Kubernetes", not "k8s". Keywords are \
skills, tools, technologies and named qualifications only: never dates, \
graduation windows, degree abbreviations, or filler like "related field" — no \
resume matches those verbatim, and they only distort the coverage score.

JOB POSTING
-----------
{jd}""",
        JobSpec, effort="medium",
    )


# --- stage 2 -----------------------------------------------------------

def _revision_block(revision: str | None, what: str) -> str:
    if not revision:
        return ""
    return f"""

REVISION — a previous résumé for this posting was judged and held. The judges said:
{revision}

{what} Where the record has nothing for a point, leave the gap and say so — \
never invent, stretch, or rename experience to match."""


def _guidance_block(profile: Profile) -> str:
    """The candidate's own standing instructions for how the record should be
    read (`tailoring_guidance` in career.yaml): which kinds of work to keep
    apart, what not to default to. Guidance shapes selection; it never adds
    a fact."""
    note = str((profile.career or {}).get("tailoring_guidance") or "").strip()
    return f"\nTHE CANDIDATE'S STANDING GUIDANCE\n{note}\n" if note else ""


async def select_evidence(profile: Profile, job: JobSpec, revision: str | None = None) -> Selection:
    reqs = "\n".join(f"- [{r.kind}] {r.text}  (keywords: {', '.join(r.keywords)})" for r in job.requirements)
    n_roles = len(profile.career.get("experience_details", []) or [])
    n_proj = len(profile.career.get("projects", []) or [])
    proj_note = (
        f"There are {n_proj} project(s) in the record (proj0..proj{n_proj - 1}). In "
        "include_project_ids, list the ones that earn space for this posting, most "
        "relevant first — a project demonstrating a required skill the roles do not "
        "is worth more than one repeating what the roles already show. For a student "
        "or early-career candidate projects usually earn space; leave the list empty "
        "only when none add anything."
        if n_proj else "The record has no projects; leave include_project_ids empty."
    )
    return await _parse(
        _career_system(profile),
        f"""Plan a resume for this role before any writing happens.

ROLE: {job.role_title} ({job.seniority}) at {job.company or 'unnamed company'}
CONTEXT: {job.domain_context}

REQUIREMENTS
{reqs}

For each requirement, cite the evidence ids that support it. "adjacent" means a \
real, reportable outcome using a neighbouring technology or domain — a genuine \
outcome, not a stretch. Where nothing supports a requirement, say so with \
strength "none" and empty evidence_ids; do not force a weak match to look strong.

There are {n_roles} experience entries in the record (exp0..exp{n_roles - 1}). \
Produce exactly one role_plan per entry, in the order they should appear \
(most relevant first). Decide bullet_count per role from how much it actually \
supports this posting — 0 for a role that adds nothing to this application, up \
to 5 for the strongest match. The bullet counts you choose become the number of \
bullets written; do not default to a uniform number.

{proj_note}
{_guidance_block(profile)}
State the positioning in one sentence: the angle a reader should come away with.{_revision_block(revision, "Re-plan so that the evidence answering each point is selected, placed first, and given the most bullets; put a project that demonstrates a required skill ahead of one that repeats what the roles show; state the positioning in the posting's own terms.")}""",
        Selection,
    )


# --- stage 3 ----------------------------------------------------------------

async def draft_role(profile: Profile, job: JobSpec, sel: Selection, plan, revision: str | None = None,
                     base_bullets: list[str] | None = None) -> RoleDraft | None:
    """`base_bullets` — the applicant's own résumé wording for this role —
    turns the draft into a rewording: the same bullets, in order, each
    changed only where the posting's terms are supported by the facts."""
    if plan.bullet_count <= 0:
        return None
    exp = (profile.career.get("experience_details") or [])[int(plan.evidence_id.removeprefix("exp"))]
    index = profile.evidence_index()
    facts = "\n".join(
        f"{k}: {v}" for k, v in index.items()
        if k == plan.evidence_id or k.startswith(plan.evidence_id + ".")
    )
    # A role cannot earn more bullets than it has facts. Left uncapped, a plan
    # asking for five from a three-fact role is an instruction to split facts
    # in two — which is exactly what happened.
    n_facts = sum(1 for k in index if k.startswith(plan.evidence_id + ".r") or k.startswith(plan.evidence_id + ".b"))
    n = len(base_bullets) if base_bullets else min(plan.bullet_count, n_facts)
    if n <= 0:
        return None
    base_block = ""
    if base_bullets:
        keywords = sorted({k for r in job.requirements for k in r.keywords})[:30]
        listed = "\n".join(f"{plan.evidence_id}.b{j}: {t}" for j, t in enumerate(base_bullets))
        base_block = f"""

THE APPLICANT'S OWN RÉSUMÉ WORDING FOR THIS ROLE — the starting point
{listed}

Write exactly {n} bullets, one per résumé bullet above and in that order. Each \
is that bullet, reworded only where the posting's own terms are supported by \
the facts: keep its substance, keep every number exactly as written, keep its \
length within about a fifth of the original, and put the whole sentence in \
`action` with `outcome` empty. A bullet the posting gives no reason to touch \
comes back as it is. Cite the bullet's own id ({plan.evidence_id}.b0, …) in \
source_fact_ids, plus any other fact it draws on.
POSTING TERMS TO ECHO WHERE THE FACTS SUPPORT THEM: {', '.join(keywords) or '(none extracted)'}"""
    # This role's facts are the only ids it may cite — the gate enforces that —
    # so the full index is not sent. On a low tokens-per-minute tier that shared
    # prefix was most of every draft call's cost.
    draft = await _parse(
        [{"type": "text", "text": _SYSTEM_ROLE}],
        f"""Write {'exactly' if base_bullets else 'at most'} {n} bullet(s) for this one role{'' if base_bullets else ' — one bullet per fact'}.

ROLE BEING WRITTEN: {exp.get('position', '')} at {exp.get('company', '')}
FOCUS FOR THIS POSTING: {plan.focus}
POSITIONING: {sel.positioning}

FACTS AVAILABLE FOR THIS ROLE (cite only these ids)
{facts}{base_block}

Rules:
- One fact, one bullet. Never split a single fact into two bullets to fill the \
count, and never merge several facts into one run-on bullet. If fewer than {n} \
facts deserve space for this posting, write fewer.
- Lead each bullet with the outcome if the facts state one; otherwise lead with \
the action. Do not write an outcome that is not in the facts — leave `outcome` \
empty rather than inventing one. About half of real accomplishments have no \
crisp metric, and a bullet with no number is normal, not weak.
- A number may appear only if it is written in the facts above, or is arithmetic \
you performed on numbers that are — and if it is computed, declare it in \
derived_numerals exactly as "RESULT from SOURCE", e.g. "4x from 200 to 800".
- Do not name a technology, employer, or scope not present in the facts above.
- source_fact_ids must be ids from the facts above — never invent one, never \
cite a fact from a different role.

Set evidence_id to "{plan.evidence_id}".{_revision_block(revision, "Where this role's facts speak to any of those points, write those bullets first and make the match explicit, in the words the posting uses — using only what the facts state.")}""",
        RoleDraft,
    )
    # A bullet is one sentence. If the model put all of it in `outcome` and
    # left `action` empty, the outcome is the sentence; nothing renders or is
    # audited as ", won the award".
    kept = []
    for b in draft.bullets:
        action, outcome = b.action.strip().lstrip(",;:.- ").strip(), b.outcome.strip().lstrip(",;:.- ").strip()
        if not action and outcome:
            action, outcome = outcome, ""
        if not action:
            continue
        b.action = action[0].upper() + action[1:]
        b.outcome = outcome
        kept.append(b)
    draft.bullets = kept
    return draft


_SOFT_SKILL = re.compile(
    r"\b(leadership|communication|teamwork|team ?work|collaboration|collaborative|problem[- ]solving|"
    r"time management|work ethic|adaptab\w*|interpersonal|critical thinking|presentation skills|"
    r"attention to detail|self[- ]starter|fast learner|mentoring|claude code|chatgpt|copilot|cursor)\b", re.I)


def _must_survive(profile: Profile, email: str) -> dict[str, str]:
    """What a parser must find in the PDF's text layer. The audit found the
    GPA on none of 424 résumés and the city on none of the newer ones; a
    résumé missing any of these is held (batch), not sent."""
    pi = profile.career.get("personal_information", {}) or {}
    edu = (profile.career.get("education_details") or [{}])[0] or {}
    gpa = re.search(r"\d\.\d+", str(edu.get("gpa") or ""))
    year = re.search(r"20\d\d", str(edu.get("year_of_completion") or ""))
    return {"email": email, "name": profile.full_name, "city": str(pi.get("city") or ""),
            "gpa": gpa.group(0) if gpa else "", "graduation year": year.group(0) if year else ""}


async def draft_skills(profile: Profile, job: JobSpec, sel: Selection) -> SkillsDraft:
    kw = sorted({k for r in job.requirements for k in r.keywords})
    draft = await _parse(
        _career_system(profile),
        f"""Group the record's skills for this posting.

The posting keyword-matches on: {', '.join(kw) or '(none extracted)'}

Include a term only if an evidence id in the index states it, citing skill*, \
exp*.s* or proj* ids. Coursework and degrees (edu* ids) are already rendered in \
the Education section — never list a course or a degree as a skill. Group into \
two to four labelled lines (e.g. Languages, Infrastructure), most relevant to \
this posting first. At most eight terms per line and about twenty in all: a \
skills block that lists everything reads as keyword stuffing. Tools, languages, \
frameworks and named methods only — never a trait (leadership, communication, \
collaboration, problem solving) and never an AI assistant used as a tool.""",
        SkillsDraft, effort="medium",
    )
    # Belt and braces for the rule above: a group sourced only from education
    # entries is coursework wearing a skills label.
    draft.groups = [
        g for g in draft.groups
        if not g.source_fact_ids or not all(i.startswith("edu") for i in g.source_fact_ids)
    ]
    # And for the caps: the audit found blocks of 25 keywords with "team
    # leadership" and "Claude Code" among them.
    budget = 20
    for g in draft.groups:
        g.terms = [t for t in g.terms if t and not _SOFT_SKILL.search(t)][:8]
        g.terms, budget = g.terms[:max(0, budget)], budget - len(g.terms)
    draft.groups = [g for g in draft.groups if g.terms]
    return draft


# --- stages 4-6: gate, audit, repair ---------------------------------------

def _all_claims(exp_drafts: list[RoleDraft]) -> list[tuple[RoleDraft, Bullet]]:
    return [(d, b) for d in exp_drafts for b in d.bullets]


def _cited_text(index: dict[str, str], ids: list[str]) -> str:
    return " ".join(index.get(i, "") for i in ids)


def deterministic_gate(index: dict[str, str], exp_drafts: list[RoleDraft]) -> dict[tuple[str, str], list[gates.Violation]]:
    """Runs before any critic call, so the model never spends tokens on what
    arithmetic already settled."""
    out: dict[tuple[str, str], list[gates.Violation]] = {}
    record_text = " ".join(index.values())
    allowlist = gates.build_entity_allowlist(record_text)
    for draft, bullet in _all_claims(exp_drafts):
        key = (draft.evidence_id, bullet.action)
        v = list(gates.check_citations(bullet.source_fact_ids, index))
        if not bullet.source_fact_ids:
            # Enforced here rather than as a schema minItems, so every provider's
            # structured-output mode accepts the schema and the rule still holds.
            v.append(gates.Violation("citation", bullet.action, "cites no evidence at all"))
        foreign = [i for i in bullet.source_fact_ids
                   if not (i == draft.evidence_id or i.startswith(draft.evidence_id + "."))]
        if foreign:
            # A bullet under one employer citing another employer's facts is how
            # scope, team size and outcomes migrate between jobs.
            v.append(gates.Violation("citation", bullet.action,
                                     f"cites another role's facts: {', '.join(foreign)}"))
        cited = _cited_text(index, bullet.source_fact_ids)
        claim_text = f"{bullet.action} {bullet.outcome}".strip()
        v += gates.check_numerals(claim_text, cited, bullet.derived_numerals)
        # Action and outcome are checked separately so each gets its own
        # sentence start — joined, the outcome's opening verb reads as a name.
        v += gates.check_entities(bullet.action, allowlist)
        if bullet.outcome.strip():
            v += gates.check_entities(bullet.outcome, allowlist)
        if v:
            out[key] = v
    return out


async def audit_claim(cited_text: str, claim: str) -> bool:
    """The JD-blind check. Its entire context is one claim and the verbatim
    evidence it cites — no posting, no career file, no other bullets, no writer
    persona — so it has nothing to reason with beyond whether the words support
    the words."""
    from .models import AuditVerdict

    v = await _parse(
        [{"type": "text", "text": "You verify one claim against one piece of source text. "
                                   "Default to unsupported. You do not know what job this is for "
                                   "and must not guess at relevance — only at truth."}],
        f"""SOURCE TEXT (the only ground truth):
{cited_text or '(nothing was cited)'}

CLAIM TO CHECK:
{claim}

Does the source text state or plainly entail the claim? Quote the supporting \
words verbatim if so.""",
        AuditVerdict, effort="low", role="audit",
    )
    return v.supported


async def audit_and_repair(index: dict[str, str], exp_drafts: list[RoleDraft],
                            pre_flagged: dict[tuple[str, str], list]) -> list[str]:
    """Semantic check for claims the deterministic gate could not rule on, then
    drop whatever still fails. Deletion or reduction only — never substitution
    with a different unverified fact, which would just relocate the risk."""
    dropped: list[str] = []
    to_audit: list[tuple[RoleDraft, Bullet]] = []
    for draft in exp_drafts:
        for b in draft.bullets:
            key = (draft.evidence_id, b.action)
            if key in pre_flagged:
                dropped.append(f"{b.action} — {'; '.join(str(v) for v in pre_flagged[key])}")
            else:
                to_audit.append((draft, b))

    # Each audit is independent of the others, so they run together; the
    # provider's pacer decides how many are actually in flight.
    verdicts = await asyncio.gather(*(
        audit_claim(_cited_text(index, b.source_fact_ids), f"{b.action} {b.outcome}".strip())
        for _, b in to_audit
    ))
    passed = {id(b) for (_, b), ok in zip(to_audit, verdicts) if ok}

    for draft in exp_drafts:
        keep: list[Bullet] = []
        for b in draft.bullets:
            if (draft.evidence_id, b.action) in pre_flagged:
                continue
            if id(b) in passed:
                keep.append(b)
            else:
                dropped.append(f"{b.action} — audit could not confirm this against its cited evidence")
        draft.bullets = keep
    return dropped


# --- coverage ---------------------------------------------------------------

def score_coverage(job: JobSpec, pdf_text: str) -> dict[str, Any]:
    """Measured against the PDF's own text layer — a keyword lost to the render
    counts as missing, because that is what a parser would see."""
    haystack = pdf_text.lower()
    hard_hit, hard_miss, pref_hit, pref_miss = [], [], [], []
    for r in job.requirements:
        for k in r.keywords:
            present = k.lower() in haystack
            (hard_hit if r.kind == "hard" and present else
             hard_miss if r.kind == "hard" else
             pref_hit if present else pref_miss).append(k)
    total_hard, total_pref = len(hard_hit) + len(hard_miss), len(pref_hit) + len(pref_miss)
    return {
        "hard_requirements_pct": round(100 * len(hard_hit) / total_hard) if total_hard else None,
        "preferred_pct": round(100 * len(pref_hit) / total_pref) if total_pref else None,
        "hard_matched": sorted(set(hard_hit)),
        "hard_missing": sorted(set(hard_miss)),
        "preferred_missing": sorted(set(pref_miss)),
    }


# --- assembly ---------------------------------------------------------------

def _slot_of(bullet: Bullet, evidence_id: str) -> int | None:
    """Which base bullet a tailored one stands for, from the base id it cites."""
    for fid in bullet.source_fact_ids:
        m = re.fullmatch(re.escape(evidence_id) + r"\.b(\d+)", fid.strip())
        if m:
            return int(m.group(1))
    return None


def resume_file_name(profile: Profile) -> str:
    """The name the PDF is uploaded under: the skeleton's, else
    First_Last_resume.pdf — never the company or the role."""
    named = str((profile.base_resume or {}).get("file_name") or "").strip()
    if named:
        return named
    stem = re.sub(r"[^A-Za-z0-9]+", "_", profile.full_name or "resume").strip("_") or "resume"
    return f"{stem}_resume.pdf"


async def _tailor_on_base(profile: Profile, job: JobSpec, out_dir: str | Path, label: str,
                          revision: str | None, variant: str, say) -> TailorResult:
    """The résumé on its skeleton (resume/base.yaml): the same roles in the
    same order with the same number of bullets, each bullet a rewording of
    the applicant's own toward the posting's terms — gated and audited like
    any draft, and standing in its own words where a rewording fails — and
    the projects, skills and honors as the skeleton writes them. One page,
    one file name."""
    base = profile.base_resume or {}
    index = profile.evidence_index()
    roles = [r for r in (base.get("roles") or []) if re.fullmatch(r"exp\d+", str(r.get("id") or "")) and r.get("base")]
    keywords = sorted({k for r in job.requirements for k in r.keywords})
    focus = "Echo the posting's own terms where this role's facts support them: " + (", ".join(keywords[:20]) or "(none extracted)")
    plans = [RolePlan(evidence_id=str(r["id"]), bullet_count=len(r["base"]), focus=focus) for r in roles]
    selection = Selection(matches=[], role_plans=plans, include_project_ids=[], gaps=[],
                          positioning=f"{job.role_title} applicant" + (f" at {job.company}" if job.company else "")
                          + " whose record is read in the posting's own terms")
    say(f"rewording {len(plans)} role(s) on the résumé skeleton (in parallel, paced to your rate limit)…")
    drafts_raw = await asyncio.gather(*(
        draft_role(profile, job, selection, p, revision, base_bullets=[str(b) for b in r["base"]])
        for p, r in zip(plans, roles)))
    exp_drafts = [d for d in drafts_raw if d is not None]
    n_bullets = sum(len(d.bullets) for d in exp_drafts)
    say(f"{n_bullets} bullets reworded; running the deterministic gates…")
    flagged = deterministic_gate(index, exp_drafts)
    dropped = await audit_and_repair(index, exp_drafts, flagged)

    # Every slot is filled: the tailored bullet that survived the gates and
    # the audit, else the résumé's own wording for that slot.
    by_id = {d.evidence_id: d for d in exp_drafts}
    filled: list[RoleDraft] = []
    for r, p in zip(roles, plans):
        base_bullets = [str(b) for b in r["base"]]
        slots: list[Bullet | None] = [None] * len(base_bullets)
        loose: list[Bullet] = []
        for b in (by_id[p.evidence_id].bullets if p.evidence_id in by_id else []):
            j = _slot_of(b, p.evidence_id)
            if j is not None and 0 <= j < len(slots) and slots[j] is None:
                slots[j] = b
            else:
                loose.append(b)
        for j in range(len(slots)):
            if slots[j] is None and loose:
                slots[j] = loose.pop(0)
        bullets = [s if s is not None else Bullet(action=base_bullets[j], outcome="",
                                                  source_fact_ids=[f"{p.evidence_id}.b{j}"],
                                                  numerals_used=[], derived_numerals=[])
                   for j, s in enumerate(slots)]
        filled.append(RoleDraft(evidence_id=p.evidence_id, bullets=bullets))
    exp_drafts = filled
    say(f"{len(dropped)} rewording(s) fell back to the résumé's own words; rendering…")

    from .compose import document_from_base

    style = str(base.get("style") or "jake")
    max_pages = int(base.get("max_pages") or 1)
    file_name = resume_file_name(profile)
    slug = (re.sub(r"[^a-z0-9]+", "-", f"{job.company}-{job.role_title}".lower()).strip("-") or label) + variant
    target = Path(out_dir) / "resumes" / slug / file_name
    html = document_from_base(profile.career, base, exp_drafts)
    pdf_path, pages, notch = await render_to_fit_async(html, target, style=style,
                                                       title=profile.full_name or "Resume", max_pages=max_pages)
    if notch:
        say(f"fitted to one page at compact notch {notch}")
    # Still over the page: the longest role gives up its last bullet, once
    # per round, until it fits — the skeleton's shape kept as far as it can be.
    trimmed = 0
    while pages > max_pages and trimmed < 6:
        victim = max(exp_drafts, key=lambda d: len(d.bullets), default=None)
        if victim is None or len(victim.bullets) <= 1:
            break
        victim.bullets.pop()
        trimmed += 1
        html = document_from_base(profile.career, base, exp_drafts)
        pdf_path, pages, notch = await render_to_fit_async(html, target, style=style,
                                                           title=profile.full_name or "Resume", max_pages=max_pages)
    if trimmed:
        say(f"trimmed {trimmed} bullet(s) so the résumé fits one page")
    pdf_text = extract_pdf_text(pdf_path)
    pi = profile.career.get("personal_information", {}) or {}
    email = str((base.get("header") or {}).get("email") or pi.get("email", ""))
    render_violations = (gates.check_rendered(pdf_text, _must_survive(profile, email))
                         + gates.check_bullets_survive(html, pdf_text))
    warnings: list[str] = []
    if dropped:
        warnings.append(f"{len(dropped)} rewording(s) failed verification and stand in the résumé's own words.")
    if render_violations:
        warnings.append(f"{len(render_violations)} issue(s) found in the rendered PDF's own text layer.")
    if pages > max_pages:
        warnings.append(f"{pages} pages — the skeleton would not fit on {max_pages}.")
    return TailorResult(
        pdf_path=pdf_path, html=html, job=job, selection=selection,
        coverage=score_coverage(job, pdf_text), pages=pages,
        roles_included=len(exp_drafts),
        dropped_claims=dropped, render_violations=[str(v) for v in render_violations],
        warnings=warnings,
    )


async def tailor(profile: Profile, job_description: str, style: str = "clean",
                  out_dir: str | Path = "output", label: str = "role",
                  log=None, revision: str | None = None, variant: str = "",
                  job: JobSpec | None = None) -> TailorResult:
    """`revision` carries the judges' notes from a held draft into a fresh
    plan and fresh bullets; `variant` distinguishes that draft's PDF from the
    first; `job` reuses an already-read posting so a revision does not read it
    again."""
    say = log or (lambda _m: None)

    if job is None:
        say("reading the posting…")
        job = await read_posting(job_description)
    say(f"{job.role_title}{' at ' + job.company if job.company else ''} — "
        f"{len(job.requirements)} requirements found; selecting evidence…")
    if profile.base_resume:
        return await _tailor_on_base(profile, job, out_dir, label, revision, variant, say)
    selection = await select_evidence(profile, job, revision)
    index = profile.evidence_index()

    n_active = sum(1 for p in selection.role_plans if p.bullet_count > 0)
    say(f"positioning: {selection.positioning}")
    say(f"drafting {n_active} role(s) and the skills section (in parallel, paced to your rate limit)…")
    role_drafts_raw, skills_draft = await asyncio.gather(
        asyncio.gather(*(draft_role(profile, job, selection, p, revision) for p in selection.role_plans)),
        draft_skills(profile, job, selection),
    )
    exp_drafts = [d for d in role_drafts_raw if d is not None]
    n_bullets = sum(len(d.bullets) for d in exp_drafts)

    say(f"{n_bullets} bullets drafted; running the deterministic gates…")
    flagged = deterministic_gate(index, exp_drafts)
    say(f"{len(flagged)} caught by the gates; auditing the other {n_bullets - len(flagged)} against their cited evidence…")
    dropped = await audit_and_repair(index, exp_drafts, flagged)
    exp_drafts = [d for d in exp_drafts if d.bullets]
    say(f"{len(dropped)} dropped in total; rendering…")

    include_extras = {"projects", "certifications", "achievements", "languages"}
    project_ids = list(selection.include_project_ids)
    html = document(profile.career, exp_drafts, skills_draft, include_extras, project_ids=project_ids)

    slug = (re.sub(r"[^a-z0-9]+", "-", f"{job.company}-{job.role_title}".lower()).strip("-") or label) + variant
    # One page, for an internship résumé: a render that spills a few lines
    # onto a second page is tried again at tighter notches before it stands.
    pdf_path, pages, notch = await render_to_fit_async(html, Path(out_dir) / f"{slug}.pdf", style=style,
                                                       title=profile.full_name or "Resume", max_pages=1)
    if notch:
        say(f"fitted to one page at compact notch {notch}")
    # Still over: the record is bigger than a page, and the least relevant
    # material goes first — the last project the plan listed, then the last
    # bullet of the role the plan ranked lowest — until it fits.
    trimmed = 0
    while pages > 1 and trimmed < 12:
        if len(project_ids) > 1:
            project_ids.pop()
        else:
            victim = next((d for d in reversed(exp_drafts) if len(d.bullets) > 1), None)
            if victim is None:
                break
            victim.bullets.pop()
        trimmed += 1
        html = document(profile.career, exp_drafts, skills_draft, include_extras, project_ids=project_ids)
        pdf_path, pages, notch = await render_to_fit_async(html, Path(out_dir) / f"{slug}.pdf", style=style,
                                                           title=profile.full_name or "Resume", max_pages=1)
    if trimmed:
        say(f"trimmed {trimmed} item(s) so the résumé fits one page")
    pdf_text = extract_pdf_text(pdf_path)

    pi = profile.career.get("personal_information", {}) or {}
    render_violations = (gates.check_rendered(pdf_text, _must_survive(profile, pi.get("email", "")))
                         + gates.check_bullets_survive(html, pdf_text))

    warnings: list[str] = []
    if dropped:
        warnings.append(f"{len(dropped)} claim(s) failed verification and were dropped before render.")
    if render_violations:
        warnings.append(f"{len(render_violations)} issue(s) found in the rendered PDF's own text layer.")
    if pages > 2:
        warnings.append(f"{pages} pages — two is the practical ceiling for most roles.")
    if selection.gaps:
        warnings.append(f"{len(selection.gaps)} requirement(s) your record does not cover.")
    if not exp_drafts:
        warnings.append("No experience survived selection and audit — the record may not fit this role.")

    return TailorResult(
        pdf_path=pdf_path, html=html, job=job, selection=selection,
        coverage=score_coverage(job, pdf_text), pages=pages,
        roles_included=len(exp_drafts),
        dropped_claims=dropped, render_violations=[str(v) for v in render_violations],
        warnings=warnings,
    )
