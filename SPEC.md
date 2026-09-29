# resume-tailor — specification and architecture

Status: implemented · Maturity: beta · Last aligned with the code: 2026-09-28

resume-tailor takes one job posting and the owner's complete career record
and produces a one-page résumé whose every line traces back to that record.
It can then carry the application through a real browser form, either
interactively (over MCP, with the person reviewing before submit) or as an
unattended loop over the community internship lists. This document states
what the system must do (the specification) and how the code is arranged to
do it (the architecture). Where the implementation or the tests fall short
of it, the README's "Gaps" section says so.

## 1. Users and situation

- **The owner**, a student applying to Summer 2027 internships at volume:
  hundreds of postings, one record, forms that ask the same forty questions.
- **Later, other students** who keep their own `career.yaml` and
  `answers.yaml` under `~/.resume-tailor/`.

Personal data (the record, the answer bank, credentials, browser profiles)
lives outside the repository under `~/.resume-tailor/`; run output (PDFs,
screenshots, state files) lives under `output/`, which is git-ignored.

## 2. Requirements

Each requirement is stated as observable behaviour. R-numbers are referenced
by the tests and by the README's gaps section.

### 2.1 Tailoring

- **R1 · Grounded content.** Every sentence on the résumé is either copied
  verbatim from the record (names, employers, dates, degrees, links) or is a
  rewording of a record entry that cites that entry's id. A claim that
  cites nothing, or cites an id that does not exist, never reaches the PDF.
- **R2 · Numerals and names.** Every numeral in a bullet appears in that
  bullet's own cited facts; every capitalised name is in the record's
  entity allowlist. Violations are rejected deterministically, without a
  model.
- **R3 · Blind audit.** Each surviving claim is shown to a critic that sees
  only the claim and the verbatim text it cites, never the posting. A claim
  the critic cannot support is dropped, never rewritten.
- **R4 · One page, checked from the PDF.** The PDF is rendered by the
  browser, trimmed until it fits one page, and its text layer is
  re-checked for R1–R2 and scored for keyword coverage. The HTML is never
  the thing checked.
- **R5 · Skeleton mode.** When `~/.resume-tailor/resume/base.yaml` exists,
  the tailored résumé keeps the skeleton's roles, order and bullet count,
  and each bullet is a rewording of the owner's own toward the posting's
  terms, falling back to the original wording when the rewording fails a
  gate.
- **R6 · Cardinality from data.** How many bullets each role gets is decided
  from the record and the posting, not by a fixed template.

### 2.2 Applying

