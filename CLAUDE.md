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
