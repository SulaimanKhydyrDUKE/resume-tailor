---
name: write-spec
description: Turn a rough GitHub issue into a production-oriented, reviewable specification without implementing it.
---

# Write a specification

1. Read `CLAUDE.md`, `.ai/project.yaml`, the product brief, Definition of Done, and the complete issue discussion.
2. Inspect relevant code read-only so requirements match current behavior.
3. Separate product decisions from reversible implementation choices.
4. Produce the structure in `docs/specs/TEMPLATE.md`, with observable acceptance criteria and relevant failure states.
5. Explicitly prohibit demo substitutes where real integrations, persistence, authentication, or data are required.
6. List only the unresolved questions that materially change product behavior, security, cost, or architecture.
7. Post the finished specification and finish with either `READY FOR OWNER APPROVAL` or `BLOCKED ON OWNER DECISIONS`.

Do not edit application code, claim owner approval, add `ai:ready`, or implement the request.
