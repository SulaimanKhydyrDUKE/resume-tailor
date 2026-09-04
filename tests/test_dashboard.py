"""The applications page's data: what the watch wrote, read back as the page
shows it — and the record of answers each attempt leaves behind. No server,
no browser."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor.batch import _snapshot
from resume_tailor.dashboard import build_index

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


# --- the answers record ---------------------------------------------------------
fields = [
    {"id": "rt-1", "tag": "input", "type": "text", "label": "Email", "section": "", "value": "a@b.c", "required": True},
    {"id": "rt-2", "tag": "input", "type": "radio", "label": "Office preference", "section": "Location", "group": "g1",
     "option_label": "Durham, NC", "checked": True, "required": True},
    {"id": "rt-3", "tag": "input", "type": "radio", "label": "Office preference", "section": "Location", "group": "g1",
     "option_label": "Columbus, OH", "checked": False, "required": True},
    {"id": "rt-4", "tag": "input", "type": "file", "label": "Resume", "section": "", "files": ["whatnot.pdf"], "required": True},
    {"id": "rt-5", "tag": "input", "type": "checkbox", "label": "I agree to the terms", "section": "", "group": "I agree", "checked": True},
    {"id": "rt-6", "tag": "input", "type": "text", "label": "What is your GPA?", "section": "Education", "value": "", "required": True},
    {"id": "rt-7", "tag": "input", "type": "text", "label": "", "section": "", "value": ""},
]
sources = {("", "Email"): "answer bank: personal.email", ("Location", "Office preference"): "form plan: own city"}
snap = _snapshot(fields, ["What is your GPA? — not in the profile"], sources)
by_q = {a["question"]: a for a in snap}
check("snapshot: a text field records its value and source",
      by_q["Email"]["answer"] == "a@b.c" and by_q["Email"]["source"] == "answer bank: personal.email")
check("snapshot: a radio group is one row with the chosen option and all options",
      by_q["Office preference"]["answer"] == "Durham, NC" and by_q["Office preference"]["options"] == ["Durham, NC", "Columbus, OH"]
      and by_q["Office preference"]["source"].startswith("form plan"))
check("snapshot: a file field records the file name", by_q["Resume"]["answer"] == "whatnot.pdf")
check("snapshot: a ticked consent box reads Yes", by_q["I agree to the terms"]["answer"] == "Yes")
check("snapshot: an unanswered required question carries its reason",
      by_q["What is your GPA?"]["answer"] == "" and by_q["What is your GPA?"]["reason"] == "not in the profile")
check("snapshot: a nameless empty control is left out", "" not in by_q and len(snap) == 5)

# --- the index the page reads ------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    out = Path(d)
    (out / "screenshots").mkdir()
    (out / "acme.pdf").write_bytes(b"%PDF-1.4 fake")
    (out / "screenshots" / "job-1-post-submit.png").write_bytes(b"png")
    (out / "batch-state.json").write_text(json.dumps({
        "done": {
            "job-1": {"status": "applied", "company": "Acme", "role": "SWE Intern", "fit": "hiring manager 90/100 pass | screener 95/100 pass",
                      "detail": "submitted — confirmation shown", "pdf": str(out / "acme.pdf"), "when": "2026-09-03T01:00:00-04:00",
                      "answers": [{"question": "Email", "answer": "a@b.c"}]},
            "job-2": {"status": "needs_review", "company": "Beta", "role": "", "detail": "could not answer: GPA", "pdf": "/elsewhere/x.pdf",
                      "when": "2026-09-03T00:30:00-04:00"},
        },
        "applied_companies": ["acme"],
    }))
    (out / "listings-cache.json").write_text(json.dumps([
        {"id": "job-1", "company_name": "Acme", "title": "SWE Intern", "url": "https://jobs.example/1", "locations": ["NYC"], "date_posted": 1756000000},
        {"id": "job-2", "company_name": "Beta Corp", "title": "Backend Intern", "url": "https://jobs.example/2", "locations": []},
    ]))
    idx = build_index(out, profile_dir=out)  # no watch files there: the loop shows as stopped
    apps = {a["id"]: a for a in idx["applications"]}
    check("index: counts by status", idx["counts"] == {"applied": 1, "needs_review": 1})
    check("index: newest attempt first", [a["id"] for a in idx["applications"]] == ["job-1", "job-2"])
    check("index: the PDF under output/ becomes a /files URL", apps["job-1"]["pdf"] == "/files/acme.pdf")
    check("index: a PDF outside output/ is not exposed", apps["job-2"]["pdf"] is None)
    check("index: screenshots are found by entry id and kind", apps["job-1"]["screenshots"] == {"post-submit": "/files/screenshots/job-1-post-submit.png"})
    check("index: the listing fills in role and posting link when the record lacks them",
          apps["job-2"]["role"] == "Backend Intern" and apps["job-2"]["url"] == "https://jobs.example/2")
    check("index: answers travel with the record", apps["job-1"]["answers"] == [{"question": "Email", "answer": "a@b.c"}])
    check("index: watch status reports stopped when there is no pid file", idx["watch"]["running"] is False)

    import os
    from resume_tailor import dashboard as _dash
    _dash.REVIEWS_DIR = out / "reviews"
    _dash.REVIEWS_DIR.mkdir()
    (_dash.REVIEWS_DIR / "job-1.json").write_text(json.dumps({"pid": os.getpid(), "since": "2026-09-03T04:00:00"}))
    (_dash.REVIEWS_DIR / "job-2.json").write_text(json.dumps({"pid": 999999, "since": "2026-09-03T04:00:00"}))
    live = build_index(out, profile_dir=out)
    by = {a["id"]: a for a in live["applications"]}
    check("reviews: a marker whose process is alive shows the window as open",
          by["job-1"]["reviewing"] and by["job-1"]["reviewing"]["since"] == "2026-09-03T04:00:00")
    check("reviews: a marker whose process is gone does not", by["job-2"]["reviewing"] is None)
    from resume_tailor.dashboard import mark
    rec = mark(out, "job-2", "applied")
    after = build_index(out, profile_dir=out)
    check("mark: a posting finished by hand is recorded as applied, with the company remembered",
          rec["status"] == "applied" and after["counts"].get("applied") == 2 and "beta" in after["applied_companies"])
    try:
        mark(out, "job-2", "blocked")
        check("mark: only applied/skipped/needs_review can be set by hand", False)
    except ValueError:
        check("mark: only applied/skipped/needs_review can be set by hand", True)

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
