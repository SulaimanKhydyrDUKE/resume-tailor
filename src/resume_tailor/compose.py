"""Render the document from real records plus generated prose.

Every fact here is copied out of the career YAML: name, contact details, links,
employer, title, dates, location, degree, institution, year. None of it passes
through a model call, so none of it can come back altered. The model supplies
bullet prose and skill groupings; this decides what exists and how many of it
there are.

Cardinality is the point. The old generator handed the model an HTML skeleton
with three work-experience blocks and twelve skill slots, which reads as an
instruction to produce three jobs and twelve skills whether or not you have
them.
"""
from __future__ import annotations

import re
from html import escape as esc

from .models import Bullet, RoleDraft, SkillsDraft


def _contact_line(pi: dict) -> str:
    bits: list[str] = []
    loc = ", ".join(x for x in (pi.get("city"), pi.get("country")) if x)
    if loc:
        bits.append(f"<span>{esc(loc)}</span>")
    if pi.get("phone"):
        phone = f"{pi.get('phone_prefix', '')} {pi['phone']}".strip()
        bits.append(f"<span>{esc(phone)}</span>")
    if pi.get("email"):
        bits.append(f"<span>{esc(pi['email'])}</span>")
    for key, label in (("linkedin", "LinkedIn"), ("github", "GitHub"), ("website", "Website")):
        if pi.get(key):
            bits.append(f'<a href="{esc(pi[key])}">{label}</a>')
    # A literal separator, not a CSS pseudo-element: generated content does not
    # reach the PDF text layer, and the fields would extract run together.
    joined = "\n    ".join(
        f"<p>{b}</p>" + ("\n    <p><span>&middot;</span></p>" if i < len(bits) - 1 else "")
        for i, b in enumerate(bits)
    )
    return f'  <div class="contact-info">\n    {joined}\n  </div>'


def header(career: dict) -> str:
    pi = career.get("personal_information", {}) or {}
    name = " ".join(x for x in (pi.get("name"), pi.get("surname")) if x)
    return f"<header>\n  <h1>{esc(name)}</h1>\n{_contact_line(pi)}\n</header>"


_PREPOSITIONAL = ("on ", "using ", "with ", "for ", "across ", "via ", "at ", "in ", "to ", "by ", "through ")


def _clean_clause(s: str) -> str:
    """A clause as it may appear in a bullet: no leading punctuation or
    filler, no trailing full stop."""
    return s.strip().lstrip(",;:.- ").strip().rstrip(".").strip()


def bullet_text(b: Bullet) -> str:
    """The sentence a bullet renders as. The model sometimes puts a whole
    accomplishment in `outcome` and leaves `action` empty — "Won the ACM
    HotMobile Best Demo Award" is an outcome, after all — and joining an
    empty action to it with a comma once printed ", won the ACM…" at the
    left margin of thirty résumés. An empty action means the outcome is the
    sentence; whatever the parts, the sentence starts with a capital."""
    action = _clean_clause(b.action)
    out = _clean_clause(b.outcome)
    if not action and not out:
        return ""
    if not action:
        text = out
    elif out:
        out = out[0].lower() + out[1:]
        # "…services, on Yandex Cloud" reads wrong: a prepositional tail joins
        # with a space, an independent result clause with a comma.
        sep = " " if out.startswith(_PREPOSITIONAL) or action.endswith(",") else ", "
        text = f"{action}{sep}{out}"
    else:
        text = action
    return text[0].upper() + text[1:]


def _bullet_html(b: Bullet) -> str:
    text = bullet_text(b)
    return f"<li>{esc(text)}.</li>" if text else ""


