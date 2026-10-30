# Repository instructions

This repository is **resume-tailor**: a Python package under `src/resume_tailor/` (install with `pip install -e .`) that tailors a résumé to one job posting from a personal `career.yaml` record, and can drive the application through a Playwright browser. It ships a CLI (`resume-tailor`) and an MCP server (`resume-tailor-mcp`). Commands run from the repository root. Tests are standalone scripts under `tests/` that stub every model call and print PASS/FAIL lines.

## Read before changing anything

- `.ai/project.yaml` defines automation limits and the exact setup and check commands.
- `docs/product/PRODUCT_BRIEF.md` defines the product and its sources of truth.
- `docs/quality/DEFINITION_OF_DONE.md` defines completion evidence.
- `docs/architecture/OVERVIEW.md` explains the repository layout.
- For issue-driven work, read the complete issue and its comments. A specification in a later owner-approved comment is part of the contract.

## Commands

The commands in `.ai/project.yaml` are the only ones CI runs. Run them before claiming anything works.

## Project-specific rules

- Never run `batch`, `watch`, `start`, `submit`, `login`, `discover` or anything else that opens a browser against a real site, sends e-mail, or calls a model API from automation. Tests keep stubbing model calls and need no credentials.
- Nothing from `~/.resume-tailor/`, `output/`, or any real résumé, answer bank, mailbox or credential may be committed, quoted in an issue or PR, or written to a log.
- The anti-fabrication chain (evidence citation, deterministic gates, JD-blind audit) is the product. A change that lets an unsupported claim reach a PDF is a bug, whatever else it improves.

## Running the pipeline: an agent applying in your own Chrome

This is how the owner runs it day to day, and how anyone with their own
`~/.resume-tailor/` can replicate it. It is an interactive session the user
starts, not CI: the "never run … from automation" rule above is about issue
automation and tests, not this.

### One-time setup

1. `uv venv && uv pip install -e . && .venv/bin/python -m playwright install chromium`, then `resume-tailor init`
   and fill `~/.resume-tailor/career.yaml`, `answers.yaml` (`profile/*.example.*` show every key) and `env`.
   Put your résumé PDF at `~/.resume-tailor/resume/base.pdf`.
2. **`resume-tailor intake`**: answer once every personal fact a form can ask that the record does not hold
   (current and permanent address and zip, date of birth, college within the university, high school, most recent
   employer, availability dates, start-within-3-weeks, housing, relocation, related-to-employees, offer deadlines,
   top languages, interests, self-rating, clubs, how-you-heard default, demographics defaults, signature name).
   Saved to `answers.yaml → facts`; `resume-tailor intake --check` lists what is still missing. This is what lets
   the agent run without stopping to ask.
3. Register the MCP server with Claude Code: `claude mcp add resume-tailor -- "$PWD/.venv/bin/resume-tailor-mcp"`.
4. Install the **Claude in Chrome** extension and sign in to Chrome as yourself. The agent drives this real
   browser (portals such as Lever, SmartRecruiters, Netflix, IBM and Apple refuse the tool's Playwright browser
   but accept your own Chrome).
5. Open Claude Code in this directory. `.claude/settings.json` (checked in) allows the Chrome and resume-tailor
   tools without prompts and registers the keep-going Stop hook. Use the default permission mode: auto mode's
   classifier can still refuse ticking "I certify" boxes even with the allow rules. New hooks are read at session
   start (or open `/hooks` once).

### Running it

- Say "apply" or run `/chrome-apply` (`.claude/skills/chrome-apply/SKILL.md` is the full procedure). For a timer,
  `/loop 30m /chrome-apply`. The agent touches `.claude/keep-going`; the Stop hook keeps the turn alive until the
  queue is empty, the user says stop, or 30 blocks pass.
- Order: `resume-tailor discover` refreshes the pool; tier 1 (`search.company_tiers.first`) first, then newest.
- Afterwards: `resume-tailor pending` lists the questions the agent could not answer (each left open in its tab);
  `resume-tailor pending --answer N "…"` answers one and teaches the bank. `resume-tailor dashboard --open` shows
  every application.

### Rules the agent follows (and changes must keep)

- **Facts first, never ask what is on file.** Before a batch: `intake_facts` (MCP). Before any question to the
  user: grep `answers.yaml`, `career.yaml`, `resume/base.yaml` and call `answer_form_question`. Questions that
  remain are batched up front, never asked mid-form.
- **Autonomy on routine steps.** Privacy notices, required "I agree / acknowledge / certify accurate" boxes,
  typed-name signatures and today's date, cookie banners (decline non-essential), talent-network prompts: done
  without asking. Optional marketing/EEO-sharing consents stay unticked; voluntary demographics use the facts
  (default decline). Never stop to read a policy.
- **Never stop the run for a question.** Something truly unanswerable (an essay in the user's words, a video, a
  preference never recorded, a missing intake fact): leave the form open in its tab, `note_pending(...)` (MCP;
  `output/pending-questions.jsonl`), record the posting `needs_review`, open the next one.
- **Hard limits, unchanged:** no account creation and no typed passwords (`needs_login`, move on; the user signs
  in later), no captchas (`blocked`), no invented facts, no required field submitted empty, no second submit
  without proof the first failed, one application per company, companies in `search.approve_before_submit` are
  shown to the user before submit.
- Submissions are proven (confirmation page, `/thanks` or `/confirmation`, an "Applied" badge) and recorded with
  `assist_record`, so the unattended loop never retries them.

## Non-negotiable rules

- Complete real behavior. Do not substitute mock data, fake integrations, placeholder controls, skipped checks, or unresolved TODOs unless the specification explicitly allows them.
- Handle relevant loading, empty, error, and recovery states.
- Keep changes within the approved issue. Report useful adjacent findings instead of expanding scope silently.
- Never expose secrets or commit environment files.
- Never push to the default branch, merge a pull request, deploy, change production data, add a paid service, or make destructive infrastructure/database changes.
- Ask before changing authentication, authorization, privacy, data retention, public claims, or product meaning.
- Do not alter or delete user changes that are outside the task.

## Issue automation

- `ai:needs-spec` means specification only. Produce observable requirements, edge cases, non-goals, verification, and unresolved owner decisions. Do not implement.
- `ai:ready` means the owner approved unattended implementation. Work only on the branch created for that issue.
- A task is not complete because code was written. Run every applicable check and provide evidence against each acceptance criterion.
- If blocked, preserve useful work, state one precise blocker, and do not claim completion.

## Handoff format

Every implementation handoff must include:

1. Outcome delivered.
2. Acceptance-criteria evidence, item by item.
3. Commands run and exact pass/fail results.
4. Screenshots or other visual evidence for visible changes.
5. Files changed.
6. Assumptions, residual risks, and anything not verified.
