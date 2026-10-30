"""The answers audit: what it flags and what it leaves alone. Offline; the
records below are shaped like batch-state.json's."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor import audit

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


class _P:
    def flat_answers(self):
        return {"work_authorization.citizenship_status": "U.S. lawful permanent resident (green card holder)",
                "education.gpa": "3.42"}


def rec(company, when, *qa, status="applied"):
    return {"status": status, "company": company, "when": when,
            "answers": [{"question": q, "answer": a, "source": "form plan"} for q, a in qa]}


TODAY = date(2026, 10, 5)
done = {
    "a1": rec("Allegion", "2026-09-16T10:00:00", ("Are you legally authorized to work in the United States?*", "No")),
    "a2": rec("Philips", "2026-09-16T10:00:00", ("Will you now, or in the future, require sponsorship from Philips?", "No")),
    "a3": rec("Humana", "2026-09-07T10:00:00", ("Will you now or in the future require immigration sponsorship by our company?", "Yes")),
    "a4": rec("Merck", "2026-09-08T10:00:00", ("Will you require our Company to provide immigration sponsorship?", "No – I hold a temporary visa status that provides work authorization")),
    "a5": rec("Fed", "2026-10-02T10:00:00", ("Please list ALL countries of citizenship:*", "United States")),
    "a6": rec("Cox", "2026-09-08T10:00:00", ("Are you able to work in the internship's city without relocation assistance?*", "No")),
    "a7": rec("Relay", "2026-09-16T10:00:00", ("When is your desired start date?*", "08/2024")),
    "a8": rec("Sierra", "2026-09-03T10:00:00", ("What is your GPA?", "3.5")),
    "a9": rec("Graco", "2026-09-17T10:00:00", ("What is the highest educational degree you have earned?*", "Bachelors")),
    "b1": rec("Fidelity", "2026-09-12T10:00:00", ("Please select your highest level of education obtained or, if a current student, the degree you are pursuing", "Bachelor's")),
    "b2": rec("Cybernetic", "2026-09-09T10:00:00", ("How many years of industry experience do you have?", "2.5")),
    "b3": rec("Ameren", "2026-09-09T10:00:00", ("How many years of relevant experience do you have?", "0 to 3 years")),
    "b4": rec("Aerotech", "2026-09-05T10:00:00", ("What is your overall GPA?*", "-- No answer --")),
    "b5": rec("CACI", "2026-09-07T10:00:00", ("How Did You Hear About Us?*", "LinkedIn")),
    "b6": rec("AArete", "2026-09-08T10:00:00", ("Where did you hear about us?*", "Job Posting (LinkedIn, Indeed, Handshake, etc.)")),
    "b7": rec("Scale", "2026-09-04T10:00:00", ("I confirm my availability for a Summer 2026 (May/June starts) internship*", "Yes")),
    "b8": rec("Semgrep", "2026-09-08T10:00:00", ("Are you available to start on Monday, June 14th, 2027?", "Yes")),
    "b9": rec("Eulerity", "2026-09-03T10:00:00", ("Are you legally authorized to work in the United States?*", "Yes"),
              ("What is your GPA?", "3.42"), ("Are you willing to relocate?", "Yes")),
    "c1": rec("Held", "2026-09-03T10:00:00", ("Are you legally authorized to work in the United States?*", "No"), status="needs_review"),
    "c2": rec("Edu", "2026-09-05T10:00:00", ("Start date year*", "2024"), ("Opt-In to receive text messages", "Opt-In"),
              ("What is your current GPA?", "3.4"), ("If you are not a U.S. citizen, U.S. national, permanent resident or a protected individual under the INA, are you authorized to work?", "No")),
}
rows = audit.audit_answers(done, _P(), TODAY)
by_rec = {r["record"]: r["kind"] for r in rows}
check("authorized No is flagged", by_rec.get("a1") == "authorized answered No")
check("a correct No to 'require sponsorship' is not", "a2" not in by_rec)
check("sponsorship Yes is flagged", by_rec.get("a3") == "sponsorship answered Yes")
check("a visa-holder's option for a permanent resident is flagged", by_rec.get("a4") == "a visa-holder's status")
check("United States as country of citizenship is flagged for a non-citizen", by_rec.get("a5") == "citizenship claimed")
check("relocation No is flagged", by_rec.get("a6") == "relocation answered No")
check("a start date in the past is flagged", by_rec.get("a7") == "start date in the past")
check("a GPA above the bank's is flagged", by_rec.get("a8") == "GPA above the bank's")
check("'degree earned: Bachelors' is flagged", by_rec.get("a9") == "degree completed = a degree")
check("'…or, if a current student, pursuing' is not", "b1" not in by_rec)
check("2.5 years of industry experience is flagged", by_rec.get("b2") == "years of experience above a student's")
check("the lowest band '0 to 3 years' is not", "b3" not in by_rec)
check("a required field left unanswered is flagged", by_rec.get("b4") == "required field left unanswered")
check("'how did you hear: LinkedIn' is flagged", by_rec.get("b5") == "how did you hear: a named source")
check("a job-board category that lists sites in brackets is not", "b6" not in by_rec)
check("confirming a term already over is flagged", by_rec.get("b7") == "confirmed a term already over")
check("a 2027 start confirmation is not", "b8" not in by_rec)
check("sound answers raise nothing", "b9" not in by_rec)
check("a plain start-date field, an opt-in box, a GPA band below the bank's and a conditional authorization question raise nothing", "c2" not in by_rec)
check("only submitted applications are read", "c1" not in by_rec)
check("rows are oldest first", [r["date"] for r in rows] == sorted(r["date"] for r in rows))
s = audit.summary(rows, 19)
check("the summary counts rows, applications and the last date per kind", s.startswith(f"{len(rows)} answers across {len(by_rec)} of 19") and "last 2026-10-02" in s, s)
out = audit.write_csv(rows, Path(__import__("tempfile").mkdtemp()) / "answers-audit.csv")
check("the CSV has one line per row plus the header", len(out.read_text(encoding="utf-8").splitlines()) == len(rows) + 1)
check("nothing flagged without a bank: no GPA or citizenship comparison, the rest still read",
      {r["record"] for r in audit.audit_answers(done, None, TODAY)} >= {"a1", "a3", "a6", "a7", "b4", "b5", "b7"}
      and "a8" not in {r["record"] for r in audit.audit_answers(done, None, TODAY)})

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
