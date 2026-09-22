# AI-assisted delivery workflow

This repository uses GitHub Issues as the approval queue and Claude Code GitHub Actions as isolated workers. Nothing merges or deploys automatically.

## One-time repository setup

1. Install the official Claude GitHub App for the repository.
2. Generate a Claude subscription automation token locally with `claude setup-token`.
3. Store it as the repository Actions secret `CLAUDE_CODE_OAUTH_TOKEN`.
4. Create the lifecycle labels listed below.
5. Protect `main`: require a pull request and the `verify` CI job, and block force pushes and deletion.

Do not add `ANTHROPIC_API_KEY`; that selects separately billed API usage instead of the Claude subscription credential.

## Labels

| Label | Meaning |
|---|---|
| `ai:needs-spec` | A read-only agent should expand the rough issue into a complete specification. |
| `ai:needs-approval` | The draft specification needs an owner decision or approval. |
| `ai:ready` | The owner authorizes unattended implementation. |
| `ai:running` | An implementation workflow is active. |
| `ai:pr-open` | A draft pull request was prepared for human review. |
| `ai:blocked` | Automation stopped; inspect its precise blocker and run link. |
| `ai:done` | The approved change was merged and verified. |

Only the repository owner should add `ai:ready`.

## Normal cycle

1. Open a feature or bug issue with a rough outcome.
2. The `ai:needs-spec` workflow posts a structured specification without implementing it.
3. Resolve consequential questions and edit or comment with corrections.
4. Remove `ai:needs-spec` and add `ai:ready` when the contract is correct.
5. The implementation workflow works on one isolated `claude/` branch, runs checks, and opens a draft PR.
6. CI and a fresh read-only Claude session review the PR.
7. Review the product behavior and evidence yourself, then merge manually if it is acceptable.

If no issue is owner-approved, no implementation should run. Idleness is safer than manufacturing work.

## Handoff and reports

The issue tracking comment, draft PR, CI jobs, independent review, and GitHub Actions job summary together form the evidence packet. The private `ai-control` repository can aggregate these facts into one morning digest across enrolled repositories. Cloud workers always see committed GitHub state, never unpushed local edits.
