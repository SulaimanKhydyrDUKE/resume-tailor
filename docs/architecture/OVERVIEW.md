# Architecture overview

## Repository layout

```text
resume-tailor/
├── .ai/ .claude/ .github/ docs/   # Agent policy, skills, automation, docs
├── profile/                       # Example career.yaml, answers.yaml, queue.yaml, env
├── src/resume_tailor/             # The package
│   ├── cli.py, server.py          # CLI entry point; MCP server entry point
│   ├── tailor.py, compose.py, gates.py, qa.py, judge.py   # Tailoring chain and judges
│   ├── render.py, styles/, ats.py # PDF rendering and text-layer keyword coverage
│   ├── profile.py, models.py, llm.py, freetext.py         # Record, schemas, model access
│   ├── apply.py, batch.py, queue.py, planner.py           # Browser filling and unattended runs
│   ├── discover.py, outreach.py, mailbox.py, mailscan.py  # Discovery, recruiter outreach, e-mailed codes
│   └── dashboard.py, static/      # Local review dashboard
└── tests/                         # Standalone scripts, one per area, model calls stubbed
```

## Application

Two halves that work independently. Tailoring reads a posting and the career record, selects evidence before writing, drafts, runs deterministic gates and a JD-blind audit, renders a PDF, and measures keyword coverage from the PDF's text layer. Applying owns its own Playwright browser (so it can attach files), answers each form field through a fixed ladder (answer bank, boilerplate rules, grounded essay, grounded question-answerer, otherwise `needs_review`), and requires two independent judges to pass before submit.

## Architectural boundaries

- Model access goes through `llm.py`; provider and model ids come from environment variables.
- Personal data lives under `RESUME_TAILOR_PROFILE` (default `~/.resume-tailor`), outputs under `RESUME_TAILOR_OUTPUT`. Neither is inside the repository.
- The browser is created only inside the applying half; tailoring must stay browser-free.
- New dependencies, hosts on which accounts may be created, and anything that relaxes the audit need a specification.

## Decision records

For a consequential, durable choice, add a short file under `docs/architecture/decisions/`.
