# Product brief: resume-tailor

## Goal

Turn one job posting plus the owner's complete career record into a one-page résumé whose every claim traces back to that record, and optionally carry the application through a real browser form, unattended, without ever guessing at an answer the record does not hold.

## Primary audiences

- The owner, applying to internships at volume during recruiting season.
- Later, other students who maintain their own `career.yaml` and `answers.yaml`.

## Primary journeys

1. Paste a posting, get a tailored PDF and a report of unsupported claims and gaps.
2. Apply to one posting interactively through the MCP server, reviewing before submit.
3. Run a queue of postings unattended; every posting lands in a named status with a screenshot.
4. Discover new internships from the community lists, judge fit twice, apply to what passes.
5. Watch outcomes on the local dashboard; approve held submissions; log in once per site.

## Product principles

- Nothing in a résumé that is not in the record. Gaps are reported, never filled.
- Batch mode removes the approval step, not the checking.
- A required field with no honest answer stops that one posting, never the run.
- No CAPTCHA solving, no identity verification, no account creation beyond the hosts the owner lists.

## Sources of truth

- `~/.resume-tailor/career.yaml` and `answers.yaml` (local, never committed) for every fact and form answer.
- The posting text itself for requirements and keywords.
- Community internship lists named in `answers.yaml` for discovery.

## Non-goals

- A hosted multi-user service.
- Working around bot checks or terms of service.

## Success indicators

- The checks in `.ai/project.yaml` pass on every push.
- Every test script passes with model calls stubbed.
- A tailored PDF's audit report lists zero unsupported claims for a record that covers the posting.
