"""Intake and the pending-question log, offline: a fact the record already
holds is never asked, an answer is written to answers.yaml → facts with the
owner's comments kept, the bank finds it, and an unanswered question is
written down (once) instead of waited on.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml

from resume_tailor import intake, pending

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


d = Path(tempfile.mkdtemp())
(d / "answers.yaml").write_text(
    "# my answers — keep this comment\n"
    "personal:\n  email: jane@example.edu\n  phone: \"5550100\"\n"
    "address:\n  street: 1 Main St\n  city: Springfield\n  state: IL\n  zip: \"62701\"\n"
    "hand_answers:\n  - question: What is your date of birth?\n    answer: January 1, 2005\n",
    encoding="utf-8")
(d / "career.yaml").write_text("education:\n  gpa: \"3.5\"\n", encoding="utf-8")

st = intake.status(d)
vals = {r["key"]: r["value"] for r in st["facts"]}
check("record answers a fact: zip from address.zip", vals["current_address_zip"] == "62701", vals["current_address_zip"])
check("career.yaml answers a fact: gpa", vals["gpa"] == "3.5", vals["gpa"])
check("an earlier hand answer counts: date of birth", vals["date_of_birth"] == "January 1, 2005", vals["date_of_birth"])
check("unknown facts are listed missing", "permanent_address_zip" in st["missing"] and "college_within_university" in st["missing"])
check("known facts are not missing", "current_address_zip" not in st["missing"] and "email" not in st["missing"])

asked = []
answers = {"Permanent home zip code": "62704", "College / school within the university": "College of Arts and Sciences"}


def fake_ask(prompt):
    asked.append(prompt)
    for q, a in answers.items():
        if prompt.startswith(q):
            return a
    return ""


got = intake.run(d, ask=fake_ask)
check("only missing facts are asked", not any(p.startswith("Current zip code") for p in asked) and len(asked) == len(st["missing"]))
check("answers returned", got == {"permanent_address_zip": "62704", "college_within_university": "College of Arts and Sciences"}, got)
text = (d / "answers.yaml").read_text(encoding="utf-8")
data = yaml.safe_load(text)
check("written to answers.yaml → facts", data["facts"]["permanent_address_zip"] == "62704", data.get("facts"))
check("zip kept a string", isinstance(data["facts"]["permanent_address_zip"], str))
check("owner's comment kept", "# my answers — keep this comment" in text)
check("other keys untouched", data["address"]["zip"] == "62701" and data["personal"]["email"] == "jane@example.edu")
check("a backup was written", any(p.name.startswith("answers.yaml.bak-") for p in d.iterdir()))
st2 = intake.status(d)
check("answered facts are no longer missing", "permanent_address_zip" not in st2["missing"])
check("facts_map carries them", intake.facts_map(d).get("college_within_university") == "College of Arts and Sciences")

from resume_tailor.profile import Profile  # noqa: E402

try:
    prof = Profile.load(d)
    hit = prof.lookup("Which college within the university are you enrolled in?")
    check("the bank finds an intake fact by its words", hit[0] == "College of Arts and Sciences", hit)
except Exception as e:  # a profile without career basics may refuse to load; the facts are still read by intake_facts
    check("profile loads for the bank check", False, repr(e))

d2 = Path(tempfile.mkdtemp())
(d2 / "answers.yaml").write_text(
    "address:\n  zip: \"62701\"\n  alternates:\n    - street: 1 Elm St\n      zip: \"62704\"\n"
    "self_identification:\n  gender: Decline to self-identify\n"
    "hand_answers:\n  - question: Are you related to anyone who currently works at the company?\n    answer: \"No\"\n"
    "  - question: Where did you attend high school/secondary school?\n    answer: Springfield High\n"
    "  - question: What are your internship availability dates?\n    answer: May 2027 - Aug 2027\n"
    "  - question: Will you need housing?\n    answer: Yes\n"
    "  - question: What is your date of birth? (month and day of birth)\n    answer: January 1, 2005\n",
    encoding="utf-8")
(d2 / "career.yaml").write_text(
    "personal_information:\n  name: Jane\n  surname: Doe\n  email: jane@example.edu\n"
    "education_details:\n  - institution: State University\n    field_of_study: Computer Science\n"
    "experience_details:\n  - position: Software Engineer Intern\n    company: Acme Corp\n  - position: Research Assistant\n    company: Lab\n"
    "  - position: Software Engineering Intern\n    company: Campus IT\n", encoding="utf-8")
v2 = intake.facts_map(d2)
check("career.yaml personal_information layout is read", v2.get("legal_first_name") == "Jane" and v2.get("email") == "jane@example.edu", v2)
check("list paths: education_details.0.institution", v2.get("university") == "State University", v2.get("university"))
check("permanent address from address.alternates[0]", v2.get("permanent_address_zip") == "62704" and v2.get("permanent_address_street") == "1 Elm St")
check("signature name derived from first + last", v2.get("signature_name") == "Jane Doe", v2.get("signature_name"))
check("intern companies derived from experience, interns only", v2.get("previous_internship_companies") == "Acme Corp, Campus IT", v2.get("previous_internship_companies"))
check("hand answer found by its question pattern", v2.get("related_to_employees") == "No" and v2.get("high_school") == "Springfield High")
check("date of birth is not taken from an earlier dates answer", v2.get("date_of_birth") == "January 1, 2005", v2.get("date_of_birth"))
check("a YAML boolean reads back as Yes/No", v2.get("need_housing") == "Yes" and v2.get("related_to_employees") == "No", (v2.get("need_housing"), v2.get("related_to_employees")))
check("self_identification answers demographics", v2.get("gender") == "Decline to self-identify")

out = d / "output"
r1 = pending.add(out, "Why do you want to work here?", company="Acme", url="https://x/1", entry_id="e1", tab="tab 3")
r2 = pending.add(out, "Why do you want to work here?", company="Acme", url="https://x/1", entry_id="e1")
r3 = pending.add(out, "Record a 2-minute video", company="Beta", url="https://x/2")
check("pending: a question is noted with a number", r1["n"] == 1 and r1["status"] == "open")
check("pending: the same question for the same posting is not repeated", r2["n"] == 1 and len(pending.listing(out)) == 2)
check("pending: numbers increase", r3["n"] == 2)
closed = pending.close(out, 1, "Because of the product")
check("pending: answering closes it", closed and closed["status"] == "answered" and len(pending.listing(out)) == 1)
check("pending: closed ones still listed with --all", len(pending.listing(out, include_closed=True)) == 2)
check("pending: file lives in the output dir", (out / pending.FILE).is_file())

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
