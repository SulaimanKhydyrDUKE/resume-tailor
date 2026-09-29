"""The applications page's data: what the watch wrote, read back as the page
shows it — and the record of answers each attempt leaves behind. No server,
no browser."""
import json
import os
import time
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
    check("index: the list carries no per-attempt answers (they weigh 80 % of a 12 MB page)", "answers" not in apps["job-1"])
    from resume_tailor.dashboard import application_detail, cached_view, index_view
    check("detail: answers travel with the record", application_detail(out, "job-1")["answers"] == [{"question": "Email", "answer": "a@b.c"}])
    check("detail: an unknown id is None", application_detail(out, "nope") is None)
    first = index_view(out)
    check("cache: the same files give the same built view", index_view(out) is first)
    check("cache: the body is the list, not the details", b'"answers"' not in first["body"] and b'"applications"' in first["body"])
    check("cache: the gzipped body inflates to the body", __import__("gzip").decompress(first["gz"]) == first["body"])
    check("cache: an ETag is quoted", first["etag"].startswith('"') and first["etag"].endswith('"'))
    st = out / "batch-state.json"
    data = json.loads(st.read_text()); data["done"]["job-1"]["detail"] = "changed by the test"; st.write_text(json.dumps(data))
    os.utime(st, (time.time() + 5, time.time() + 5))
    second = index_view(out)
    check("cache: a changed state file rebuilds the view", second is not first and second["etag"] != first["etag"])
    check("cache: the rebuilt view carries the change", next(a for a in second["value"]["applications"] if a["id"] == "job-1")["detail"] == "changed by the test")
    calls = []
    cached_view("probe", [st], lambda: calls.append(1) or {"n": len(calls)})
    cached_view("probe", [st], lambda: calls.append(1) or {"n": len(calls)})
    check("cache: an unchanged input never rebuilds", calls == [1])
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

# --- the calendars: applications by day, dates from the inbox, the .ics ---------
import json as _json
import tempfile as _tempfile
from datetime import datetime as _dt
from resume_tailor.calendar import TZ, _find_dates, _local_date, _plausible, build_calendar, candidate_messages, write_ics

_r = _dt(2026, 9, 10, 9, 0, tzinfo=TZ)
_found = [d.isoformat()[:16] for d in _find_dates("Complete the assessment by September 14 at 11:59 PM. Interview on 10/03/2026. Reply within 7 days. Founded in 2023.", _r)]
check("find_dates: month-day with a time, a slash date, and a 'within N days' window", _found == ["2026-09-14T23:59", "2026-09-17T09:00", "2026-10-03T00:00"], str(_found))
check("find_dates: a bare past year is not a date", not _find_dates("We were founded in 2023 and grew in 2024.", _r))
check("find_dates: a month-day already passed this year rolls to next year", [d.year for d in _find_dates("Applications open January 5.", _r)] == [2027])
check("find_dates: nothing beyond half a year", not _find_dates("The program runs until 2027-06-01.", _r))
check("plausible: a date before the message is not a deadline", not _plausible("2020-10-05", "2026-09-25T10:00:00-04:00"))
check("plausible: a date the next week is", _plausible("2026-10-01", "2026-09-25T10:00:00-04:00"))
check("local_date: an ISO stamp with offset gives the local day", _local_date("2026-09-14T23:59:00-04:00") == "2026-09-14")
check("local_date: a UTC stamp late at night lands on the local day", _local_date("2026-09-15T02:30:00Z") == "2026-09-14")

_cands = candidate_messages({"companies": {
    "acme": {"company": "Acme", "timeline": [{"uid": 1, "stage": "oa", "subject": "Your assessment", "when": "2026-09-10T09:00:00-04:00"},
                                             {"uid": 2, "stage": "applied", "subject": "Thanks for applying", "when": "2026-09-10T09:00:00-04:00"},
                                             {"uid": 3, "stage": "other", "subject": "Reminder: complete your profile by Friday", "when": "2026-09-11T09:00:00-04:00"}]}}})