def experience(career: dict, drafts: list[RoleDraft]) -> str:
    """One entry per real record that earned bullets. A role the plan gave zero
    bullets is omitted entirely rather than padded."""
    exps = career.get("experience_details", []) or []
    by_id = {f"exp{i}": e for i, e in enumerate(exps)}
    entries: list[str] = []
    for d in drafts:
        exp = by_id.get(d.evidence_id)
        if exp is None or not d.bullets:
            continue
        meta = " &middot; ".join(
            esc(x) for x in (exp.get("company"), exp.get("location"), exp.get("employment_period")) if x
        )
        lis = "\n      ".join(h for h in (_bullet_html(b) for b in d.bullets) if h)
        if not lis:
            continue
        entries.append(
            f'  <div class="entry">\n'
            f'    <h3>{esc(exp.get("position", ""))}</h3>\n'
            f'    <p class="entry-meta">{meta}</p>\n'
            f'    <ul>\n      {lis}\n    </ul>\n'
            f'  </div>'
        )
    if not entries:
        return ""
    return "<section>\n  <h2>Experience</h2>\n" + "\n".join(entries) + "\n</section>"


def skills(draft: SkillsDraft) -> str:
    groups = [g for g in draft.groups if g.terms]
    if not groups:
        return ""
    lines = "\n  ".join(
        f"<p><strong>{esc(g.label)}:</strong> {esc(', '.join(g.terms))}</p>" for g in groups
    )
    return f'<section class="skills">\n  <h2>Skills</h2>\n  {lines}\n</section>'


def education(career: dict) -> str:
    eds = career.get("education_details", []) or []
    if not eds:
        return ""
    entries = []
    for ed in eds:
        title = ", ".join(x for x in (ed.get("education_level"), ed.get("field_of_study")) if x)
        meta = " &middot; ".join(
            esc(x) for x in (ed.get("institution"), ed.get("year_of_completion")) if x
        )
        grade = ed.get("final_evaluation_grade")
        if grade:
            meta += f" &middot; {esc(str(grade))}"
        course = ed.get("coursework") or []
        coursework = (
            f'\n    <p class="entry-meta"><strong>Coursework:</strong> '
            f'{esc(", ".join(str(c) for c in course))}</p>'
            if course else ""
        )
        entries.append(
            f'  <div class="entry">\n    <h3>{esc(title)}</h3>\n'
            f'    <p class="entry-meta">{meta}</p>{coursework}\n  </div>'
        )
    return "<section>\n  <h2>Education</h2>\n" + "\n".join(entries) + "\n</section>"


def _simple_section(title: str, items: list, fmt) -> str:
    """A section is emitted only when its source data is non-empty, so the model
    is never asked whether a section should exist and never sees an empty list."""
    rows = [fmt(i) for i in items if i]
    rows = [r for r in rows if r]
    if not rows:
        return ""
    lis = "\n    ".join(f"<li>{r}</li>" for r in rows)
    return f"<section>\n  <h2>{esc(title)}</h2>\n  <ul>\n    {lis}\n  </ul>\n</section>"


def _project_entry(p: dict) -> str:
    name = esc(p["name"])
    if p.get("link"):
        name = f'<a href="{esc(p["link"])}">{name}</a>'
    meta_bits = []
    tech = p.get("technologies") or []
    if tech:
        meta_bits.append(esc(", ".join(str(t) for t in tech)))
    if p.get("period"):
        meta_bits.append(esc(str(p["period"])))
    meta = f'\n    <p class="entry-meta">{" &middot; ".join(meta_bits)}</p>' if meta_bits else ""
    bullets = [str(b) for b in (p.get("bullets") or []) if b]
    if not bullets and p.get("description"):
        bullets = [str(p["description"])]
    lis = "\n      ".join(f"<li>{esc(b.rstrip('.'))}.</li>" for b in bullets)
    body = f"\n    <ul>\n      {lis}\n    </ul>" if lis else ""
    return f'  <div class="entry">\n    <h3>{name}</h3>{meta}{body}\n  </div>'


def projects(career: dict, project_ids: list[str] | None = None) -> str:
    """Projects are real records, rendered verbatim like experience. Which of
    them appear, and in what order, is a per-posting decision made in the
    selection stage and passed in as ids — so a project that adds nothing to a
    given application is left off rather than padding the page."""
    by_id = {
        f"proj{i}": p for i, p in enumerate(career.get("projects", []) or [])
        if isinstance(p, dict) and p.get("name")
    }
    if project_ids is None:
        chosen = list(by_id.values())
    else:
        chosen = [by_id[i] for i in project_ids if i in by_id]
    if not chosen:
        return ""
    return "<section>\n  <h2>Projects</h2>\n" + "\n".join(_project_entry(p) for p in chosen) + "\n</section>"


