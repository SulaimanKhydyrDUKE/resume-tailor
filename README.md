# resume-tailor

Tailor a resume to one job posting, and drive the application through a real
browser.

Two halves that work independently:

- **Tailoring** needs no browser, no login, and no job board. Paste a
  description, get a PDF. This is where the leverage is.
- **Applying** adds a persistent browser that can read a form, fill it from a
  record you maintain, attach the PDF, and submit — with you reading it first.

## Quick start

```bash
git clone <this repo> && cd resume-tailor
uv venv && uv pip install -e .
.venv/bin/python -m playwright install chromium
.venv/bin/resume-tailor init          # creates ~/.resume-tailor/{career,answers,queue}.yaml and env
# fill in ~/.resume-tailor/career.yaml (everything you have done) and answers.yaml (form answers)
# put your API key in ~/.resume-tailor/env (see profile/env.example)
.venv/bin/resume-tailor tailor --text posting.txt        # one PDF, no browser
.venv/bin/resume-tailor watch --once --dry-run           # one discovery pass, nothing submitted
.venv/bin/resume-tailor start && .venv/bin/resume-tailor dashboard   # unattended, with a review page
```

Nothing personal lives in this repo: your record, answers, credentials and
site logins stay under `~/.resume-tailor/`, and every run's output is in
`output/` (git-ignored).

## Why it is built this way

Everything the resume says traces to `career.yaml`. The chain selects evidence
*before* it writes anything, and audits every claim afterwards against the same
record. A resume that needs a number the record does not have gets written
without the number, and the gap gets reported to you instead.

The keyword-coverage score is measured by extracting the finished PDF's text
layer — what a parser sees, not what the model generated.

## Install

```bash
cd resume-tailor
uv venv && uv pip install -e .
.venv/bin/python -m playwright install chromium
# Credentials — one of:
ant auth login                        # your Anthropic account (unset ANTHROPIC_API_KEY first: a set key shadows the profile)
export ANTHROPIC_API_KEY=sk-ant-...   # or an Anthropic API key
export OPENAI_API_KEY=sk-...          # or OpenAI — also set RESUME_TAILOR_PROVIDER=openai
# ...or put any of these in ~/.resume-tailor/env (one KEY=value per line); it is read automatically.
```

## Set up your record

```bash
.venv/bin/resume-tailor init
```

Creates `~/.resume-tailor/career.yaml` and `answers.yaml`.

`career.yaml` is everything you have ever done — every role, bullet, skill,
project. Write more than any one job needs; the tailorer selects from it per
application and **cannot select what is not there**. Bullets with real numbers
are worth ten without, and the tailorer will not manufacture one for you.

Three parts of it matter more than they look. The top-level `skills:` block is
your resume's skills section — a skill listed only there still gets an
evidence id, so it can be cited and can appear. Project `bullets:` are rendered
verbatim, never rewritten, and which projects appear is decided per posting.
Education `coursework:` entries are citable too, which is how a course can
answer a requirement no job has yet.

`answers.yaml` is the form answer bank: work authorization per country,
sponsorship, notice period, salary, relocation. Filled once, looked up per form.

## Tailor

```bash
.venv/bin/resume-tailor tailor jd.txt
pbpaste | .venv/bin/resume-tailor tailor -
```

You get a PDF plus a report: positioning, hard-requirement coverage, gaps the
posting asks for that you do not cover, and any claim the audit could not trace
back to your record. **Read the unsupported-claims list before sending
anything.**

## Apply to one job, interactively

Register the MCP server:

```bash
claude mcp add resume-tailor -- /absolute/path/to/resume-tailor/.venv/bin/resume-tailor-mcp
```

Then drive it in conversation. The `apply-to-job` skill in `.claude/skills/`
walks the loop: read posting, tailor, open form, answer from the bank, attach,
review, submit.

Log in to a site by hand once — the browser profile persists at
`~/.resume-tailor/browser-profile`, so later runs are already authenticated.

## Apply to many jobs, unattended

```bash
.venv/bin/resume-tailor init                    # also creates queue.yaml
# edit ~/.resume-tailor/queue.yaml — one entry per posting
.venv/bin/resume-tailor batch queue.yaml --dry-run   # fills and screenshots, never submits
.venv/bin/resume-tailor batch queue.yaml             # runs for real, unattended
```

No approval per application — that's the point of this mode. What replaces
approval is that a required field the answer bank cannot resolve **skips that
one posting and logs why**, rather than guessing at it; you review the misses
afterward instead of the whole batch beforehand.

Every posting lands in one of:

