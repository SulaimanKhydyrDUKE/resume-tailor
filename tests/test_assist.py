"""The assist lane, offline: which stuck postings are offered to a hand and in
what order, what a brief carries, how an outcome is recorded (and marks the
company applied to), and how an answer taught by hand is found again.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor import assist
from resume_tailor.profile import Profile
from resume_tailor.queue import RunState

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


now = time.time()
d = Path(tempfile.mkdtemp())
(d / "listings-cache.json").write_text(json.dumps({"listings": [
    {"id": "r1", "company_name": "Acme", "title": "SWE Intern", "url": "https://job-boards.greenhouse.io/acme/jobs/1", "date_posted": int(now - 3 * 86400), "active": True},
    {"id": "r2", "company_name": "Beta", "title": "Data Intern", "url": "https://beta.wd5.myworkdayjobs.com/x/job/Data-Intern_R1", "date_posted": int(now - 10 * 86400), "active": True},
    {"id": "r3", "company_name": "Gone Co", "title": "Intern", "url": "https://jobs.lever.co/gone/1", "date_posted": int(now - 2 * 86400), "active": False},
    {"id": "r4", "company_name": "Old Co", "title": "Intern", "url": "https://jobs.lever.co/old/1", "date_posted": int(now - 90 * 86400), "active": True},
    {"id": "r5", "company_name": "Done Co", "title": "Intern", "url": "https://jobs.lever.co/done/1", "date_posted": int(now - 1 * 86400), "active": True},
    {"id": "r6", "company_name": "Wall Co", "title": "Intern", "url": "https://careers-wall.icims.com/jobs/9/job", "date_posted": int(now - 4 * 86400), "active": True},
    {"id": "r7", "company_name": "Cap Co", "title": "Intern", "url": "https://www.tesla.com/careers/search/job/7", "date_posted": int(now - 5 * 86400), "active": True},
]}))
state = RunState(path=d / "s.json")
state.record("r1", {"status": "needs_review", "company": "Acme", "role": "SWE Intern", "fit": "hiring manager 90/100 pass | screener 90/100 pass",
                    "detail": "could not answer: How did you hear about us?* — rests on nothing in the profile, GPA* — empty, not the intended '3.42'; still empty after filling: Start date*",
                    "pdf": "/tmp/acme.pdf", "screenshot": "/tmp/acme.png", "when": "2026-10-17T10:00:00-04:00",
                    "answers": [{"question": "First Name*", "answer": "A", "source": "answer bank: personal.first_name"}, {"question": "GPA*", "answer": "", "source": None}]})
state.record("r2", {"status": "awaiting_approval", "company": "Beta", "role": "Data Intern", "fit": "x", "detail": "filled and ready", "when": "2026-10-16T10:00:00-04:00"})
state.record("r3", {"status": "needs_review", "company": "Gone Co", "detail": "no application form was found on this page", "when": "2026-10-16T10:00:00-04:00"})
state.record("r4", {"status": "needs_review", "company": "Old Co", "detail": "no application form was found on this page", "when": "2026-10-16T10:00:00-04:00"})
state.record("r5", {"status": "needs_review", "company": "Done Co", "detail": "could not answer: x", "when": "2026-10-16T10:00:00-04:00"})
state.mark_applied("Done Co")
state.record("r6", {"status": "needs_login", "company": "Wall Co", "detail": "hand lane: careers-wall.icims.com guards its form with a captcha", "when": "2026-10-16T09:00:00-04:00"})
state.record("r7", {"status": "blocked", "company": "Cap Co", "detail": "a bot/verification check was on the page — not attempted", "when": "2026-10-16T08:00:00-04:00"})
state.record("r8", {"status": "fit_rejected", "company": "Low Co", "detail": "screener 60/100", "when": "2026-10-16T08:00:00-04:00"})
state.record("r9", {"status": "applied", "company": "Sent Co", "detail": "submitted", "when": "2026-10-16T08:00:00-04:00"})
state.save()

prof = Profile(career={}, answers={"hand_answers": [
    {"question": "How did you hear about us?", "answer": "A GitHub list of internships"},
    {"question": "Are you a previous Skyward employee?", "answer": "No"},
    {"question": "What is your expected hourly rate?", "answer": "35"}]}, root=d)

# The loop's handoffs: a Workday posting never opened (r10), one prepared earlier
# with a current PDF (r11), one whose PDF predates the résumé the user sends now (r12).
from resume_tailor.batch import RESUME_VERSION
(d / "cur.pdf").write_bytes(b"%PDF-1.4 current")
(d / "old.pdf").write_bytes(b"%PDF-1.4 old")
for rid, comp in (("r10", "Beta"), ("r11", "Prep Co"), ("r12", "Stale Co")):
    (d / "listings-cache.json").write_text(json.dumps({"listings": json.loads((d / "listings-cache.json").read_text())["listings"] + [
        {"id": rid, "company_name": comp, "title": "SWE Intern", "url": f"https://{comp.lower().replace(' ', '')}.wd5.myworkdayjobs.com/x/job/SWE_{rid}",
         "date_posted": int(now - 2 * 86400), "active": True, "locations": ["Durham, NC"], "terms": ["Summer 2027"]}]}))
state.record("r10", {"status": "for_agent", "company": "Beta", "role": "SWE Intern", "detail": "for the agent driver: workday at beta.wd5.myworkdayjobs.com is not one of the loop's boards",
                     "url": "https://beta.wd5.myworkdayjobs.com/x/job/SWE_r10", "when": "2026-10-18T10:00:00-04:00"})
state.record("r11", {"status": "for_agent", "company": "Prep Co", "role": "SWE Intern", "detail": "for the agent driver: workday", "pdf": str(d / "cur.pdf"),
                     "fit": "hiring manager 90/100 pass | screener 90/100 pass", "resume_version": RESUME_VERSION,
                     "url": "https://prepco.wd5.myworkdayjobs.com/x/job/SWE_r11", "when": "2026-10-18T10:00:00-04:00"})
state.record("r12", {"status": "for_agent", "company": "Stale Co", "role": "SWE Intern", "detail": "for the agent driver: workday", "pdf": str(d / "old.pdf"),
                     "fit": "hiring manager 90/100 pass | screener 90/100 pass", "resume_version": 1,
                     "url": "https://staleco.wd5.myworkdayjobs.com/x/job/SWE_r12", "when": "2026-10-18T10:00:00-04:00"})
# r2 (awaiting_approval) is at Beta too; the one-company rule is about applied_companies, so both stay.

rows = assist.queue(state, d, prof, limit=10)
ids = [r["id"] for r in rows]
check("queue: the newest posting first whatever held it (the user, 2026-10-29): the 2-day handoffs, then 3, 4, 5 days, the 10-day filled form last",
      ids == ["r10", "r11", "r12", "r1", "r6", "r7", "r2"], str(ids))
tiered = Profile(career={}, answers={"search": {"company_tiers": {"first": ["Cap Co"], "never": ["Wall Co"]}}}, root=d)
check("queue: a tier-1 firm comes before every newer posting, and a never-tier firm is not listed",
      [r["id"] for r in assist.queue(state, d, tiered, limit=0)] == ["r7", "r10", "r11", "r12", "r1", "r2"]
      and assist.next_batch(state, d, tiered, size=2)["portal"] == "www.tesla.com")
state.record("r13", {"status": "needs_review", "company": "Linked Co", "url": "https://www.linkedin.com/jobs/view/123", "detail": "x", "when": "2026-10-18T10:00:00-04:00"})
state.record("r14", {"status": "needs_review", "company": "No URL Co", "detail": "x", "when": "2026-10-18T10:00:00-04:00"})
check("queue: a LinkedIn posting and a record with nothing to open are never the driver's",
      not {"r13", "r14"} & {r["id"] for r in assist.queue(state, d, prof, limit=0)}
      and assist._host_blacklisted(prof, "https://www.linkedin.com/jobs/view/1") and not assist._host_blacklisted(prof, "https://jobs.lever.co/x")
      and not assist._host_blacklisted(Profile(career={}, answers={"search": {"host_blacklist": []}}, root=d), "https://www.linkedin.com/jobs/view/1"))
check("brief row: a company on approve_before_submit is flagged for the driver",
      assist.queue(state, d, Profile(career={}, answers={"search": {"approve_before_submit": ["Acme"]}}, root=d), limit=0, statuses=("needs_review",))[0]["approval_required"] is True
      and next(r for r in rows if r["id"] == "r1")["approval_required"] is False)
check("queue: for_agent is in the default statuses and ranks above needs_review", assist.ASSIST_STATUSES[1] == "for_agent" and assist._RANK["for_agent"] < assist._RANK["needs_review"])
byid = {r["id"]: r for r in rows}
check("brief row: prepared / stale — no PDF yet; a current PDF with a verdict; a PDF from an earlier composer generation",
      (byid["r10"]["prepared"], byid["r10"]["pdf_stale"]) == (False, False)
      and (byid["r11"]["prepared"], byid["r11"]["pdf_stale"]) == (True, False)
      and (byid["r12"]["prepared"], byid["r12"]["pdf_stale"]) == (False, True), str([(byid[k]["prepared"], byid[k]["pdf_stale"]) for k in ("r10", "r11", "r12")]))
check("brief row: a Workday posting with no sign-in on file says a portal account may be needed", "portal account" in byid["r10"]["needs_you"], byid["r10"]["needs_you"])

nb = assist.next_batch(state, d, prof, size=2)
check("next batch: one portal at a time — every Workday tenant is one portal, the batch is capped, the rest is counted",
      nb["portal"] == "workday" and [b["id"] for b in nb["postings"]] == ["r10", "r11"] and nb["remaining"] == 5
      and [x["portal"] for x in nb["portals"]] == ["workday", "greenhouse", "icims", "www.tesla.com"], str(nb["portal"]) + str([b["id"] for b in nb["postings"]]) + str(nb["portals"]))
check("next batch: the portal can be picked by kind or by host suffix",
      [b["id"] for b in assist.next_batch(state, d, prof, size=5, portal="greenhouse")["postings"]] == ["r1"]
      and [b["id"] for b in assist.next_batch(state, d, prof, size=5, portal="tesla.com")["postings"]] == ["r7"])
check("next batch: an empty queue says so", assist.next_batch(RunState(path=d / "empty.json"), d, prof)["postings"] == [])

import asyncio
before = int(state.done["r11"].get("attempts") or 0)
res = asyncio.run(assist.prepare(state, d, "r11", prof, "Prep Co — SWE Intern\n\n" + "A long posting about building software for interns who like Python and distributed systems. " * 4))
after = RunState.load(d / "s.json").done["r11"]
check("prepare: a current PDF with a passing verdict is reused — nothing spent, the record updated, no attempt counted",
      res.get("cached") is True and res["pdf"] == str(d / "cur.pdf") and res["judge_gate"] is True
      and after["pdf"] == str(d / "cur.pdf") and after["status"] == "for_agent" and int(after.get("attempts") or 0) == before, str(res)[:300])
check("prepare: a posting with no text is refused before any model call", "error" in asyncio.run(assist.prepare(state, d, "r10", prof, "")))
check("prepare: an unknown id is an error", "error" in asyncio.run(assist.prepare(state, d, "nope", prof, "x" * 200)))
check("queue: a company already applied to, an inactive listing, a posting past 45 days, fit_rejected and applied are all left out",
      not {"r3", "r4", "r5", "r8", "r9"} & set(ids))
check("queue: include_old brings the 90-day posting back", "r4" in [r["id"] for r in assist.queue(state, d, prof, limit=10, max_age_days=0)])
r1 = next(r for r in rows if r["id"] == "r1")
check("brief row: the unresolved fields are parsed out of the detail",
      r1["unresolved"] == ["How did you hear about us?*", "GPA*", "Start date*"], str(r1["unresolved"]))
check("brief row: ATS, host, apply URL, posted age, answers counted",
      r1["ats"] == "greenhouse" and r1["host"] == "job-boards.greenhouse.io" and r1["apply_url"] == r1["url"]
      and r1["posted_days_ago"] == 3 and r1["answers_decided"] == 1 and r1["answers_total"] == 2 and r1["needs_you"] == "")
check("brief row: a captcha block and a hand-lane wall say they need the user",
      "captcha" in next(r for r in rows if r["id"] == "r7")["needs_you"] and "sign-in" in next(r for r in rows if r["id"] == "r6")["needs_you"])
check("queue: statuses can be narrowed", [r["id"] for r in assist.queue(state, d, prof, statuses=("blocked",))] == ["r7"])
full = assist.brief(state, d, "r1", prof)
check("brief: carries every decided answer with its source, and the listing",
      full and full["answers"][0]["answer"] == "A" and "answer bank" in full["answers"][0]["source"] and full["listing"]["company_name"] == "Acme")
check("brief: an unknown id is None", assist.brief(state, d, "nope", prof) is None)

rec = assist.record(state, "r1", "applied", "finished in the user's Chrome; submitted")
again = RunState.load(d / "s.json")
check("record: applied is written with the assist prefix and marks the company applied to",
      again.done["r1"]["status"] == "applied" and again.done["r1"]["detail"].startswith("assist: finished") and again.has_applied("Acme")
      and again.done["r1"]["worker"] == "assist")
check("record: the company is then out of the queue", "r1" not in [r["id"] for r in assist.queue(again, d, prof)])
try:
    assist.record(state, "r2", "fit_rejected")
    check("record: an outcome a hand cannot settle is refused", False)
except ValueError:
    check("record: an outcome a hand cannot settle is refused", True)
rec2 = assist.record(state, "r2", "skipped")
check("record: a default detail names the status", rec2["detail"] == "assist: left alone on purpose")
rec3 = assist.record(state, "r7", "blocked", "an hCaptcha stood on the form")
check("record: blocked is the driver's to record, stamped as its own", rec3["status"] == "blocked" and rec3["detail"] == "assist: an hCaptcha stood on the form" and rec3["worker"] == "assist")
from resume_tailor.discover import _worn_out
check("record: an outcome the driver recorded is off limits to the loop; a hand re-queue brings it back",
      _worn_out(rec3) and not _worn_out({**rec3, "detail": "re-queued by hand"}))

check("hand answers: the same question normalised ('*', case, punctuation) hits exactly",
      assist.hand_answer(prof.answers, "How did you hear about us? *") == ("A GitHub list of internships", "hand_answers[0]")
      and prof.lookup("HOW DID YOU HEAR ABOUT US")[0] == "A GitHub list of internships")
check("hand answers: a longer question carrying every significant word of a stored one matches; a two-word overlap does not",
      assist.hand_answer(prof.answers, "Are you a previous Skyward employee? If yes, when?")[0] == "No"
      and assist.hand_answer(prof.answers, "Are you a veteran?")[0] is None
      and assist.hand_answer(prof.answers, "Expected hourly rate")[0] is None)
check("hand answers: nothing taught, nothing found", assist.hand_answer({}, "anything") == (None, None))

# learn: appends to answers.yaml with comments kept, updates in place
(d / "answers.yaml").write_text("# my bank\npersonal:\n  email: a@b.c   # the address forms get\n")
(d / "career.yaml").write_text("personal_information:\n  name: A\n  surname: B\n")
assist.learn(d, "Do you have any recruiting timelines we should be aware of?", "No")
assist.learn(d, "Do you have any recruiting timelines we should be aware of? *", "No, none")
text = (d / "answers.yaml").read_text()
import yaml
data = yaml.safe_load(text)
check("learn: written under hand_answers, the owner's comment kept, the same question updated not repeated",
      "# the address forms get" in text and len(data["hand_answers"]) == 1 and data["hand_answers"][0]["answer"] == "No, none"
      and data["hand_answers"][0]["question"].startswith("Do you have any recruiting"))
check("learn: Profile.lookup finds what was taught", Profile.load(d).lookup("Do you have any recruiting timelines we should be aware of?")[0] == "No, none")
check("learn: a backup of the bank was taken", any(p.name.startswith("answers.yaml.bak-") for p in d.iterdir()))

# --- tiers and the deterministic reads in the driver's queue ----------------
never = Profile(career={}, answers={"search": {"company_tiers": {"never": ["Acme"], "first": ["Beta"]}}}, root=d)
rows_t = assist.queue(state, d, never, limit=0)
check("tiers: a never-tier company is not the driver's; first-tier firms carry tier 0, others 2",
      "r1" not in [r["id"] for r in rows_t] and all(r["tier"] == (0 if r["company"] == "Beta" else 2) for r in rows_t) and len(rows_t) >= 3,
      str([(r["id"], r["company"], r.get("tier")) for r in rows_t]))
pr = Profile(career={}, answers={"work_authorization": {"citizenship_status": "U.S. lawful permanent resident", "requires_us_sponsorship": "No"},
                                 "education": {"graduation_date": "May 2028", "degree_level": "Bachelor's"}}, root=d)
res_bar = asyncio.run(assist.prepare(state, d, "r10", pr, "Beta — SWE Intern\n\n" + "You will build services. " * 8 + "Applicants must be a U.S. citizen."))
after_bar = RunState.load(d / "s.json").done["r10"]
check("prepare: an eligibility bar is recorded as the driver's fit_rejected before any model call",
      res_bar.get("status") == "fit_rejected" and after_bar["status"] == "fit_rejected" and after_bar["worker"] == "assist" and after_bar["detail"].startswith("assist: eligibility"), str(res_bar)[:200])
res_small = asyncio.run(assist.prepare(state, d, "r12", pr, "Stale Co — SWE Intern\n\n" + "You will build services. " * 8 + "We are a pre-seed startup with a team of three."))
check("prepare: a tier-4 signal is recorded as skipped", res_small.get("status") == "skipped" and RunState.load(d / "s.json").done["r12"]["status"] == "skipped", str(res_small)[:200])

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