def extras(career: dict, include: set[str]) -> str:
    out = []
    for key, title in (("certifications", "Certifications"), ("achievements", "Achievements")):
        if key in include:
            out.append(_simple_section(
                title, career.get(key, []) or [],
                lambda x: f"<strong>{esc(x['name'])}</strong>" + (f" — {esc(x['description'])}" if x.get("description") else "")
                if isinstance(x, dict) and x.get("name") else ""))
    if "languages" in include:
        out.append(_simple_section(
            "Languages", career.get("languages", []) or [],
            lambda l: f"{esc(l['language'])} ({esc(l.get('proficiency', ''))})".replace(" ()", "")
            if isinstance(l, dict) and l.get("language") else ""))
    return "\n".join(s for s in out if s)


def _row(left: str, right: str, sub_left: str = "", sub_right: str = "") -> str:
    """A LaTeX-style heading: title left, dates right; subtitle left,
    location right. A two-cell table reads left to right as one line, so the
    text layer stays in order."""
    top = f'    <tr><td class="t">{left}</td><td class="r d">{right}</td></tr>\n'
    sub = f'    <tr><td class="s">{sub_left}</td><td class="r l">{sub_right}</td></tr>\n' if sub_left or sub_right else ""
    return f'  <table class="row">\n{top}{sub}  </table>'


