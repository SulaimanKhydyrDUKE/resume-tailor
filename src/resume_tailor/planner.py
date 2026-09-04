"""The whole form, read once and answered together.

A field answered on its own is answered blind: "second preference" only means
something next to "first preference", "Legal Name (First Name Last Name)"
only reads right with the whole label in view, and "write their name" is
plainly not about the candidate once the sentence around it is read. So the
form's questions — each with its section, its widget, its options, whether it
is required, and what the answer bank would suggest — go to a reasoning model
in one call, and it decides all of them at once.

What the model decides is still held to the same rails as everything else
here: every answer must rest on named profile keys or record ids, an answer
to a multiple-choice question must be one of its options verbatim, a question
about someone other than the candidate never receives the candidate's own
details, and prose about the candidate goes through the essay writer and its
gates rather than being accepted as-is. When the profile cannot decide a
question, the honest plan says so, and the posting goes to review with that
question named.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .apply import _pick_option
from .models import FieldAnswer, FormPlan
from .profile import Profile

# Questions about another person or organisation. The candidate's own name,
# phone and address are never the answer to these, however well the words match.
THIRD_PARTY = re.compile(
    r"emergency|\breferences?\b|\breferr|\brecruiter|\bemployer|\bmanager|supervisor|guardian|\bparent|spouse|"
    r"contact (person|name)|company name|organi[sz]ation name|their (name|email|phone|number)|someone",
    re.I,
)
# Keys that describe the candidate personally — the ones a third-party question must not draw on.
SELF_KEYS = ("personal.", "address.", "about.")
# "Are you being referred?" / "Were you referred by an employee?" is about the
# candidate, however much it says "referr"; only the referrer's own details
# are about someone else.
_ASKS_IF_REFERRED = re.compile(r"\b(are|were|have|did) (you|anyone)\b[^?]{0,40}\breferr|\bdo you have an? (employee )?referral\b", re.I)

_SYSTEM = (
    "You fill in job application forms on a candidate's behalf, from their profile and record only. "
    "You see every question on the form at once; decide them together.\n"
    "Rules:\n"
    "- Answer only what the profile or record decides, and name in `basis` the keys or ids it rests on. "
    "Never invent — no grade, score, address, date, employer or document the profile does not state.\n"
    "- Multiple choice: the answer is one option, verbatim. For a select-all-that-apply list, several "
    "options separated by ' | '.\n"
    "- A picker or search box (school, city, degree, country): the single entry a person would type, "
    "not a sentence. A location or city picker searches as you type, so the options listed for it are "
    "only its opening suggestions, not the choices: answer with the candidate's own place written out, "
    "'City, State, Country' (address.city, address.state, address.country).\n"
    "- Prose about the candidate — why this company, a project, a cover letter, something about them — "
    "is `essay: true` with no answer; the essay writer handles it from the record.\n"
    "- A question about someone other than the candidate — a referrer's name, a reference, an emergency "
    "contact, an employer's address or contact person — is `skip: true`. So is anything the profile cannot "
    "decide. The candidate's own history is not that: 'current or most recent employer', 'most recent job "
    "title', 'where did you complete your most recent internship' and the like are answered with the employer's "
    "name or the title as the record spells it — never with a record id — citing the role id in basis.\n"
    "- Optional questions with no basis in the profile: skip. Do not fill for the sake of filling.\n"
    "- A REQUIRED question is never skipped: a blank required field stops the application, and the "
    "candidate has asked that the tool decide and apply. Decide it from the record: an opinion, "
    "preference, motivation or free-text question is answered briefly in the candidate's voice from the "
    "record (basis ['reasoning'] plus the ids it draws on); a yes/no or choice the profile does not "
    "state is answered with what is true for this candidate as the profile describes them (a U.S. "
    "permanent resident undergraduate available for the posted term) — 18 or older: Yes; background "
    "check or drug screen: Yes; worked here before, referred by an employee, related to an employee: No; "
    "willing to relocate or commute: Yes; agree to terms: Yes. Never claim a citizenship, clearance, "
    "degree, licence, or credential the profile does not state — for those the profile's own answer "
    "stands even when it hurts.\n"
    "- Read labels in full: 'Legal Name (First Name Last Name)' wants the whole name; a 'first' and a "
    "'second' preference should differ; 'End date' under an Education heading is the graduation date, "
    "not a start date; a date field takes the format its hint shows.\n"
    "- Experience with a named thing, or a past tie to the hiring company (employee, intern, contractor, "
    "referral, relative): the record is complete — absent means No. 'Have you applied here before?': No.\n"
    "- 'How much experience in X' with duration options: add up the dates of the roles and projects in "
    "the record that list X (cite them) and pick the matching band; a language or tool absent from the "
    "record gets the option that means none, however that option is worded ('None', '0 years', "
    "'No experience', 'Less than 1 year' when nothing smaller is offered).\n"
    "- Preferences among offered office locations or teams: the candidate's own city first; when "
    "work_preferences.open_to_relocation is Yes, every offered location is acceptable, so an alternate "
    "preference is any other offered one, and an N/A option only when nothing else applies.\n"
    "- When the answer is No because the record shows nothing of the kind (no such employer, no such "
    "experience, no clearance), say No and cite the record's role ids (exp0, exp1, ...) as the basis. "
    "'Do you have experience with X? If yes, explain' with no X in the record is simply 'No'.\n"
    "- A profile key whose name says 'unknown which' settles only the broader question. It does not "
    "decide between the alternatives it names — a question that asks which one must be skipped.\n"
    "- A puzzle or brain-teaser — a question with a right answer that has nothing to do with the "
    "candidate's history or preferences (\"the car wash is two blocks away, walk or drive?\") — is "
    "answered by reasoning, briefly, with basis ['reasoning']. A question about the candidate's own "
    "favourites, reading, opinions or habits is not a puzzle: answer it from a profile key that holds it "
    "(about.recent_reading for 'what have you read lately', for instance), in the candidate's voice and "
    "in one or two sentences; skip it only when no key does.\n"
    "- Neighbouring questions give context: a Start/End date listed next to School and Degree is the "
    "education period, whatever heading the page put above it. Under a work-experience entry (a Job "
    "Title and Company beside it), Location, From/To dates and 'I currently work here' describe THAT "
    "role from the record — the role's own city, its dates as MM and YYYY — never the candidate's "
    "home address; fill one entry with the most recent role unless the form has more.\n"
    "- A GPA field that offers bands: the band containing education.gpa. A GPA, school or date field for "
    "a level the candidate has not done (Graduate, Doctorate, Master's): the option meaning not "
    "applicable or none when offered; otherwise skip it.\n"
    "- 'How did you hear about us': the postings come from a job board; choose a job-board or Other option.\n"
    "- Consent and acknowledgement boxes (terms, privacy policy, accuracy): yes. The consents.* keys say "
    "which further consents the candidate gives when a form makes them a condition of applying — "
    "interview recording or AI note-taking, text or email updates, data processing.\n"
    "- A preference among offered teams, programs, tracks, platforms or divisions is the candidate's "
    "choice, not a fact: pick the option closest to the record (software, backend, AI, vehicle or flight "
    "software over IT, design or business), with basis ['reasoning'], rather than skip a required one.\n"
    "- A compensation picker with bands: the band holding the salary_expectations annual figure "
    "(an hourly figure × 2080 when only that is given).\n"
    "- A test-score picker (SAT, ACT) lists scores: the entry equal to the education score, on the 1600 "
    "scale for an SAT ('1560 out of 1600', never 'out of 2400'); the 'did not take' option only when the "
    "profile has no score.\n"
    "- A required text box asking to list something the record has none of — other colleges attended, "
    "other names used, employment gaps, 'if applicable' — is answered 'None', not skipped."
)

# Questions that ask which of several offered things the candidate would
# rather have — a team, a program, a track. Opinions, not facts: reasoning
# from the record is a fair basis for them.
# Facts the profile alone may answer: eligibility and credentials. Reasoning
# never fills these in; the answer bank holds the candidate's truth for them.
_HARD_FACT = re.compile(r"citizen|clearance|authori[sz]|sponsor|visa|degree|gpa|graduat|felon|convict|licen[cs]e|"
                        r"certif|security|passport|national", re.I)
_PREFERENCE = re.compile(r"prefer|preference|which (team|program|track|platform|division|group|area)|team choice", re.I)

# Questions that the record answers by its silence: nothing of the kind on
# file means No, and No needs no key to rest on.
_SILENCE = re.compile(
    r"employed by|worked (for|at|with)|team member|contract(or|ed| work)|relative|related to|referred|"
    r"experience (with|in|using|of)|familiar|certif|clearance|previously (worked|employed|applied|interned)|"
    r"currently (work|employed)|ever (worked|been employed|applied|interviewed)|applied (to|for)|"
    r"participated in|competed in|competition|olympiad|member of|published|patent|how much experience|"
    r"years of experience|if applicable|list any|other (colleges|universities|schools|names)|attended|"
    r"\((graduate|doctorate|master'?s|phd|masters)\)|graduate gpa|doctora(te|l) gpa|master'?s gpa",
    re.I,
)
_NO_LIKE = re.compile(r"^\s*(no|none|n/?a|not applicable|never|no,? i (have|do) not|0 years?|no experience|less than)\b"
                      r"|\b(not applicable|n/a|none|do not recall|did not take)\b", re.I)


@dataclass
class Decision:
    """What the plan says about one question: an answer, or none with the reason."""

    answer: str | None
    essay: bool = False
    reason: str = ""


def widget_of(field: dict, group_type: str | None = None) -> str:
    if group_type:
        return "checkboxes" if group_type == "checkbox" else "radio"
    if field.get("combobox"):
        return "picker"
    tag, typ = field.get("tag") or "", field.get("type") or ""
    if tag == "select":
        return "select"
    if typ == "yesno":
        return "yesno"
    if tag in ("textarea", "editor"):
        return "textarea"
    if typ == "checkbox":
        return "checkbox"
    return typ or "text"


def question_key(label: str, widget: str, section: str = "") -> tuple[str, str, str]:
    """How a plan entry is found again after the page re-renders: by the
    question's words, its kind of control and the heading it sits under —
    not by a DOM id. The heading is what keeps an Education "End date" apart
    from a Work History one."""
    return (" ".join((label or "").split()).lower()[:200], widget, " ".join((section or "").split()).lower()[:80])


def describe_questions(fields: list[dict], groups: dict[str, list[dict]], hint_for=None) -> list[dict]:
    """The form as a numbered list of questions: one entry per single control
    and one per radio/checkbox group. `hint_for(label)` may supply what the
    answer bank would say, shown to the model as a suggestion."""
    grouped = {m["id"] for ms in groups.values() for m in ms}
    out: list[dict] = []

    def add(label: str, widget: str, field: dict, options: list[str], required: bool):
        q = {
            "qid": f"q{len(out) + 1}",
            "key": question_key(label, widget, field.get("section") or ""),
            "label": label,
            "section": field.get("section") or "",
            "widget": widget,
            "options": options,
            "required": bool(required),
            "maxlength": field.get("maxlength"),
            "hint": " ".join(h for h in (field.get("placeholder"), field.get("hint")) if h),
            "bank": (hint_for(label) if hint_for else None) or "",
        }
        out.append(q)

    for f in fields:
        if f["id"] in grouped or f.get("type") == "file":
            continue
        options = list(f.get("options") or [])
        add(f.get("label", ""), widget_of(f), f, options, f.get("required", False))
    for key, members in groups.items():
        label = next((m["label"] for m in members if m.get("label")), key)
        add(label, widget_of(members[0], members[0].get("type")), members[0],
            [m.get("option_label") or "" for m in members], any(m.get("required") for m in members))
    return out


def _render(questions: list[dict]) -> str:
    lines = []
    for q in questions:
        bits = [q["qid"], f"section: {q['section']}" if q["section"] else "", f"label: {q['label']}",
                f"widget: {q['widget']}", f"options: {q['options']}" if q["options"] else "",
                "required" if q["required"] else "optional",
                f"max {q['maxlength']} chars" if q.get("maxlength") else "",
                f"hint: {q['hint']}" if q["hint"] else "",
                f"the answer bank suggests: {q['bank']!r}" if q["bank"] else ""]
        lines.append(" | ".join(b for b in bits if b))
    return "\n".join(lines)


def _context(profile: Profile) -> str:
    import datetime

    from .qa import _record_summary

    flat = profile.flat_answers()
    today = datetime.date.today()
    return (f"TODAY: {today.isoformat()} ({today.strftime('%B %d, %Y')}) — a 'Date' next to a signature or on a "
            f"self-identification form is today's date (basis: today).\n\n"
            f"PROFILE (key: value)\n{chr(10).join(f'{k}: {v}' for k, v in flat.items())}\n\n"
            f"RECORD (roles, skills, education)\n{_record_summary(profile)}")


async def plan(questions: list[dict], profile: Profile, posting_text: str) -> dict[tuple[str, str], Decision]:
    """One model call for the whole form. Returns decisions keyed by
    question_key, already passed through the rails."""
    from .tailor import _parse

    if not questions:
        return {}
    prompt = (f"THE POSTING\n{posting_text[:3000]}\n\n{_context(profile)}\n\n"
              f"THE FORM — answer every question by its id\n{_render(questions)}")
    out = await _parse([{"type": "text", "text": _SYSTEM}], prompt, FormPlan, effort="medium")
    return rails(out.answers, questions, profile)


async def repair(question: dict, previous: str | None, error: str, profile: Profile, posting_text: str) -> Decision:
    """A second look at one question the page did not accept: the model sees
    what was tried, what the form said, and the options as they stand now."""
    from .tailor import _parse

    prompt = (f"THE POSTING\n{posting_text[:1500]}\n\n{_context(profile)}\n\n"
              f"ONE QUESTION THE FORM DID NOT ACCEPT\n{_render([question])}\n"
              f"previous attempt: {previous!r}\nwhat the form said: {error or '(nothing — the field is still empty)'}\n"
              "Answer it again by its id, or skip it.")
    out = await _parse([{"type": "text", "text": _SYSTEM}], prompt, FormPlan, effort="low")
    return rails(out.answers, [question], profile).get(question["key"], Decision(None, reason="no answer"))


def _id_to_name(profile: Profile, answer: str) -> str | None:
    """'exp0' → that role's employer; 'proj1' → that project's name. A form
    never wants the record's own ids."""
    m = re.fullmatch(r"(exp|proj)(\d+)(?:\.\w+)?", answer.strip(), re.I)
    if not m:
        return None
    career = getattr(profile, "career", {}) or {}
    items = career.get("experience_details" if m.group(1).lower() == "exp" else "projects") or []
    i = int(m.group(2))
    if i >= len(items):
        return None
    item = items[i] or {}
    return str(item.get("company") or item.get("name") or "") or None