check("candidates: assessments and deadline-worded subjects, not a plain confirmation", sorted(c["uid"] for c in _cands) == [1, 3], str(_cands))

_tmp = Path(_tempfile.mkdtemp())
(_tmp / "batch-state.json").write_text(_json.dumps({"done": {
    "id-1": {"company": "Acme", "role": "SWE Intern", "status": "applied", "when": "2026-09-11T19:02:44-04:00", "pdf": str(_tmp / "acme.pdf")},
    "id-2": {"company": "Beta", "role": "Data Intern", "status": "needs_review", "when": "2026-09-11T21:10:00-04:00"},
    "id-3": {"company": "Gamma", "role": "Intern", "status": "applied", "when": "2026-09-12T03:30:00Z"},
}}))
(_tmp / "results.json").write_text(_json.dumps({"companies": {
    "acme": {"company": "Acme", "stage": "oa", "timeline": [{"uid": 11, "when": "2026-09-12T10:00:00-04:00", "subject": "Assessment invite", "stage": "oa", "date": "2026-09-14T23:59:00-04:00", "note": "finish by deadline"},
                                                            {"uid": 12, "when": "2026-09-12T10:00:00-04:00", "subject": "old", "stage": "other", "date": "2023-09-18", "note": "credit card offer"}]}}}))
(_tmp / "deadlines.json").write_text(_json.dumps({"events": {"11": {"uid": "11", "key": "acme", "company": "Acme", "subject": "Assessment invite", "received": "2026-09-12T10:00:00-04:00",
                                                                     "stage": "oa", "date": "2026-09-14T22:00", "kind": "assessment", "what": "finish the HackerRank", "source": "model"}}}))
(_tmp / "acme.pdf").write_bytes(b"%PDF-1.4")
_cal = build_calendar(_tmp)
check("calendar: attempts grouped by local day, the UTC one on its local evening", sorted(_cal["applied"]) == ["2026-09-11"] and [a["company"] for a in _cal["applied"]["2026-09-11"]] == ["Acme", "Beta", "Gamma"], str({k: [a["company"] for a in v] for k, v in _cal["applied"].items()}))
check("calendar: every attempt carries its status so the page can filter", [a["status"] for a in _cal["applied"]["2026-09-11"]] == ["applied", "needs_review", "applied"])
check("calendar: the PDF is served as a /files/ link", str(_cal["applied"]["2026-09-11"][0]["pdf"]).startswith("/files/"), str(_cal["applied"]["2026-09-11"][0]["pdf"]))
check("calendar: the inbox pass's event wins over the scan's for the same message, and the stale 2023 mention is dropped",
      list(_cal["deadlines"]) == ["2026-09-14"] and _cal["deadlines"]["2026-09-14"][0]["what"] == "finish the HackerRank" and _cal["counts"]["events"] == 1, str(_cal["deadlines"]))
_ics = write_ics(_tmp, _cal).read_text()
check("ics: one timed deadline and one all-day applied summary", _ics.count("BEGIN:VEVENT") == 2 and "DTSTART;TZID=America/New_York:20260914T220000" in _ics and "SUMMARY:Applied: 2 (Acme\, Gamma)" in _ics, _ics[:600])

from resume_tailor.calendar import _settled_date
check("settled_date: a model date equal to the arrival time gives way to the text's later date",
      _settled_date({"date": "2026-09-28T12:53", "received": "2026-09-28T12:53:00-04:00", "rule_dates": ["2026-10-07T00:00"]}) == "2026-10-07T00:00")