def _plain_link(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


def _base_header(career: dict, base: dict | None = None) -> str:
    """The name over one contact line — phone | email | linkedin | github —
    as the LaTeX résumé lays it out. The skeleton's `header` block may name
    the phone, e-mail or links to print (a school address, say) in place of
    the record's."""
    pi = career.get("personal_information", {}) or {}
    over = (base or {}).get("header") or {}
    name = " ".join(x for x in (pi.get("name"), pi.get("surname")) if x)
    bits: list[str] = []
    phone = str(over.get("phone") or f"{pi.get('phone_prefix', '')} {pi.get('phone', '')}".strip())
    if phone:
        bits.append(esc(phone))
    email = str(over.get("email") or pi.get("email") or "")
    if email:
        bits.append(f'<a class="u" href="mailto:{esc(email)}">{esc(email)}</a>')
    for key in ("linkedin", "github", "website"):
        url = str(over.get(key) or pi.get(key) or "")
        if url:
            bits.append(f'<a class="u" href="{esc(url)}">{esc(_plain_link(url))}</a>')
    line = '<span class="sep">|</span>'.join(f"<p>{b}</p>" for b in bits)
    return f'<header>\n  <h1>{esc(name)}</h1>\n  <div class="contact-info">{line}</div>\n</header>'


def document_from_base(career: dict, base: dict, exp_drafts: list[RoleDraft]) -> str:
    """The résumé on its skeleton (resume/base.yaml): every role in the
    skeleton's order with the tailored bullets that stand for its own — one
    per base bullet — and the projects, skills and honors exactly as the
    skeleton writes them. Nothing here comes from a model but the role
    bullets, and those were gated and audited."""
    parts = [_base_header(career, base)]

    # Education: the record's degree and school, the skeleton's coursework and extra lines.
    ed = next(iter(career.get("education_details") or []), {}) or {}
    edu = base.get("education") or {}
    degree = str(edu.get("degree") or ", ".join(x for x in (ed.get("education_level"), ed.get("field_of_study")) if x))
    grad = str(edu.get("graduation") or ed.get("year_of_completion") or "")
    lines = []
    if edu.get("coursework"):
        lines.append(f"<li><strong>Coursework:</strong> {esc(str(edu['coursework']))}</li>")
    for extra in edu.get("lines") or []:
        text = str(extra)
        m = re.match(r"^([^:]{2,40}):\s*(.+)$", text)
        lines.append(f"<li><strong>{esc(m.group(1))}:</strong> {esc(m.group(2))}</li>" if m else f"<li>{esc(text)}</li>")
    parts.append(
        "<section>\n  <h2>Education</h2>\n"
        + f'  <div class="entry">\n{_row(esc(str(ed.get("institution") or "")), esc(str(ed.get("location") or "")), esc(degree), esc(grad))}\n'
        + (f'    <ul class="edu-lines">\n      {chr(10).join(lines)}\n    </ul>\n' if lines else "")
        + "  </div>\n</section>"
    )

    # Experience: the skeleton's roles, in its order.
    by_id = {d.evidence_id: d for d in exp_drafts}
    exps = career.get("experience_details", []) or []
    entries = []
    for role in base.get("roles") or []:
        rid = str(role.get("id") or "")
        exp = exps[int(rid[3:])] if re.fullmatch(r"exp\d+", rid) and int(rid[3:]) < len(exps) else {}
        draft = by_id.get(rid)
        texts = [bullet_text(b) for b in draft.bullets] if draft and draft.bullets else [str(b) for b in (role.get("base") or [])]
        texts = [t for t in texts if t]
        if not texts:
            continue
        org = esc(str(role.get("org") or exp.get("company") or ""))
        if role.get("org_link"):
            org = f'<a class="u" href="{esc(str(role["org_link"]))}">{org}</a>'
        title = esc(str(role.get("title") or exp.get("position") or ""))
        dates = esc(str(role.get("dates") or exp.get("employment_period") or ""))
        location = esc(str(role.get("location") or exp.get("location") or ""))
        lis = "\n      ".join(f"<li>{esc(t)}</li>" for t in texts)
        entries.append(f'  <div class="entry">\n{_row(title, dates, org, location)}\n    <ul>\n      {lis}\n    </ul>\n  </div>')
    if entries:
        parts.append("<section>\n  <h2>Experience</h2>\n" + "\n".join(entries) + "\n</section>")

    # Projects, verbatim from the skeleton.
    projects_html = []
    for p in base.get("projects") or []:
        name = esc(str(p.get("name") or ""))
        if p.get("link"):
            name = f'<a class="u" href="{esc(str(p["link"]))}">{name}</a>'
        head = f"{name}" + (f' <span class="tech">| {esc(str(p["tech"]))}</span>' if p.get("tech") else "")
        lis = "\n      ".join(f"<li>{esc(str(b))}</li>" for b in (p.get("bullets") or []) if b)
        projects_html.append(f'  <div class="entry project">\n{_row(head, esc(str(p.get("dates") or "")))}\n'
                             + (f"    <ul>\n      {lis}\n    </ul>\n" if lis else "") + "  </div>")
    if projects_html:
        parts.append("<section>\n  <h2>Projects</h2>\n" + "\n".join(projects_html) + "\n</section>")

    # Skills and honors, verbatim.
    skill_lines = [f"<p><strong>{esc(str(s.get('label')))}:</strong> {esc(str(s.get('terms')))}</p>"
                   for s in (base.get("skills") or []) if s.get("label") and s.get("terms")]
    if skill_lines:
        parts.append('<section class="skills">\n  <h2>Technical Skills</h2>\n  ' + "\n  ".join(skill_lines) + "\n</section>")
    honors = [f"<li>{esc(str(h))}</li>" for h in (base.get("honors") or []) if h]
    if honors:
        parts.append('<section class="honors">\n  <h2>Honors &amp; Awards</h2>\n  <ul>\n    ' + "\n    ".join(honors) + "\n  </ul>\n</section>")
    return "<body>\n" + "\n".join(parts) + "\n</body>"


def document(career: dict, exp_drafts: list[RoleDraft], skills_draft: SkillsDraft,
             include_extras: set[str], project_ids: list[str] | None = None) -> str:
    # Education leads: for a current student a reader wants school, degree and
    # graduation date first, and the entry is three lines — it does not push
    # the experience far.
    parts = [
        header(career),
        education(career),
        experience(career, exp_drafts),
        projects(career, project_ids) if "projects" in include_extras else "",
        skills(skills_draft),
        extras(career, include_extras),
    ]
    return "<body>\n" + "\n".join(p for p in parts if p) + "\n</body>"
