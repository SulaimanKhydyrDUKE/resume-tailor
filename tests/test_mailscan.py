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

from resume_tailor.mailbox import unforward
_own = {"sulaiman.khydyruulu@duke.edu"}
_flat = ("________________________________ From: no-reply@us.greenhouse-mail.io <no-reply@us.greenhouse-mail.io> Sent: Tuesday, September 29, 2026 8:02:06 AM "
         "To: Sulaiman Khydyr uulu <sulaiman.khydyruulu@duke.edu> Subject: Thank you for applying to GITAI Sulaiman, Thank you for your interest in the Field-Deployed role.")
_f, _s, _b, _w = unforward("Sulaiman Khydyr uulu <sulaiman.khydyruulu@duke.edu>", "FW: Thank you for applying to GITAI", _flat, _own)
check("forward (Outlook, run-together text): the original sender is read", "greenhouse-mail.io" in _f and _w)
check("forward: the subject loses its prefix", _s == "Thank you for applying to GITAI")
_lines = "---------- Forwarded message ---------\nFrom: Waymo Recruiting <no-reply@waymo.com>\nDate: Mon, Sep 28, 2026\nSubject: Thank You for Applying to Waymo!\nTo: <sulaiman.khydyruulu@duke.edu>\n\nHi Sulaiman, we got it."
_f2, _s2, _b2, _w2 = unforward("Sulaiman <sulaiman.khydyruulu@duke.edu>", "Fwd: Thank You for Applying to Waymo!", _lines, _own)
check("forward (Gmail, lines): sender and subject from the block, body after it", "waymo.com" in _f2 and _s2 == "Thank You for Applying to Waymo!" and _b2.strip().startswith("Hi Sulaiman"))
check("not a forward: a company's own mail passes through untouched",
      unforward("Acme <jobs@acme.com>", "Thank you for applying", "We got it.", _own) == ("Acme <jobs@acme.com>", "Thank you for applying", "We got it.", False))
check("the user's own outgoing mail is not a forward", unforward("Me <sulaiman.khydyruulu@duke.edu>", "Question about the role", "Hi, I applied last week.", _own)[3] is False)
check("a forwarded confirmation is classified as one", _rule_stage(_s, _b) == "applied")

# --- the company matcher: a product name in a body is not the sender ----------------
from resume_tailor.mailscan import _match_company, new_company_from_sender
_idx = {"microsoft": {"key": "microsoft", "names": {"Microsoft"}, "domains": {"microsoft.com"}, "slugs": {"microsoft"}},
        "td": {"key": "td", "names": {"TD", "TD Bank"}, "domains": set(), "slugs": {"td"}},
        "united": {"key": "united", "names": {"United", "United Airlines"}, "domains": {"united.com"}, "slugs": {"united"}},
        "chicago trading company": {"key": "chicago trading company", "names": {"Chicago Trading Company"}, "domains": set(), "slugs": {"chicagotrading"}}}
_teams = "When: Sunday 9:00 PM. Where: Microsoft Teams Meeting. Join: https://teams.microsoft.com/meet/1"
check("matcher: a Teams link in a recruiter's mail is not Microsoft",
      _match_company(_idx, "Ashlee Ruiz <ashleer@palantir.com>", "", "Palantir | Interview Confirmation", _teams) is None)
check("matcher: Workday mail for TD naming 'Microsoft Transcription' in its body is TD",
      _match_company(_idx, "TD@myworkday.com", "", "TD Careers: Thank you for your job application!", "If you are invited to interview, we may use Microsoft Transcription") == "td")
check("matcher: a newsletter about interview questions 'at Microsoft' is nobody's",
      _match_company(_idx, "Exponent <support@tryexponent.com>", "", "New interview questions recently asked at Microsoft", "") is None)
check("matcher: Microsoft's own mail, by domain", _match_company(_idx, "Microsoft Careers <donotreply@email.careers.microsoft.com>", "", "Thank you for your application!", "") == "microsoft")
check("matcher: 'United States' in a rejection body is not United", _match_company(_idx, "Voyager <recruiting@voyagertechnologies.com>", "", "Update on your application to Voyager Technologies", "work in the United States") is None)
check("matcher: an ATS mail naming a distinctive company only in its body still matches",
      _match_company(_idx, "no-reply@greenhouse.io", "", "Your application", "Thank you for applying to Chicago Trading Company.") == "chicago trading company")
check("new company: a recruiter's own domain with an interview confirmation gets its own bucket",
      new_company_from_sender(_idx, "Ashlee Ruiz <ashleer@palantir.com>", "", "Palantir | Interview Confirmation", "Your interview has been confirmed") == "palantir" and "palantir" in _idx)
check("new company: not from gmail, an ATS, or for a mere confirmation",
      new_company_from_sender(_idx, "someone@gmail.com", "", "Interview Confirmation", "interview has been confirmed") is None
      and new_company_from_sender(_idx, "no-reply@greenhouse.io", "", "Interview Confirmation", "interview has been confirmed") is None
      and new_company_from_sender(_idx, "jobs@acmecorp.com", "", "Thank you for applying", "we received your application") is None)

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
