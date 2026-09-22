---
name: review-change
description: Adversarially review an AI-authored pull request against its issue specification and project Definition of Done.
---

# Review a change

Use a fresh, read-only perspective. Do not trust the implementation summary.

1. Read the linked issue, acceptance criteria, repository rules, diff, and CI evidence.
2. Check every acceptance criterion against implementation evidence.
3. Look specifically for demo substitutes, missing states, regressions, weak tests, unsafe data handling, secret exposure, accessibility problems, invented content, and unrelated changes.
4. Distinguish pre-existing failures from regressions introduced by the PR.
5. Report each material finding with severity, file/location, evidence or reproduction, and a concrete correction.
6. If no material finding exists, explain the evidence supporting that conclusion and name any residual manual checks.

Do not approve, merge, deploy, or modify the PR.
