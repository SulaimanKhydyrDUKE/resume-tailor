---
name: chrome-apply
description: Apply to internship postings continuously in the user's own Chrome (Claude in Chrome extension), tier-1 companies first then newest, filling every field from the user's saved facts and answer bank without stopping to ask; unanswerable questions are logged and the run moves on. Use when the user asks to apply, run the pipeline, work the queue, or continue applying.
---

# Applying in the user's Chrome, without stopping

You drive the user's **own** Chrome through the Claude in Chrome tools
(`mcp__claude-in-chrome__*`). Portals that refuse an automated browser (Lever,
SmartRecruiters, Netflix, IBM, Apple…) accept it, because it is the user's
real signed-in browser. Records, facts and the queue come from the
`resume-tailor` MCP server.

## Before the first posting (once per session, not per posting)

1. `intake_facts()`: every fact the user answered once (address, zip,
   permanent address, birthday, college, high school, employer, availability,
   demographic defaults, self-ratings, how-heard default). If `missing` is not
   empty, tell the user in one line to run `resume-tailor intake` and keep
   going: the missing ones are logged as pending when a form needs them. Never
   ask for a fact that is in this map, and never ask a question the files
   answer: grep `~/.resume-tailor/answers.yaml`, `career.yaml`,
   `resume/base.yaml` and call `answer_form_question` first.
2. The résumé PDF is the user's own (`~/.resume-tailor/resume/base.pdf`, or a
   copy named `First_Last_resume.pdf`). Do not tailor unless asked.
3. Order: `resume-tailor discover` refreshes the pool; then tier 1
   (`answers.yaml → search.company_tiers.first`) first, then newest first.
   `assist_next` gives the held queue; the priority list is
   `output/priority-list.csv` when present.
4. `touch .claude/keep-going` so the Stop hook keeps the run alive; the user
   can also schedule `/loop 30m /chrome-apply`.

## One posting

- Open it in a tab of the session's tab group. `get_page_text` / `find` to
  read the whole form before typing.
- Fill from, in order: `intake_facts` → the brief's decided answers
  (`assist_brief`) → `answer_form_question` → the record. Type text with
  keystrokes (a value set by script can submit blank); pick options by
  clicking them; upload the PDF with `file_upload` (for a hidden or shadow-DOM
  input, add a proxy `<input type=file>` with `javascript_tool`, upload to it,
  copy its `files` to the real input and fire `change`).
- **Act without asking** on everything routine: privacy notices, "I agree",
  "I acknowledge", "I certify the information is accurate", data-processing
  consents required to apply, a typed-name signature (`facts.signature_name`)
  and today's date, cookie banners (decline non-essential), "already in our
  talent network?" (no thanks, continue). Optional marketing or EEO-sharing
  consents stay unticked. Voluntary demographics use the `facts`
  demographics (default decline). Do not stop to read policies.
- Eligibility: read the posting for bars (citizenship, clearance, graduation
  window, PhD-only); a bar the facts fail is `assist_record(id, "skipped",
  "not eligible: …")`, next.
- Submit, then prove it: a confirmation page, `/thanks` or `/confirmation`
  URL, or an "Applied" badge. Record `assist_record(id, "applied", "…")`.
  One application per company: skip the company's other postings.
- Companies in `search.approve_before_submit`: fill completely, then show
  the user a one-paragraph summary and submit on their yes.

## When something cannot be answered (rare)

Do **not** stop the run and do not wait for approval. Leave the form filled
as far as it goes in its tab, then:

1. `note_pending(question, company, role, url, entry_id, tab)` (written to
   `output/pending-questions.jsonl`; the user sees it with
   `resume-tailor pending` and answers with `resume-tailor pending --answer N
   "…"`, which also teaches the bank),
2. `assist_record(id, "needs_review", "pending: <question>")`,
3. open the next posting in a new tab.

What counts: an essay that must be in the user's words (draft one from the
record only if the user has said essays may be drafted, and still log it for
approval), a video, a preference nobody recorded. A missing intake fact is
also logged this way.

## Never

- Create an account or type a password: a portal that needs one is
  `assist_record(id, "needs_login", "<portal> needs an account")` and the run
  moves on (the user signs in later; then the posting is driven like any
  other).
- Solve a captcha: `blocked`, next.
- Invent a fact, a number or an employer; never submit a required field
  empty; never click submit twice without the inbox (`inbox_confirmation`)
  or the page proving the first one failed.

## Ending

Remove `.claude/keep-going` only when the queue is empty or the user says
stop; end with: applied (with proof), needs_login (by portal), pending
questions (count), skipped.
