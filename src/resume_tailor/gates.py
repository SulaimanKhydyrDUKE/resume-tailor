"""Deterministic checks. No model calls, no tokens, no judgement.

These run before any critic, so the expensive semantic pass never spends
attention on what arithmetic can settle. Each returns a list of Violation.

The scoping rule that makes the numeral gate work: a bullet's numbers are
checked against **the facts that bullet cites**, never against every number in
the record. Global membership launders fabrication — if "12" appears anywhere in
your history, a global check happily passes "led a team of 12" attached to a
role where you led nobody.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from dataclasses import dataclass

# Font Awesome and friends live here. A glyph from this range in the extracted
# text layer means an icon font leaked into what a parser reads.
_PUA = re.compile(r"[-\U000f0000-\U000ffffd]")
_NUM = re.compile(r"\d[\d,.]*")
_WORDY = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]*")

# Words that look like proper nouns at the start of a sentence or in a heading
# but are ordinary vocabulary. Without this the entity gate fires on every bullet.
_COMMON = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "by", "with", "from",
    "built", "led", "cut", "grew", "shipped", "designed", "developed", "reduced",
    "improved", "migrated", "owned", "drove", "delivered", "launched", "wrote",
    "created", "managed", "scaled", "automated", "refactored", "introduced",
    "team", "teams", "engineer", "engineers", "service", "services", "system",
    "systems", "data", "code", "test", "tests", "api", "apis", "time", "times",
    "user", "users", "customer", "customers", "product", "release", "releases",
    "latency", "throughput", "reliability", "uptime", "cost", "costs", "growth",
    "experience", "responsible", "including", "across", "through", "using", "into",
    "new", "first", "per", "week", "weeks", "month", "months", "year", "years",
    # Resume verbs. A bullet opens with one, capitalised, and none of them is a name.
    "implemented", "worked", "reached", "served", "leveraged", "integrated", "deployed",
    "configured", "won", "partnered", "iterated", "processed", "streamlined", "enhanced",
    "supported", "engineered", "architected", "maintained", "optimized", "optimised",
    "collaborated", "modified", "generated", "visualized", "contributed", "ran", "running",
    "used", "utilized", "applied", "performed", "achieved", "increased", "decreased",
    "enabled", "established", "executed", "coordinated", "mentored", "trained", "tested",
    "debugged", "resolved", "analyzed", "researched", "presented", "documented", "added",
    "extended", "containerized", "provisioned", "monitored", "instrumented", "measured",
    "benchmarked", "tuned", "validated", "verified", "reviewed", "prototyped", "evaluated",
    "adopted", "consolidated", "simplified", "replaced", "rewrote", "rebuilt", "restructured",
    "upgraded", "fixed", "co", "building", "detecting", "serving", "improving", "cutting",
}


@dataclass
class Violation:
    kind: str
    claim: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.claim[:80]!r}: {self.detail}"


def _norm_number(tok: str) -> str:
    """1,000 -> 1000; 40.0 -> 40; trailing punctuation dropped."""
    t = tok.replace(",", "").rstrip(".")
    if t.endswith(".0"):
        t = t[:-2]
    try:
        f = float(t)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return t


def numbers_in(text: str) -> set[str]:
    return {_norm_number(m.group()) for m in _NUM.finditer(text)}


def check_numerals(claim: str, cited_text: str, declared_derived: list[str]) -> list[Violation]:
    """Every number in the claim must appear in the text it cites.

    A number the model declares as derived (a ratio computed from two figures
    the record does state) is allowed through but reported, because arithmetic
    over real data is legitimate and a hard gate on it deletes true content.
    """
    allowed = numbers_in(cited_text)
    derived = {_norm_number(m.group()) for d in declared_derived for m in _NUM.finditer(d)}
    out: list[Violation] = []
    for n in numbers_in(claim):
        if n in allowed:
            continue
        if n in derived:
            out.append(Violation("numeral-derived", claim,
                                 f"{n} is computed, not stated. Declared: {'; '.join(declared_derived)}"))
            continue
        out.append(Violation("numeral", claim, f"{n} does not appear in the cited evidence"))
    return out


def _token(w: str) -> str:
    """A word as the gate compares it: the full stop that ends a sentence is
    punctuation, not part of the name — "FastAPI." is FastAPI."""
    return w.rstrip(".")


def build_entity_allowlist(record_text: str) -> set[str]:
    out: set[str] = set()
    for w in _WORDY.findall(record_text):
        w = _token(w).lower()
        if w:
            out.add(w)
            out.update(p for p in w.split("-") if p)
    return out


_SENTENCE_END = re.compile(r"[.!?;:]\s*$")


def _name_like(w: str) -> bool:
    return bool(w) and (w[0].isupper() or any(c.isdigit() for c in w) or any(c in w for c in "+#./_"))


def check_entities(claim: str, allowlist: set[str]) -> list[Violation]:
    """Named technologies and proper nouns must exist somewhere in the record.

    Scoped globally on purpose, unlike numerals: a skill genuinely learned at one
    job may legitimately be described at another, whereas a metric may not move.

    Capitalisation alone is not evidence of a name at the start of a sentence —
    every bullet opens with a capitalised verb, and "Implemented" is not an
    entity. There, only a digit or a symbol (Python3, C++, Next.js) marks one.
    A hyphenated compound ("AI-driven", "real-world") is judged by its parts:
    each must be in the record or be plain vocabulary.
    """
    out = []
    for m in _WORDY.finditer(claim):
        w = _token(m.group())
        low = w.lower()
        if not w or low in _COMMON or len(w) < 3 or low in allowlist:
            continue
        if "-" in w and all(p.lower() in allowlist or p.lower() in _COMMON or not _name_like(p)
                            for p in w.split("-") if p):
            continue
        at_sentence_start = m.start() == 0 or bool(_SENTENCE_END.search(claim[:m.start()]))
        marked = any(c.isdigit() for c in w) or any(c in w for c in "+#./_")
        if marked or (w[0].isupper() and not at_sentence_start):
            out.append(Violation("entity", claim, f"{w!r} appears nowhere in your career record"))
    return out


def check_pua(text: str) -> list[Violation]:
    """Icon-font glyphs in the text layer, which a parser reads as garbage."""
    hits = _PUA.findall(text)
    if not hits:
        return []
    return [Violation("pua-glyph", "", f"{len(hits)} private-use glyph(s) in the PDF text layer — an icon font leaked in")]


def check_citations(ids: list[str], index: dict[str, str]) -> list[Violation]:
    return [Violation("citation", "", f"cites {i!r}, which is not in the evidence index")
            for i in ids if i not in index]


def check_rendered(text: str, must_contain: dict[str, str]) -> list[Violation]:
    """Post-render assertions on the extracted text layer.

    The document is judged on what a parser gets out of the PDF, not on the HTML
    that went in. A contact detail lost to the render is lost to the reader.
    """
    out = list(check_pua(text))
    flat = re.sub(r"\s+", "", text).lower()
    for label, value in must_contain.items():
        if value and re.sub(r"\s+", "", value).lower() not in flat:
            out.append(Violation("missing-in-pdf", label, f"{value!r} did not survive into the text layer"))
    if len(text.strip()) < 400:
        out.append(Violation("thin-text", "", f"text layer is only {len(text.strip())} chars — the render may have failed"))
    if any(unicodedata.category(c) == "Co" for c in text):
        out.append(Violation("private-use", "", "private-use characters present in the text layer"))
    return out


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def check_bullets_survive(html: str, text: str) -> list[Violation]:
    """Every list item the HTML carries must come out of the PDF whole. A
    bullet the page cut off ("…LaTeX editing, and university comm" stood on
    311 résumés) reads as a typo to a human and a broken sentence to a
    parser; one that starts with punctuation (", won the ACM…") is the
    composer's join gone wrong. Compared on letters and digits only, so
    dashes, quotes and line breaks the extractor rewrites do not count."""
    flat = _letters(text)
    out: list[Violation] = []
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", html, re.S):
        item = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        item = re.sub(r"\s+", " ", item).strip()
        if len(item) < 12:
            continue
        if item[0] in ",;:.":
            out.append(Violation("leading-punctuation", item, "a bullet starts with punctuation"))
        if _letters(item) not in flat:
            out.append(Violation("clipped", item, "this bullet did not come out of the PDF whole"))
    return out


def repetition_density(text: str, cap: int = 4) -> list[Violation]:
    """Keyword stuffing, mechanically. A term repeated past `cap` reads as
    stuffing to a human and is what keyword-optimisation degenerates into."""
    counts: dict[str, int] = {}
    for w in _WORDY.findall(text.lower()):
        if len(w) > 3 and w not in _COMMON:
            counts[w] = counts.get(w, 0) + 1
    return [Violation("stuffing", "", f"{w!r} appears {c} times")
            for w, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c > cap]