check("settled_date: a real model date stands", _settled_date({"date": "2026-10-01", "received": "2026-09-28T12:53:00-04:00", "rule_dates": ["2026-10-07T00:00"]}) == "2026-10-01")
check("settled_date: arrival time with no better date stays (and is later dropped as a same-day mention)",
      _settled_date({"date": "2026-09-28T12:53", "received": "2026-09-28T12:53:00-04:00", "rule_dates": []}) == "2026-09-28T12:53")

check("settled_date: a date named in the model's summary beats an earlier one from the body",
      _settled_date({"date": "2026-09-28T12:53", "received": "2026-09-28T12:53:00-04:00", "what": "Invitation to Duke campus event on October 7th",
                     "rule_dates": ["2026-10-05T00:00", "2026-10-07T00:00"]}).startswith("2026-10-07"))

# --- the Files tab: résumés on disk, portals with a saved sign-in, no secret -------
from resume_tailor.dashboard import build_files, portal_hosts
_names = ["jobright.ai.json", "acme.wd5.myworkdayjobs.com.json", "doubleclick.net.json", "campus-x.icims.com.json", "1rx.io.json",
          "careers.example.com.json", "google.com.json", "linkedin.com.json"]
check("portal_hosts: ATS hosts, careers hosts and jobright kept; ad-tech, google and linkedin dropped",
      portal_hosts(_names, ["icims.com"]) == ["acme.wd5.myworkdayjobs.com", "campus-x.icims.com", "careers.example.com", "jobright.ai"], str(portal_hosts(_names, ["icims.com"])))
_ftmp = Path(_tempfile.mkdtemp())
(_ftmp / "resumes" / "acme-swe" / "documents").mkdir(parents=True)
(_ftmp / "resumes" / "acme-swe" / "First_Last_resume.pdf").write_bytes(b"%PDF-1.4")
(_ftmp / "resumes" / "acme-swe" / "documents" / "cover-letter.pdf").write_bytes(b"%PDF-1.4")
(_ftmp / "beta.pdf").write_bytes(b"%PDF-1.4")
(_ftmp / "batch-state.json").write_text(_json.dumps({"done": {
    "a": {"company": "Acme", "role": "SWE Intern", "status": "applied", "when": "2026-09-11T10:00:00-04:00", "pdf": str(_ftmp / "resumes" / "acme-swe" / "First_Last_resume.pdf")},
    "a2": {"company": "Acme", "role": "SWE Intern", "status": "needs_review", "when": "2026-09-12T10:00:00-04:00", "pdf": str(_ftmp / "resumes" / "acme-swe" / "First_Last_resume.pdf")},
    "b": {"company": "Beta", "role": "Intern", "status": "applied", "when": "2026-09-13T10:00:00-04:00", "pdf": str(_ftmp / "beta.pdf")},
    "c": {"company": "Gone", "role": "Intern", "status": "applied", "when": "2026-09-14T10:00:00-04:00", "pdf": str(_ftmp / "missing.pdf")}}}))
_ldir = _ftmp / "logins"; _ldir.mkdir()
for n in _names: (_ldir / n).write_text("[]")
_files = build_files(_ftmp, logins_dir=_ldir, answers={"search": {"create_accounts_on": ["icims.com"]}})
check("files: one entry per PDF on disk, newest first, a missing file skipped",
      [r["company"] for r in _files["resumes"]] == ["Beta", "Acme"] and _files["counts"]["resumes"] == 2, str([r["company"] for r in _files["resumes"]]))
check("files: documents beside the résumé are listed with /files/ links",
      _files["resumes"][1]["documents"] == [{"name": "cover-letter.pdf", "url": "/files/resumes/acme-swe/documents/cover-letter.pdf"}], str(_files["resumes"][1]["documents"]))
check("files: portals with a saved sign-in, and the count of every saved site", _files["logins"]["hosts"][:2] == ["acme.wd5.myworkdayjobs.com", "campus-x.icims.com"] and _files["logins"]["saved_sites"] == len(_names))
check("files: no password anywhere in the files response", "password" not in _json.dumps(_files).lower())


width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
