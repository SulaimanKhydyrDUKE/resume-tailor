"""The deterministic eligibility read and the tier-4 signal, offline: a bar
fires only on a requirement the applicant's own facts cannot meet, never on a
softened or alternative wording; the vendor API address for each host.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor import eligibility as E
from resume_tailor.profile import Profile

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


gc = E.Facts(citizen=False, permanent_resident=True, needs_sponsorship=False, grad_year=2028, grad_month=5, degree_level="bachelor")
visa = E.Facts(citizen=False, permanent_resident=False, needs_sponsorship=True, grad_year=2027, grad_month=5, degree_level="master")
none = E.Facts()


def bar(text, facts=gc):
    return E.scan(text, facts)[0] == "bar"


check("citizenship: 'must be a U.S. citizen' bars a green-card holder; silent when citizenship is unknown",
      bar("Applicants must be a U.S. citizen due to contract requirements.") and not bar("Applicants must be a U.S. citizen.", none))
check("citizenship: 'U.S. citizens or permanent residents' is open to a green-card holder; bars a visa holder",
      not bar("Must be a U.S. citizen or lawful permanent resident.") and bar("Must be a U.S. citizen or lawful permanent resident.", visa))
check("citizenship: a preference is not a bar", not bar("U.S. citizenship preferred but not required."))
check("citizenship: 'U.S. citizen or national, U.S. permanent resident, or protected individual' is not cut at the period in U.S. and stays open to a green-card holder (Xcimer, 2026-10-18)",
      not bar("Must be a U.S. citizen or national, U.S. permanent resident, or protected individual as defined by 8 U.S.C. 1324b(a)(3). Apply below.")
      and bar("Must be a U.S. citizen or national. We cannot consider permanent residents at this time.")
      and bar("Must be a U.S. citizen or national, U.S. permanent resident, or protected individual.", visa))
v2, why2 = E.scan("Requirements: Must be a U.S. citizen or national. Benefits: a stipend.", gc)
check("the quote keeps 'U.S.' whole and stops at the real sentence end", v2 == "bar" and why2.endswith("citizen or national.'") , why2)
check("clearance: an active clearance or the ability to obtain one bars a non-citizen; 'may require' does not",
      bar("An active Secret clearance is required for this role.") and bar("Must be able to obtain and maintain a security clearance.")
      and not bar("This role may require eligibility to obtain a U.S. security clearance."))
check("ITAR: 'must be a U.S. person' is open to a permanent resident, closed to a visa holder; EAR licensing is never a bar",
      not bar("The person hired must be a \"U.S. person\" as defined in the ITAR.")
      and bar("The person hired must be a \"U.S. person\" as defined in the ITAR.", visa)
      and not bar("For certain non-U.S. persons to access this data, Axon must apply for a deemed export license.", visa))
check("sponsorship: 'OPT holders are not eligible' bars only someone who needs sponsorship",
      bar("F-1 students on OPT or CPT are not eligible for this position.", visa) and not bar("F-1 students on OPT or CPT are not eligible for this position."))
check("sponsorship: 'we do not sponsor' bars a sponsorship-needing applicant; a green-card holder is unaffected",
      bar("We will not sponsor applicants for work visas.", visa) and not bar("We will not sponsor applicants for work visas."))
check("degree: 'must be enrolled in a Master's program' bars an undergraduate; 'Bachelor's or Master's' does not",
      bar("Candidates must be currently enrolled in a Master's degree program.")
      and not bar("Must be currently pursuing a Bachelor's or Master's degree in Computer Science.")
      and not bar("Must be currently enrolled in a Master's degree program.", visa))
check("degree: 'must hold a bachelor's degree' bars a current undergraduate; 'or be pursuing one' does not",
      bar("Applicants must hold a Bachelor's degree in a technical field.") and not bar("Must have a bachelor's degree or be pursuing one."))
check("graduation: a window that ends before May 2028 bars; one that includes it does not",
      bar("Expected graduation date between December 2026 and June 2027.")
      and not bar("Expected graduation date between Fall 2027 and Summer 2028.")
      and not bar("Graduating between May 2027 and December 2029."))
check("graduation: 'must graduate by Spring 2027' bars a May 2028 graduate; 'class of 2028' is fine; 'class of 2027' with 'only' bars",
      bar("Must graduate by Spring 2027.") and not bar("Open to the class of 2028 only.") and bar("Only students in the class of 2027 are eligible."))
check("graduation: a term word naming the product, or an alternative term, is not a bar",
      not bar("Our product is fall detection for older adults; we hire interns for summer 2027.")
      and not bar("Internships run winter or summer 2027; graduation 2028 expected."))
check("graduation: silent when the applicant's date is unknown", not bar("Expected graduation between December 2026 and June 2027.", none))
check("a plain posting is ok", E.scan("Build backend services in Go. Interns work with a mentor. Remote OK.", gc) == ("ok", ""))
v, why = E.scan("Due to federal contract rules, applicants must be a U.S. citizen. We offer a stipend.", gc)
check("the reason quotes the sentence, not the whole posting", v == "bar" and "must be a U.S. citizen" in why and "stipend" not in why, why)

check("tier 4: pre-seed, a team of four, a founding engineer and a stealth company are signals; a Series B or 1,200 employees outranks them",
      bool(E.tier4_signal("We are a pre-seed startup building X.")) and bool(E.tier4_signal("Join our team of four engineers."))
      and bool(E.tier4_signal("Founding engineer intern wanted.")) and bool(E.tier4_signal("We are a stealth company."))
      and bool(E.tier4_signal("Team Size: 3")) and not E.tier4_signal("After our seed round we raised a $40M Series B.")
      and not E.tier4_signal("A team of five in the Boston office of a company with 1,200 employees.") and not E.tier4_signal("A large bank."))
check("tier 4: the switch", E.skip_tier4(Profile(career={}, answers={}, root=Path("/tmp")))
      and not E.skip_tier4(Profile(career={}, answers={"search": {"skip_tier4": False}}, root=Path("/tmp"))))

f = E.facts_from_profile(Profile(career={}, answers={"work_authorization": {"citizenship_status": "U.S. lawful permanent resident (green card holder)", "requires_us_sponsorship": "No"},
                                                     "education": {"graduation_date": "May 2028", "degree_level": "Bachelor's"}}, root=Path("/tmp")))
check("facts: read from answers.yaml", (f.citizen, f.permanent_resident, f.needs_sponsorship, f.grad_year, f.grad_month, f.degree_level) == (False, True, False, 2028, 5, "bachelor"), str(f))
f2 = E.facts_from_profile(Profile(career={"education_details": [{"education_level": "Bachelor of Science", "year_of_completion": "2028"}]}, answers={}, root=Path("/tmp")))
check("facts: career.yaml's education is the fallback; unknown citizenship stays unknown", f2.degree_level == "bachelor" and f2.grad_year == 2028 and f2.citizen is None)

check("api: each vendor's address",
      E.api_url("https://job-boards.greenhouse.io/acme/jobs/4567") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4567"
      and E.api_url("https://boards.greenhouse.io/acme/jobs/4567") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4567"
      and E.api_url("https://jobs.lever.co/acme/abc-123/apply") == "https://api.lever.co/v0/postings/acme/abc-123"
      and E.api_url("https://jobs.ashbyhq.com/acme/9f8e/application").startswith("https://api.ashbyhq.com/posting-api/job-board/acme#job=9f8e")
      and E.api_url("https://apply.workable.com/acme/j/ABCD1234/") == "https://apply.workable.com/api/v2/accounts/acme/jobs/ABCD1234"
      and E.api_url("https://jobs.smartrecruiters.com/Acme/744000012-swe-intern") == "https://api.smartrecruiters.com/v1/companies/Acme/postings/744000012"
      and E.api_url("https://beta.wd5.myworkdayjobs.com/x/job/1") == "" and E.fetch_text("https://beta.wd5.myworkdayjobs.com/x/job/1") is None)

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
