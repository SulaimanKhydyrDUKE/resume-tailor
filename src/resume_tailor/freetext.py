"""Answers to a form's required free-text questions — "Why do you want to
work here?", "Tell us about a project you're proud of" — written from the
career record and the posting, then gated like a resume bullet.

Only required questions are answered; an optional essay left blank costs
nothing, an unforced essay is an unforced risk. Factual statements about the
candidate are held to the same rule as everywhere else: every number must
appear in the cited facts (or in the posting itself), every named technology
or employer must exist in the record. Motivation may be stated plainly —
"I want to work on X" is not a fact that can be fabricated — which is why the
deterministic gates decide here rather than the entailment audit, whose
question ("does the source state this?") has no sensible answer for a sentence
about wanting something.
"""
from __future__ import annotations

import re

from . import gates
from .models import FreeTextAnswer
from .profile import Profile

MAX_WORDS = 160
ATTEMPTS = 2  # the model is stochastic; one draft that fails a gate is not a verdict

# Evidence ids the model sometimes writes into the prose — "(exp0.r0)" — as
# if the form were a paper. They belong in facts_used, not in the answer.
_INLINE_ID = re.compile(r"\s*\(\s*(?:exp|skill|edu|lang|proj)\d[\w.]*(?:\s*,\s*(?:exp|skill|edu|lang|proj)\d[\w.]*)*\s*\)")


async def answer_question(question: str, posting_text: str, profile: Profile,
                          max_chars: int | None = None, words: int | None = None) -> str | None:
    from .tailor import _career_system, _cited_text, _parse

    index = profile.evidence_index()
    record_text = " ".join(index.values())
    allow = gates.build_entity_allowlist(record_text + " " + posting_text)
    # A field's own length limit, when it has one, is the binding one: a
    # truncated essay reads worse than a short one.
    words = words or (min(120, max(40, max_chars // 7)) if max_chars else 120)
    long_form = words > 200  # an essay or a cover letter: paragraphs, not a paragraph
    for _ in range(ATTEMPTS):
        out = await _parse(
            _career_system(profile),
            f"""Answer this application question for the candidate: first person, at most \
{words} words{f' and {max_chars} characters' if max_chars else ''}, plain prose\
{', in several paragraphs separated by blank lines' if long_form else ''}, no headings, no bullet points.

QUESTION
{question}

THE POSTING (so the answer speaks to what they care about)
{posting_text[:6000]}

Rules:
- Every factual statement about the candidate — what was built, where, with what, \
any number — must come from the evidence index, and facts_used must list the ids \
it rests on. Never invent experience, employers, technologies or numbers.
- The ids go in facts_used only; never write them into the answer itself.
- Interest and motivation may be stated directly and specifically. Refer to the \
company and the role as the posting names them.
- No flattery padding, no "I am confident that", no restating the question.""",
            FreeTextAnswer, effort="medium",
        )

        cleaned = _INLINE_ID.sub("", out.answer)
        if long_form:
            text = "\n\n".join(" ".join(par.split()) for par in re.split(r"\n\s*\n", cleaned) if par.strip())
        else:
            text = " ".join(cleaned.split())
        ids = [i for i in out.facts_used if i in index]
        if not text or len(text.split()) > max(MAX_WORDS, int(words * 1.3)) or not ids or (max_chars and len(text) > max_chars):
            continue
        cited = _cited_text(index, ids) + " " + posting_text
        if gates.check_numerals(text, cited, []) or gates.check_entities(text, allow):
            continue
        return text
    return None