| Status | Meaning |
|---|---|
| `applied` | Filled, attached, submitted. |
| `tailored_only` | No `apply_url` given — resume generated, nothing submitted. |
| `ready_not_submitted` | `--dry-run` — filled and screenshotted, submit not clicked. |
| `needs_login` | The site wants a login this profile doesn't have. Log in by hand once, rerun the batch — that one session then covers every posting on that site. |
| `blocked` | A bot/verification check was on the page. Not attempted — this tool does not solve CAPTCHAs. |
| `needs_review` | A required field had no answer in `answers.yaml`, or the submit button was ambiguous. Check the screenshot; add the missing answer if it's a real gap. |
| `skipped` | Duplicate company (`apply_once_at_company`), or the tailored resume covered none of your experience — a poor-enough fit not worth spending an application on. |
| `fit_rejected` | One of the two independent match judges held it. The resume stays in `output/`, the reasons are in the report, nothing was submitted. |
| `error` | Something broke — network, a page that didn't load. Retried automatically next run. |

Results: `output/batch-report.csv` (one row per posting) and
`output/screenshots/` (one per attempt). The run is resumable — rerunning the
same command skips everything already `applied`, `tailored_only`, or
`skipped`, and retries only `error` / `needs_login` / `blocked`.

**On the "200 passwords" problem:** most modern application forms — Greenhouse,
Lever, Ashby, most direct-to-company pages — don't require an account at all.
Where a site does gate applications behind a login there are three paths:

- **Log in once by hand.** `resume-tailor login <company>` opens that posting
  in the tool's browser; sign in, and the tool takes over from there, applies,
  and keeps the session for every other posting on that site.
- **Let it create the account.** Hosts listed in `answers.yaml` →
  `search.create_accounts_on` (Workday is the one worth listing: no e-mail
  confirmation) get an account with your e-mail and a generated password kept
  in `RESUME_TAILOR_SITE_PASSWORD`. Nothing else is ever signed up for.
- **E-mailed codes and links.** Forms that send a one-time code or a
  "continue your application" link first (Oracle, Pella, some iCIMS) are
  handled when `RESUME_TAILOR_IMAP_*` points at your mailbox; without it they
  are logged `needs_login` with the exact URL to finish by hand.

It still does not solve CAPTCHAs or clear identity verification.

## Discover and apply, unattended

The discovery half reads the SimplifyJobs **Summer2027-Internships** list
through its machine-readable `listings.json`, plus the README tables of the
other community lists (jobright-ai SWE and Business Analyst, speedyapply,
vanshb03 — `search.extra_sources` swaps in your own), and keeps what matches
the `search` block of `answers.yaml`: the right term (a title that names
another term outright is dropped whatever the list says), a title that reads
as an engineering or analyst internship, a US location, not PhD-only, not a
blacklisted company, not already attempted. Duplicate postings across lists
collapse to one.

```bash
resume-tailor discover                   # what it would apply to right now, and why the rest was left out
resume-tailor watch --once --dry-run     # one pass: tailor, fill, screenshot — never submit
resume-tailor watch                      # every 30 min, apply to whatever is new; Ctrl-C stops it
```

Postings are attempted in the order the runner can actually do something with
them. `[form]` hosts (Greenhouse, Lever, Ashby, …) fill without help.
`[account]` hosts (Workday, iCIMS, Oracle, …) stop at a login wall and log
`needs_login` — log in there once by hand, and the next pass picks them up.
Everything else is tried and, if the page hides its form behind an Apply
control, that control is clicked once before giving up.

Only postings the list marks active are considered, the fetch is ETag-cached
so a quiet poll costs one small request, and `--max-per-run` (default 25) caps
a single pass so the first run against a 300-posting backlog is a few hours of
paced work, not a flood.

To keep it running after the terminal closes:

```bash
nohup /absolute/path/to/resume-tailor/.venv/bin/resume-tailor watch --out ~/.resume-tailor/output > ~/.resume-tailor/watch.log 2>&1 &
```

Tunables live in `answers.yaml` → `search`: `terms`, `positions`, `require_us`,
`exclude_phd_only`, `company_blacklist`, `title_blacklist`, `location_blacklist`,
`apply_once_at_company`, `approve_before_submit`, `create_accounts_on`,
`min_judge_score`, `max_revisions`, `apply_below_bar`, `jobright_account`,
`extra_sources` — each explained in `profile/answers.example.yaml`.

### Why the browser lives here

`invisible-playwright-mcp` exposes fourteen browser tools and none of them
reaches `set_input_files`. Browsers reject synthetic events on file inputs, so
no combination of click, type, and evaluate can attach a PDF. The upload has to
happen in the process holding the Playwright handle — so this server owns its
own browser rather than talking to that one.

## Tools

| Group | Tools |
|---|---|
| Tailoring | `tailor_resume`, `render_resume_pdf`, `list_styles` |
| Answer bank | `answer_form_question`, `list_known_answers` |
| Browser (interactive, one job) | `browser_open`, `browser_read`, `describe_form`, `fill_field`, `attach_file`, `click`, `list_buttons`, `screenshot` |
| Submit (interactive) | `review_before_submit`, `submit_application` |
| Batch (unattended, many jobs) | `run_batch_apply`, `batch_report` |
| Discovery (SimplifyJobs list) | `discover_internships`, `apply_new_internships` |

