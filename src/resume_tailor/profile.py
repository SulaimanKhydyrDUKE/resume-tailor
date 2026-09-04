"""Loading the two YAML files, and answering form questions from them.

The answer bank is looked up, never generated. A field the agent cannot match
to something you wrote comes back as an explicit miss so it can ask you, which
is the whole difference between a form filled from your record and a form filled
from a guess.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILE_DIR = Path(
    os.environ.get("RESUME_TAILOR_PROFILE", Path.home() / ".resume-tailor")
)


class ProfileError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    if not path.is_file():
        raise ProfileError(
            f"Missing {path}. Copy the example next to it and fill it in:\n"
            f"  cp {path.with_suffix('')}.example.yaml {path}"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path} must be a mapping at the top level.")
    return data


def _prune(value: Any) -> Any:
    """Drop empty leaves so unfilled template keys never reach a prompt.

    A blank string in the YAML means "not provided". Left in, it reads to the
    model as a field it should fill, which is exactly the wrong instinct.
    """
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        out = [_prune(v) for v in value]
        return [v for v in out if v not in (None, "", [], {})]
    if isinstance(value, str):
        return value.strip()
    return value


@dataclass
class Profile:
    career: dict
    answers: dict
    root: Path

    @classmethod
    def load(cls, root: str | Path | None = None) -> "Profile":
        root = Path(root or DEFAULT_PROFILE_DIR).expanduser()
        return cls(
            career=_prune(_load(root / "career.yaml")),
            answers=_prune(_load(root / "answers.yaml")),
            root=root,
        )

    # --- career -----------------------------------------------------------

    @property
    def full_name(self) -> str:
        pi = self.career.get("personal_information", {})
        return " ".join(x for x in (pi.get("name"), pi.get("surname")) if x)

    def career_yaml(self) -> str:
        """The career record as YAML, for the cached prefix of every call."""
        return yaml.safe_dump(self.career, sort_keys=False, allow_unicode=True)

    def evidence_index(self) -> dict[str, str]:
        """id -> claimable fact. The set of things a resume may assert.

        Every generated bullet must cite one of these ids, which is what makes
        fabrication detectable rather than merely discouraged.
        """
        idx: dict[str, str] = {}
        for i, exp in enumerate(self.career.get("experience_details", []) or []):
            base = f"exp{i}"
            head = f"{exp.get('position', '?')} at {exp.get('company', '?')} ({exp.get('employment_period', '?')})"
            idx[base] = head
            for j, r in enumerate(exp.get("key_responsibilities", []) or []):
                text = r.get("responsibility") if isinstance(r, dict) else str(r)
                if text:
                    idx[f"{base}.r{j}"] = f"[{head}] {text}"
            for j, s in enumerate(exp.get("skills_acquired", []) or []):
                idx[f"{base}.s{j}"] = f"[{head}] skill: {s}"
        for i, ed in enumerate(self.career.get("education_details", []) or []):
            idx[f"edu{i}"] = (
                f"{ed.get('education_level', '?')} in {ed.get('field_of_study', '?')}, "
                f"{ed.get('institution', '?')} ({ed.get('year_of_completion', '?')})"
            )
            for j, c in enumerate(ed.get("coursework", []) or []):
                idx[f"edu{i}.c{j}"] = f"[{ed.get('institution', '?')}] coursework: {c}"
        for i, p in enumerate(self.career.get("projects", []) or []):
            if not isinstance(p, dict):
                continue
            head = p.get("name", "?")
            tech = ", ".join(str(t) for t in (p.get("technologies") or []))
            idx[f"proj{i}"] = head + (f" ({tech})" if tech else "") + (
                f": {p['description']}" if p.get("description") else ""
            )
            for j, b in enumerate(p.get("bullets", []) or []):
                idx[f"proj{i}.b{j}"] = f"[{head}] {b}"
        for group in ("achievements", "certifications"):
            for i, item in enumerate(self.career.get(group, []) or []):
                if isinstance(item, dict):
                    idx[f"{group[:4]}{i}"] = f"{item.get('name', '?')}: {item.get('description', '')}".strip()
        # Skills claimed independently of any one role. Without these, a skill
        # from the resume's own skills section has no id to cite and can never
        # appear, however well it matches the posting.
        skills = self.career.get("skills", {}) or {}
        if isinstance(skills, dict):
            for i, (group, terms) in enumerate(skills.items()):
                for j, t in enumerate(terms or []):
                    idx[f"skill{i}.{j}"] = f"skill ({group}): {t}"
        for i, lang in enumerate(self.career.get("languages", []) or []):
            if isinstance(lang, dict):
                idx[f"lang{i}"] = f"{lang.get('language', '?')} ({lang.get('proficiency', '?')})"
        return idx

    def for_location(self, location_text: str) -> "Profile":
        """The profile with the address that applies to a posting's location.

        `answers.yaml -> address.alternates` lists addresses that replace the
        default when the posting's location matches any of their terms — a home
        address for postings in the home state, the campus address otherwise.
        The rest of the profile is shared, so the form answers and the fit
        judges both see the address that will actually be submitted."""
        base = self.answers.get("address") or {}
        text = location_text or ""
        for alt in base.get("alternates") or []:
            if not isinstance(alt, dict):
                continue
            for term in alt.get("when_location_matches") or []:
                term = str(term)
                # Short codes like "MN" match case-sensitively and only as a
                # whole token; names match case-insensitively.
                flags = 0 if len(term) <= 3 else re.I
                if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text, flags):
                    merged = {**base, **{k: v for k, v in alt.items() if k != "when_location_matches"}}
                    merged.pop("alternates", None)
                    return Profile(career=self.career, answers={**self.answers, "address": merged}, root=self.root)
        return self

    def blacklisted(self, company: str) -> bool:
        """Whether `search.company_blacklist` names this company, by substring,
        case-insensitively — "Palantir" covers "Palantir Technologies"."""
        names = (self.answers.get("search") or {}).get("company_blacklist") or []
        c = (company or "").lower()
        return any(str(n).lower() in c for n in names if str(n).strip()) if c else False

    def applicant_facts(self) -> str:
        """What the application form will say that the resume does not — for
        the fit judges, so a posting's location or sponsorship line is weighed
        against the candidate's actual answers rather than against a resume
        that never states them."""
        lines = []
        for section, title in (("work_authorization", "Work authorization"), ("work_preferences", "Work preferences"),
                               ("availability", "Availability"), ("education", "Education"), ("address", "Location")):
            sec = self.answers.get(section) or {}
            items = [f"{k.replace('_', ' ')}: {v}" for k, v in sec.items()
                     if v not in ("", None) and k != "default_region"]
            if items:
                lines.append(f"{title} — " + "; ".join(items))
        return "\n".join(lines)

    # --- answers ----------------------------------------------------------

    def flat_answers(self) -> dict[str, str]:
        flat: dict[str, str] = {}

        def walk(d: dict, prefix: str = "") -> None:
            for k, v in d.items():
                key = f"{prefix}{k}"
                if isinstance(v, dict):
                    walk(v, f"{key}.")
                elif isinstance(v, (str, int, float, bool)):
                    flat[key] = str(v)

        # Every section of answers.yaml except the search settings, so a new
        # section (address, education, ...) answers forms without a code change.
        for section, val in self.answers.items():
            if section != "search" and isinstance(val, dict):
                walk(val, f"{section}.")

        pi = self.career.get("personal_information", {})
        for k, v in pi.items():
            if isinstance(v, str) and v:
                # "city" here is the header's "Durham, NC"; a form's City field
                # wants address.city, so this one is filed as location.
                flat[f"personal.{'location' if k == 'city' else k}"] = v
        # Forms ask for first and last name separately, and a bare "Name"
        # wants the whole thing.
        if pi.get("name"):
            flat["personal.first_name"] = flat["personal.given_name"] = pi["name"]
        if pi.get("surname"):
            flat["personal.last_name"] = flat["personal.family_name"] = pi["surname"]
        if self.full_name:
            flat["personal.name"] = self.full_name

        # Education answers derived from the career record, so School, Degree
        # and Major fields answer themselves; answers.yaml entries win on a clash.
        eds = self.career.get("education_details") or []
        if eds and isinstance(eds[0], dict):
            ed = eds[0]
            for key, src in (("school", "institution"), ("university", "institution"), ("degree", "education_level"),
                             ("major", "field_of_study"), ("discipline", "field_of_study"),
                             ("field_of_study", "field_of_study"), ("gpa", "final_evaluation_grade")):
                if ed.get(src):
                    flat.setdefault(f"education.{key}", str(ed[src]))
            # The start of the degree, as written and as month and year, so an
            # Education section's "Start date month/year" has a bank answer.
            start = str(ed.get("start_date") or "").strip()
            if start:
                flat.setdefault("education.start_date", start)
                m = re.fullmatch(r"(\d{1,2})[/-](\d{4})", start) or re.fullmatch(r"(\d{4})-(\d{1,2})", start[::-1])
                if m and re.fullmatch(r"(\d{1,2})[/-](\d{4})", start):
                    month, year = int(m[1]), m[2]
                    if 1 <= month <= 12:
                        flat.setdefault("education.start_date_month", _MONTH_NAMES[month - 1])
                        flat.setdefault("education.start_date_year", year)
                else:
                    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", start)
                    if m:
                        flat.setdefault("education.start_date_month", m[1].capitalize())
                        flat.setdefault("education.start_date_year", m[2])
                    elif re.fullmatch(r"\d{4}", start):
                        flat.setdefault("education.start_date_year", start)
        if self.full_name:
            flat["personal.full_name"] = self.full_name
        if pi.get("phone"):
            flat["personal.phone"] = f"{pi.get('phone_prefix', '')} {pi['phone']}".strip()
        # Forms say "Postal Code" or "Zip Code" for the same thing; derived
        # here so an alternate address carries its own.
        if flat.get("address.zip"):
            flat.setdefault("address.postal_code", flat["address.zip"])
            flat.setdefault("address.zip_code", flat["address.zip"])
        return flat

    def lookup(self, question: str, min_score: float = 0.5, min_coverage: float = 1 / 3,
               sections: tuple[str, ...] | None = None) -> tuple[str | None, str | None]:
        """Best match for a form label. Returns (answer, matched_key).

        Scored against the key's leaf name, with the section prefix used only to
        break ties. Two rules keep it honest: a weak best match is a miss, and
        so is a tie — "will you require sponsorship" without a country matches
        the US and UK keys equally well, and the right move there is to ask you,
        not to pick one. `min_score` is the share of the key's words the
        question must contain; an essay question is held to all of them.
        """
        # The parenthetical and the example clause ("(i.e. H1-B visa)",
        # "e.g. ...") say nothing the key must cover; nor does a company
        # name. Without this, "Will you require sponsorship from Northwood
        # (i.e. H1-B visa)" never reaches requires_us_sponsorship.
        # …but a short parenthetical is the question ("Location (City)").
        question = re.sub(r"\((?:[^)]*\b(?:e\.g\.|i\.e\.|such as|for example)\b[^)]*|[^)]{24,})\)|\b(?:e\.g\.|i\.e\.)[^?.]*", " ", question)

        flat = self.flat_answers()
        words = _words(question) | _region_synonyms(question)
        if not words:
            return None, None
        asked = max(1, len([w for w in words if len(w) > 1]))

        scored: list[tuple[float, int, float, str]] = []
        for key in flat:
            if sections and not key.startswith(sections):
                continue  # the form's own heading says which part of the bank applies
            section, _, leaf = key.rpartition(".")
            leaf_words = _words(leaf.replace("_", " "))
            if not leaf_words:
                continue
            hits = sum(1 for kw in leaf_words if any(_akin(kw, qw) for qw in words))
            if not hits:
                continue
            # A multi-word key needs a distinctive word in common, not only a
            # filler one: "work" alone let "Have you ever been employed by
            # TELUS?" land on remote_work and answer Yes.
            if len(leaf_words) > 1 and not any(
                kw not in _WEAK_WORDS and any(_akin(kw, qw) for qw in words) for kw in leaf_words
            ):
                continue
            score = hits / len(leaf_words)
            if score < min_score:
                continue
            # The key must also account for a fair share of the question. One
            # shared word in a long question — "name" in "if you were referred,
            # write their name in the box below" — is coincidence, not a match;
            # a long question is the grounded answerer's job.
            if hits / asked < min_coverage:
                continue
            section_words = _words(section.replace(".", " ").replace("_", " "))
            bonus = (
                sum(1 for sw in section_words if any(_akin(sw, qw) for qw in words))
                / len(section_words)
            ) if section_words else 0.0
            scored.append((score, hits, bonus, key))

        if not scored:
            return None, None
        scored.sort(key=lambda t: t[:3], reverse=True)
        top = [t for t in scored if t[:3] == scored[0][:3]]
        # "Legal Name (First Name Last Name)" asks for the whole name, though
        # first_name and last_name each match it word for word.
        if {"first", "last"} <= words and any(
                t[3].endswith(("first_name", "last_name", "given_name", "family_name")) for t in top):
            for whole in ("personal.full_name", "personal.name"):
                if whole in flat:
                    return flat[whole], whole
        if len(top) > 1:
            # A tie is nearly always the same question asked per region —
            # "require sponsorship?" matching the US, EU and UK keys equally,
            # because a US form means the US without saying so. default_region
            # names which region an unqualified question refers to. Any tie it
            # cannot break is real ambiguity, and the answer is to ask.
            region = str((self.answers.get("work_authorization") or {}).get("default_region", "")).lower()
            narrowed = [t for t in top if region and region in _key_words(t[3])]
            if narrowed:
                top = narrowed
            # What survives may still be several keys — "require sponsorship
            # for employment visa status" fits the visa key and the sponsorship
            # key equally. That is only ambiguous if they would answer
            # differently; a tie between keys carrying the same answer has one
            # outcome, and that outcome is safe to give.
            answers = {flat[t[3]] for t in top}
            if len(answers) > 1:
                # "Durham" and "Durham, NC" are one answer at two precisions
                # (address.city and the résumé header's location), not a
                # disagreement; a form's own field wants the address one.
                if len({a.split(",")[0].strip().lower() for a in answers}) == 1:
                    top.sort(key=lambda t: not t[3].startswith("address."))
                else:
                    return None, None
        return flat[top[0][3]], top[0][3]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOPWORDS}


def _key_words(key: str) -> set[str]:
    return _words(key.replace(".", " ").replace("_", " "))


def _region_synonyms(question: str) -> set[str]:
    """Forms say "the United States"; the keys say "us". Bridge the two so a
    spelled-out region scores as a direct hit rather than a tie."""
    q = question.lower()
    out: set[str] = set()
    if "united states" in q or "u.s." in q or re.search(r"\b(usa|america)\b", q):
        out.add("us")
    if "united kingdom" in q or "britain" in q or "u.k." in q:
        out.add("uk")
    if "european union" in q or "europe" in q:
        out.add("eu")
    return out


# What may follow a shared stem for two words to count as one: inflections
# and the usual derivations, nothing else.
_SUFFIXES = {"", "s", "es", "d", "ed", "e", "ing", "ings", "ion", "ions", "ation", "ations",
             "ly", "er", "ers", "y", "ies", "ity", "ship"}


def _stems(word: str) -> set[str]:
    """The word and every form of it with one known suffix taken off, when
    what remains is long enough to be a stem: graduation -> {graduation,
    graduat, gradu}; graduating -> {graduating, graduat}."""
    out = {word}
    for s in _SUFFIXES:
        if s and word.endswith(s) and len(word) - len(s) >= 4:
            out.add(word[: -len(s)])
    return out


def _akin(a: str, b: str) -> bool:
    """Same word allowing for inflection — 'require' matches 'requires',
    'authorized' matches 'authorization' — and not merely the same opening
    letters: 'interest' is not 'international', 'person' is not 'personal',
    'state' is not 'statement'. Form labels never use the exact wording of a
    config key, but a shared prefix alone let a platform preference answer
    "why are you interested in us?"."""
    return a == b or bool(_stems(a) & _stems(b))


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"]

# Words that appear in many keys and many questions; on their own they do not
# identify a key, only reinforce a match made by a more specific word.
_WEAK_WORDS = {"work", "date", "status", "type", "level", "number", "id", "period", "rate", "range", "name", "us"}

_STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "did", "you", "your", "yours",
    "we", "our", "please", "select", "enter", "provide", "of", "for", "to", "in",
    "on", "at", "and", "or", "if", "this", "that", "will", "would", "can", "may",
    "what", "which", "any", "all", "have", "has", "be", "been", "with", "from",
    "required", "optional", "field", "question", "answer", "currently",
}