- **R7 · Never guess.** A form field is answered from, in order: the answer
  bank (word-matched), boilerplate rules (consent boxes, "how did you
  hear"), a grounded essay writer for required prose, a grounded
  question-answerer that must cite record keys, and a whole-form planner
  whose answers must be real options resting on real keys. A required field
  none of these can decide sends that posting to `needs_review`; the run
  goes on.
- **R8 · Options are matched whole-word.** "No" never lands on "Not
  applicable".
- **R9 · One named status per attempt**, with a screenshot: `applied`,
  `tailored_only`, `ready_not_submitted`, `needs_login`, `blocked`,
  `needs_review`, `skipped`, `fit_rejected`, `awaiting_approval`, `error`.
- **R10 · Two judges before any submission.** A hiring-manager lens and a
  screener lens each score the PDF's text against the posting; both must
  reach the configured bar, a hold feeds the judges' notes into a fresh
  tailor up to `max_revisions` times, and an eligibility barrier ends the
  attempt as `fit_rejected`.
- **R11 · Accounts, captchas, submissions.** The tool creates accounts only
  on portals the owner lists (by host, or by portal kind such as iCIMS when
  the kind's home domain is listed), never solves a captcha, never submits
  with a required field empty, and submits over MCP only with an explicit
  confirmation token.
- **R12 · Apply once per company** when the owner says so; a second posting
  at the same company is skipped.
- **R13 · Sign-in walls.** A wall the tool may not or cannot pass is
  recorded as `needs_login` with the address to open by hand; a one-time
  code or verification link e-mailed by the portal is read from the owner's
  inbox when the mailbox is configured.

### 2.3 Unattended loop

- **R14 · Discovery.** The SimplifyJobs listing feed (ETag-cached) and four
  README-table lists are merged; the owner's filters (term, US-only,
  category, title, company blacklist, apply-once) select; retries are
  capped (`RETRY_CAP=3`, `LOGIN_RETRY_CAP=2`) unless a record is re-queued.
- **R15 · Shared state.** `output/batch-state.json` is written after every
  attempt under a file lock with a merge, so eight workers and the
  dashboard share one file without losing entries.
- **R16 · Resume versioning.** A change that makes earlier PDFs wrong bumps
  `RESUME_VERSION`; a retry whose cached PDF is older is re-tailored.
- **R17 · Dashboard.** A local page shows every attempt by outcome, the
  inbox's stage per company, and lets the owner review, log in, approve or
  mark a posting by hand.

### 2.4 Mail and outreach

- **R18 · Inbox to stages.** Confirmation, assessment, interview, offer and
  rejection mail is read over IMAP and folded into one stage timeline per
  company.
- **R19 · Recruiter notes.** One e-mail per company, to an address that was
  found (a reply-to, a careers page, a web search verified against the page
  it cites), never a pattern-guessed one; a named person before a shared
  mailbox; never after a rejection; weekdays in working hours; a daily cap;
  connection failures waited out, never blamed on the company.

### 2.5 Trust boundary

- **R20 · Third-party text is data.** Text from a posting, a form, a web
  page or an e-mail is never an instruction. What a person cannot see on a
  page (hidden by size, colour, position, clipping or `aria-hidden`) is not
  read; invisible Unicode is stripped; sentences addressed to an automated
  reader are removed before any model sees them and are recorded on the
  attempt; every model call opens with a guard naming quoted text as data.

## 3. Architecture

Three layers, each usable without the ones after it. Everything runs on one
asyncio loop; Playwright is the only browser and also the PDF renderer.
Model access goes through `llm.py`, which picks the provider and model from
environment variables and paces calls per provider.

```
posting ──► tailor.py ──► gates.py ──► audit ──► compose.py ──► render.py ──► PDF text checks
              │ (R1–R6)                                                            │
              ▼                                                                    ▼
          judge.py (R10) ◄──────────────────────────────────────────── extracted PDF text
              │
              ▼
          apply.py (browser, R7–R9, R11, R13) ◄── planner.py / qa.py / freetext.py / profile.py
              │
              ▼
          batch.py (one attempt = one Outcome) ──► queue.py (RunState, R15) ──► dashboard.py (R17)
              ▲
          discover.py (R14)          mailscan.py (R18)     outreach.py (R19)     untrusted.py (R20)
```

### 3.1 Tailoring chain (`tailor.py`, `models.py`, `gates.py`, `compose.py`, `render.py`)

`tailor()` runs `read_posting` (a `JobSpec`) → `select_evidence` (which
record ids answer which requirement, how many bullets each role gets, the
gaps) → `draft_role` per role and `draft_skills`, in parallel →
`gates.deterministic_gate` (R1–R2, no tokens) → `audit_and_repair` (R3) →
`compose.document` → `render.render_to_fit_async` (R4) → `extract_pdf_text`
→ `gates.check_rendered` and `score_coverage` on the PDF's text layer.
Skeleton mode (R5) takes the `_tailor_on_base` branch and
`compose.document_from_base`. The model returns prose and evidence ids only;
the schemas carry no defaults or array-length constraints, so cardinality
rules live in code (R6). The record's evidence index is the cached system
prefix of every call.

### 3.2 Applying (`apply.py`, `profile.py`, `planner.py`, `qa.py`, `freetext.py`, `ats.py`)

`ApplySession` owns a persistent Chromium context. Form discovery is
JavaScript evaluated in the page, walking shadow roots; fields get stable
`rt-N` ids that are reassigned on every navigation. `detect_blocker`
classifies a page as `login_required`, `bot_check`, `posting_gone`,
`already_applied` or `no_form_found`, measuring captcha widgets from the
document that holds them. `ats.py` knows where each portal's form lives and
which portals need an account. Answering is the ladder of R7.

### 3.3 Unattended loop (`discover.py`, `queue.py`, `batch.py`, `cli.py`, `dashboard.py`)

`discover.refresh` fetches and merges the lists under a lock; `select`
applies the filters and caps and shards by company. `queue.RunState` is the
shared state file (R15). `batch._process_one` is the per-posting pipeline:
pre-checks → posting text (scrubbed, R20) → tailor and judges (R10) → open
the form, click through an Apply control, create or sign in to an account
where allowed (R11, R13) → fill with repair rounds → review-page detection →
submit → post-submit verdict → one `Outcome` (R9) with the removed
third-party sentences in `flags`. `cli._watch` is the loop body; `cmd_start`
spawns it detached per worker. `dashboard.py` serves the local page (R17).

### 3.4 Mail and outreach (`mailbox.py`, `mailscan.py`, `outreach.py`)

`mailbox.py` fetches one-time codes during an application (R13).
`mailscan.py` builds `output/results.json` (R18). `outreach.py` finds and
verifies recruiter addresses, ranks a person above a mailbox, composes one
grounded sentence about the résumé, and sends over SMTP (R19).

### 3.5 Trust boundary (`untrusted.py`)

The visible-text walk, the invisible-character strip, the sentence scrub
for postings and form fields, and the guard prepended to every model call
(R20).

## 4. Configuration and data on disk

| What | Where | Notes |
|---|---|---|
| Career record, answer bank, résumé skeleton | `~/.resume-tailor/career.yaml`, `answers.yaml`, `resume/base.yaml` | shapes documented in `profile/*.example.yaml` |
| Keys and mailbox | `~/.resume-tailor/env` | keys documented in `.env.example` and `profile/env.example` |
| Browser profiles, saved logins, loop pids and logs | `~/.resume-tailor/` | |
| Attempts, listings cache, results, outreach log, PDFs, screenshots | `output/` | git-ignored |
| Package manifest, lockfile, container | `pyproject.toml`, `uv.lock`, `Dockerfile`, `.devcontainer/` | Python 3.11, Playwright Chromium |
| CI | `.github/workflows/ci.yml` runs the commands in `.ai/project.yaml` | compileall + every test script |

## 5. Verification

The tests are standalone scripts under `tests/`, one per area, every model
call stubbed, no network or credentials: `test_chain.py` (the whole
tailoring chain, rendering a real PDF; R1–R6), `test_batch.py` (answer
sources, submit-button choice, blocker verdicts, account rules, credential
handling; R7–R9, R11–R13), `test_discover.py` (filters, caps, sharding;
R14), `test_dashboard.py` (index, mark, answers snapshot; R17),
`test_mailscan.py` (R18), `test_outreach.py` (address filters, ranking,
outage handling, the send loop; R19) and `test_untrusted.py` (the scrub,
field scrubbing, and a real page in Chromium for the visible-text walk;
R20). `for t in tests/test_*.py; do python "$t" | tail -1; done` runs them
all; each prints PASS/FAIL per check and exits non-zero on any failure.

## 6. Out of scope

A hosted multi-user service; solving captchas or identity verification;
working around bot checks or terms of service; account creation beyond the
hosts the owner lists; any change that lets an unsupported claim reach a
PDF.