`submit_application` requires `confirm="yes-submit"` and refuses while any
required field is still empty.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `RESUME_TAILOR_PROVIDER` | `anthropic` | `anthropic` or `openai`. Which model family tailors. |
| `RESUME_TAILOR_MODEL` | `claude-opus-5` / `gpt-5.2` | Override the provider's default model id. |
| `RESUME_TAILOR_AUDIT_MODEL` | same as model | The per-bullet audit checks — two-thirds of a run's calls — go to this model instead. Point it at one with a roomier rate limit (e.g. `o3-mini`) on a constrained tier. |
| `RESUME_TAILOR_JUDGE_MODEL` | same as model | The two pre-submission match judges. |
| `RESUME_TAILOR_CONCURRENCY` | `4` | Model calls in flight at once. A 429 pauses every call until the server's stated window passes, so a low rate-limit tier works — just slower. Raise this once your tier allows. |
| `ANTHROPIC_API_KEY` | — | Anthropic credential, or leave unset and use `ant auth login`. |
| `OPENAI_API_KEY` | — | OpenAI credential, when the provider is `openai`. |
| `RESUME_TAILOR_PROFILE` | `~/.resume-tailor` | Where `career.yaml` and `answers.yaml` live. |
| `RESUME_TAILOR_OUTPUT` | `./output` | Where PDFs and screenshots go. |
| `RESUME_TAILOR_HEADLESS` | `0` | `1` to hide the browser. Watch it the first few times. |
| `RESUME_TAILOR_SITE_PASSWORD` | — | The one password used for accounts the tool creates on `search.create_accounts_on` hosts. Generate it once; never commit it. |
| `RESUME_TAILOR_IMAP_USER` / `_PASSWORD` / `_HOST` | — | Mailbox for e-mailed one-time codes and continue links. Gmail: an app password, host `imap.gmail.com`. |

## Running it unattended

```bash
resume-tailor start              # detached: survives this terminal and editor closing
resume-tailor status             # counts by outcome, the last ten, the log tail
resume-tailor stop
```

`start` runs `watch` in its own session: every 30 minutes (`--interval`) it
re-reads the lists, takes up to 25 new matching postings (`--max-per-run`),
and works through them — tailor, two judges, form, submit — logging to
`~/.resume-tailor/watch.log`. `--dry-run` does everything but click submit;
`--show` makes the browser visible. Postings that need a site login are logged
as `needs_login` with the exact URL and retried each pass.

```bash
resume-tailor dashboard          # http://127.0.0.1:8765 — every posting, its status, screenshot and PDF
resume-tailor login <company>    # opens that posting's login wall in the tool's browser; sign in, it applies
resume-tailor submit <company>   # submits an application held under approve_before_submit
resume-tailor review <company>   # reopen a needs_review posting with its filled form
```

The dashboard is where `awaiting_approval` postings (companies in
`search.approve_before_submit`) wait with an Approve button, and where
`needs_login` ones have a Log-in button that does the same as the command.
It also shows how many discovered postings are still waiting in the pool.

If the loop is going to run overnight, keep the machine awake
(`caffeinate -d -i -s` on a Mac) and plugged in; a supervisor script that
restarts `start` when `status` no longer says `watch: running` is worth the
five lines.

## How a form gets answered

Every field goes through the same ladder, cheapest source first, and stops at
the first one that can decide it honestly:

1. **The answer bank** (`answers.yaml`) — exact entries: authorization, salary,
   notice period, address, graduation date. Name, email, school, degree and
   major come from `career.yaml` automatically.
2. **Boilerplate rules** — consent checkboxes, "how did you hear about us".
3. **A required essay question** — written from the record and the posting,
   then held to the same numeral and named-entity gates as a resume bullet.
   Optional essays are left blank.
4. **The grounded question-answerer** — for screening questions the bank can't
   enumerate. "Experience with JUnit?" is settled by whether JUnit is in your
   record (silence is a No). Anything else goes to a model that sees your whole
   profile, must cite the keys it used, and may say "unknown" — which it does
   for a street address it doesn't have or an obligation it can't know.

A required field none of those can answer, or a bank answer that isn't one of
the field's options, sends the posting to `needs_review` rather than to a guess.
Radio groups and checkbox lists are one question with several options; the
matching option is chosen by whole-word match, so "No" never lands on "Not
applicable". Search-as-you-type pickers (School, Degree, Country on Greenhouse)
are typed into and committed.

Before any submit, **two independent judges** read the posting and the finished
resume — plus the facts the form supplies that a resume doesn't, like
relocation and authorization — and both must pass.

## What this deliberately does not do

No account creation, no password generation, no CAPTCHA solving. Those stop
one posting and get logged, never worked around. And whatever the volume, the
per-posting anti-fabrication chain — evidence citation, deterministic gates,
the JD-blind audit — runs on every single one; batch mode removes the approval
step, not the checking.

Read the terms of any site you point this at.
