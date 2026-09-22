# Definition of done

A change is complete only when the applicable items below have evidence. “Should work” is not evidence.

## Base gates

- Every acceptance criterion is satisfied or explicitly reported as incomplete.
- The declared setup and check commands in `.ai/project.yaml` pass.
- Relevant regression coverage is added when a test layer exists.
- The production build succeeds.
- No unrelated behavior or user-owned work is changed.
- No placeholders, fake data, skipped checks, unresolved TODOs, or silent scope reductions remain.
- Documentation and configuration examples match changed behavior.
- The handoff identifies assumptions, residual risks, and unverified behavior.

## User interface changes

- Verify loading, empty, error, success, and recovery states that the feature can enter.
- Verify narrow mobile and desktop layouts.
- Verify keyboard operation, visible focus, meaningful labels, semantic structure, and reasonable contrast.
- Supply before/after or final screenshots at representative viewport sizes.
- Avoid layout shift, unnecessary client JavaScript, and broken reduced-motion behavior.

## Content and links

- Public facts come from owner-approved sources; no claim is inferred from appearance or generated text.
- New external links are authoritative, use safe external-link attributes, and are checked.
- Metadata and accessible names reflect the actual content.

## External services and security

- Secrets never reach client bundles, logs, commits, screenshots, or error messages.
- Input and external responses are validated at trust boundaries.
- Dependency failure, timeout, missing configuration, and rate limiting degrade intentionally.
- Authentication, privacy, production data, and paid-service changes require explicit approval.

## Pull-request evidence

The PR or linked issue must contain an acceptance-criteria matrix, exact command outcomes, screenshots for visible work, changed files, assumptions, risks, and anything not verified.