def rails(answers: list[FieldAnswer], questions: list[dict], profile: Profile) -> dict[tuple[str, str], Decision]:
    """The model's plan, held to the rules the rest of the tool lives by."""
    flat = profile.flat_answers()
    index = profile.evidence_index()
    by_id = {q["qid"]: q for q in questions}
    decisions: dict[tuple[str, str], Decision] = {}
    for a in answers:
        q = by_id.get(a.id)
        if q is None:
            continue
        key = q["key"]
        if a.essay:
            decisions[key] = Decision(None, essay=True, reason=a.reason)
            continue
        answer = (a.answer or "").strip()
        if a.skip or not answer:
            decisions[key] = Decision(None, reason=a.reason or "skipped")
            continue
        named = _id_to_name(profile, answer)
        if named:
            answer = named  # the model wrote the record id where a form wants the employer's name
        known = [b for b in a.basis if b in flat or b in index or b == "today"]
        # A puzzle is answered by reasoning alone — but only a question that
        # is not about the candidate ("you", "your") can be a puzzle.
        if not known and "reasoning" in a.basis and (
                not re.search(r"\byou(r|rs|rself)?\b", q["label"], re.I) or _PREFERENCE.search(q["label"])
                or (q.get("required") and not _HARD_FACT.search(q["label"])
                    and not re.search(r"\b(read|reading|favou?rite|book|paper|article|podcast|hobby|hobbies)\b", q["label"], re.I))):
            # Reasoning carries a puzzle, a preference, or a required question
            # that is not a hard eligibility fact — never a credential, and
            # never a personal fact like what the candidate has read (that
            # must rest on a record id or a bank key).
            known = ["reasoning"]
        if not known and not (_SILENCE.search(q["label"]) and _NO_LIKE.match(answer)):
            decisions[key] = Decision(None, reason="rests on nothing in the profile")
            continue
        if (known and THIRD_PARTY.search(q["label"]) and all(b.startswith(SELF_KEYS) for b in known)
                and not _ASKS_IF_REFERRED.search(q["label"])):
            decisions[key] = Decision(None, reason="asks about someone else")
            continue
        if q["options"]:
            wanted = [p.strip() for p in answer.split("|")] if q["widget"] == "checkboxes" else [answer]
            chosen: list[str] = []
            for w in wanted:
                i = _pick_option(w, [{"label": o, "value": o} for o in q["options"]])
                if i is not None and q["options"][i] not in chosen:
                    chosen.append(q["options"][i])
            if not chosen:
                decisions[key] = Decision(None, reason=f"{answer!r} is not one of the options")
                continue
            answer = " | ".join(chosen)
        decisions[key] = Decision(answer, reason=a.reason)
    return decisions
