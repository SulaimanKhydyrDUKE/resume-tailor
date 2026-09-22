"""Inbox classification rules: what a subject or body settles on its own."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from resume_tailor.mailscan import _rule_stage, _company_stage

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))

TTD = ("The Trade Desk I Application Update",
       "Hi Sulaiman, Thank you for expressing your interest in the 2027 Software Engineering Internship at The Trade Desk! "
       "We were delighted to receive your application. The entire process usually ranges from 2 to 8 weeks. The steps are as follows: "
       "A University Recruiter will review your resume. You may be asked to complete a coding exercise which is done using CodeSignal. "
       "If your application meets the requirements, a recruiter will reach out to schedule a call.")
check("a process description in a confirmation is not an interview", _rule_stage(*TTD) != "interview")
check("nor an assessment", _rule_stage(*TTD) != "oa")
GW = ("Continue to apply for the job Enterprise Analytics - Summer 2027 Internship",
      "Hello Sulaiman, We saved a draft of your job application for the job Enterprise Analytics. We invite you to complete and submit your job application.")
check("Oracle's saved-draft mail is a draft, not an assessment", _rule_stage(*GW) == "draft")
check("a draft by body wording alone", _rule_stage("Your application", GW[1]) == "draft")
check("'Interview Feedback for Application Review' is not settled by its subject",
      _rule_stage("Interview Feedback for Application Review at IMC", "Your application review feedback is available in the portal.") is None)
check("a real interview subject still is", _rule_stage("Interview invitation: Software Engineer Intern", "") == "interview")
check("an assessment invitation still is", _rule_stage("IMC Trading | Please Complete Online Assessment", "") == "oa")
check("'invite you to take the online assessment' in a body is an assessment",
      _rule_stage("Next steps", "We would like to invite you to take the online assessment within 5 days.") == "oa")
check("a rejection subject still is", _rule_stage("Update on your application", "Unfortunately we will not be moving forward with your application.") == "rejected")
check("'Important information about your application' is not a confirmation by subject",
      _rule_stage("Important information about your application to The Trade Desk", "We have made the decision to close this role and will not be moving forward.") == "rejected")
check("'Your application to X' still is one", _rule_stage("Your application to Netflix", "Thanks! We got it.") == "applied")
check("a company named 'Information Solutions' is still a confirmation",
      _rule_stage("Thank you for applying to National Information Solutions Cooperative", "We got it.") == "applied")
check("'keep track of its status' is still a confirmation",
      _rule_stage("Thank you for your application: keep track of its status", "Thanks for applying.") == "applied")
check("'Application Has Been Received | Next Steps' is still a confirmation",
      _rule_stage("Your Application Has Been Received | Next Steps", "We will review it.") == "applied")
check("'Information about your application to X' is not settled by subject",
      _rule_stage("Information about your application to Appian", "We appreciate your interest.") is None)
check("a closed role in the subject is a rejection", _rule_stage("We have closed the position", "") == "rejected")
check("a clearance questionnaire is paperwork, not an interview",
      _rule_stage("Please Review and Complete: What It Means To Hold A Government Sponsored Security Clearance", "Please complete the form to schedule a call.") == "other")
check("'Request for Information' is paperwork", _rule_stage("BTI360 Internship: Request for Information", "") == "other")
check("a draft never sets the company stage",
      _company_stage([{"stage": "draft", "when": "2026-09-09"}, {"stage": "applied", "when": "2026-09-03"}]) == "applied")
check("a draft alone leaves the company without a stage", _company_stage([{"stage": "draft", "when": "2026-09-09"}]) == "")

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
