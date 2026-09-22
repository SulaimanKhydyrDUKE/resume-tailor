---
name: apply-to-job
description: Apply to one job posting interactively through the resume-tailor MCP server, with the user reviewing before anything is submitted.
---

# Apply to one job

Use only the resume-tailor MCP tools. Never fabricate a résumé line, a form answer, or a confirmation.

1. **Read the posting.** Ask for the posting text or URL. If it is a URL, `browser_open` it and `browser_read` the page; otherwise use the pasted text.
2. **Tailor.** Call `tailor_resume` with the posting. Show the user the report: positioning, hard-requirement coverage, gaps, and every unsupported claim. If the unsupported-claims list is not empty, stop and ask how to proceed; do not edit the PDF by hand.
3. **Open the form.** `browser_open` the application URL, then `describe_form`. If the page shows a bot check or identity verification, stop and tell the user; this tool does not solve those.
4. **Answer from the bank.** For each field, call `answer_form_question`. A field the bank cannot answer is asked of the user in one question, and their answer is filled with `fill_field`. Never guess at work authorization, salary, or dates.
5. **Attach.** `attach_file` with the tailored PDF from step 2.
6. **Review.** Call `review_before_submit` and `screenshot`, and show both to the user. Read the unresolved-field list aloud.
7. **Submit only on explicit approval.** When the user says to submit, call `submit_application` with `confirm="yes-submit"`. If any required field is still empty the tool refuses; report that instead of working around it.
8. **Record.** Tell the user the outcome and where the PDF and screenshot were written.

Do not call `run_batch_apply` or `apply_new_internships` from this skill; those are the unattended modes and have their own contract.
