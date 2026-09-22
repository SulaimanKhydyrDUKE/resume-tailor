---
name: implement-spec
description: Implement an owner-approved GitHub issue completely, verify it, and return an evidence-based handoff.
---

# Implement an approved specification

1. Confirm the issue has `ai:ready`; otherwise stop without edits.
2. Read all repository instructions and the complete issue discussion.
3. Identify genuine blockers. Decide local reversible details autonomously.
4. Record baseline state and run applicable baseline checks.
5. Implement the smallest complete solution. Do not use demo substitutes or expand scope silently.
6. Run every required check. Inspect the resulting behavior and diff; fix failures and regressions.
7. For visible changes, exercise representative mobile and desktop journeys and capture evidence when tooling permits.
8. Commit and push only to the issue branch prepared by the GitHub Action.
9. Return the handoff format required by `CLAUDE.md`. If anything required is unverified, say so and do not call the task complete.
