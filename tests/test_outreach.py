"""Outreach: who counts as a recruiter to write to, and how they are addressed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from resume_tailor.outreach import NOREPLY, _person, _writable, _unligate

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))

check("an assessment mailbox is never written to", bool(NOREPLY.search("assessment@email.roblox.com")))
check("an accommodations mailbox is never written to", bool(NOREPLY.search("candidateaccommodations@rivian.com")))
check("a campus recruiting mailbox is", not NOREPLY.search("universityrecruiting@stokespace.com"))
check("a careers mailbox is", not NOREPLY.search("careers@stokespace.com"))
check("'Roblox Assessment' is not a person", not _person("Roblox Assessment", "Roblox"))
check("'GuideWell Talent Acquisition' is not a person", not _person("GuideWell Talent Acquisition", "GuideWell Mutual"))
check("'Netic Hiring Team' is not a person", not _person("Netic Hiring Team", "Netic"))
check("a two-word capitalised name is a person", _person("Akshay Kumar", "Ambrook"))
check("a name carrying the company's name is not", not _person("Ambrook Recruiting", "Ambrook"))
check("a lowercase mailbox label is not", not _person("campusus imc", "IMC"))
for bad in ("myworkday@thehartford.com", "rs.workday@medtronic.com", "hrsupport_na@micron.com", "accommodations@adobe.com",
            "recruitmentoperationsservicing@aexp.com", "seeyourself@thecignagroup.com", "contact@anduril.com"):
    check(f"never written to: {bad}", not _writable(bad))
for good in ("campusus@imc.com", "talent@dvtrading.co", "targetcareers@target.com", "careers@talos.com", "universityrecruiting@x.com", "dominique.burns@bti360.com"):
    check(f"a recruiting address: {good}", _writable(good))
check("ligatures are read as letters in the fact guard", "office" in _unligate("Duke O\ufb03ce of IT").lower())

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
