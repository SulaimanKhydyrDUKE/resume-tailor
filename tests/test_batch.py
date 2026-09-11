"""Unit tests for the pieces that make unattended batch mode safe rather than
merely fast: queue parsing, dedup state, the never-guess-on-a-real-field rule
in the boilerplate autofiller, and refusing an ambiguous submit button.

No network, no browser — these are the deterministic policy decisions the
runner makes before it ever touches a page.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor.batch import _autofill_boilerplate, _pick_submit_button, _as_amount
from resume_tailor.queue import QueueEntry, RunState, load_queue


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


RESULTS = []

# --- queue parsing -----------------------------------------------------

import tempfile

with tempfile.TemporaryDirectory() as d:
    qpath = Path(d) / "queue.yaml"
    qpath.write_text("""
- url: "https://example.com/job/1"
  company: "Acme"
- text: |
    A pasted job description that has no URL at all.
  apply_url: "https://example.com/apply/2"
  company: "Beta"
- "https://example.com/job/3"
""")
    entries = load_queue(qpath)
    check("three entries loaded", len(entries) == 3, len(entries))
    check("url entry id is the url itself", entries[0].id == "https://example.com/job/1")
    check("text-only entry gets a content-derived id", entries[1].id and entries[1].url == "", entries[1].id)
    check("apply_url falls back to url when unset", entries[0].apply_url == entries[0].url)
    check("apply_url explicit override respected", entries[1].apply_url == "https://example.com/apply/2")
    check("bare-string shorthand accepted", entries[2].url == "https://example.com/job/3")
    check("same text -> same id (dedup-stable across runs)",
          load_queue(qpath)[1].id == entries[1].id)

# --- run state / resumability -------------------------------------------

with tempfile.TemporaryDirectory() as d:
    spath = Path(d) / "state.json"
    state = RunState.load(spath)
    check("fresh state has nothing done", state.done == {})

    state.record("job-1", {"status": "applied", "company": "acme"})
    state.applied_companies.add("acme")
    state.save()

    reloaded = RunState.load(spath)
    check("state persists across load", reloaded.done.get("job-1", {}).get("status") == "applied")
    check("applied_companies persists", "acme" in reloaded.applied_companies)
    check("an applied entry is not re-attempted by default",
          reloaded.already_attempted("job-1", retry_statuses={"error", "needs_login", "blocked"}))

    reloaded.record("job-2", {"status": "error", "detail": "boom"})
    check("an error entry IS re-attempted (in the retryable set)",
          not reloaded.already_attempted("job-2", retry_statuses={"error", "needs_login", "blocked"}))
    check("a never-seen entry is not attempted-yet",
          not reloaded.already_attempted("job-3", retry_statuses={"error"}))

    from resume_tailor.queue import company_key
    check("company key: parentheticals, punctuation and suffixes are not the name",
          company_key("Chicago Trading Company (CTC)") == company_key("Chicago Trading Company") == company_key("Chicago Trading Co.") == "chicago trading")
    check("company key: distinct companies stay distinct", company_key("HP IQ") != company_key("HP") and company_key("Zip HQ, Inc") == "zip hq")
    check("company key: a leading 'The' is not the name",
          company_key("The Johns Hopkins University Applied Physics Laboratory (APL)") == company_key("Johns Hopkins University Applied Physics Laboratory"))
    reloaded.mark_applied("Chicago Trading Company (CTC)")
    check("apply-once: the same company under another spelling is recognised",
          reloaded.has_applied("Chicago Trading Company") and not reloaded.has_applied("Chicago Mercantile"))
    _held = RunState(path=spath, done={"a": {"status": "awaiting_approval", "company": "Jane Street"},
                                       "b": {"status": "applied", "company": "Acme"}})
    check("held_at: a company with an application awaiting approval is occupied", _held.held_at("Jane Street"))
    check("held_at: the held record itself is not its own conflict", not _held.held_at("Jane Street", except_id="a"))
    check("held_at: an applied company is not 'held'", not _held.held_at("Acme"))
    check("held_at: unknown company", not _held.held_at("Nowhere Inc"))

    # A required statement — an instruction to the applicant — is acknowledged, not guessed at.
    _one_role = ("We will only consider you for one role/location at a time. Therefore, if you are interested in "
                 "multiple roles, please submit an application for your first preference only.*")
    check("boilerplate: a required 'one role at a time' statement with Yes/No takes Yes",
          _autofill_boilerplate(_one_role, {"type": "radio", "required": True}, ["Yes", "No"]) == "Yes")
    check("boilerplate: the same statement as a lone required box is ticked",
          _autofill_boilerplate(_one_role, {"type": "checkbox", "required": True}, []) == "yes")
    check("boilerplate: a real question with Yes/No is still not acknowledged",
          _autofill_boilerplate("Have you worked at Optiver before?*", {"type": "radio", "required": True}, ["Yes", "No"]) is None)

    # --- trivial required questions that used to stall an application ---
    from resume_tailor.planner import _ASKS_ABOUT_OWN_TIES, _SILENCE, _NO_LIKE, _title_to_company, THIRD_PARTY
    from resume_tailor.profile import Profile as _P
    _amex ="Do you or your spouse or life partner have an Immediate Family Member or a Close Personal Relationship with a current American Express employee?*"
    check("ties: a question about the candidate's own family ties is not 'about someone else'",
          bool(THIRD_PARTY.search(_amex)) and bool(_ASKS_ABOUT_OWN_TIES.search(_amex)))
    check("ties: the record's silence answers it No", bool(_SILENCE.search(_amex)) and bool(_NO_LIKE.match("No")))
    check("ties: a referrer's own name is still about someone else", not _ASKS_ABOUT_OWN_TIES.search("Referrer's full name*"))
    check("boilerplate: 'What is their name?' after a No is N/A",
          _autofill_boilerplate("What is their name?*", {"type": "textarea", "required": True}, []) == "N/A")
    check("boilerplate: an optional 'their name' stays blank",
          _autofill_boilerplate("What is their name?", {"type": "textarea", "required": False}, []) is None)
    _rec = _P(career={"experience_details": [{"position": "Undergraduate Teaching Assistant — CS 210 Introduction to Computer Systems", "company": "Duke University, Department of Computer Science"}]}, answers={}, root=Path("."))
    check("employer: a role title given for 'most recent employer' becomes its company",
          _title_to_company(_rec, "Undergraduate Teaching Assistant — CS 210 Introduction to Computer Systems") == "Duke University, Department of Computer Science")
    check("employer: a company name is left alone", _title_to_company(_rec, "Duke University") is None)

    # --- a saved draft never outranks the bank on a hard fact ---
    from resume_tailor.batch import _HARD_FACT_Q, _same_answer
    check("draft: a sponsorship question is a hard fact", bool(_HARD_FACT_Q.search("Will you now or in the future require visa sponsorship?")))
    check("draft: a preferred contact method is not", not _HARD_FACT_Q.search("Preferred Contact Method"))
    check("draft: 'No' held against the bank's 'No' is the same answer", _same_answer("No", "No", ["Yes", "No"]))
    check("draft: 'Yes' held against the bank's 'No' is not", not _same_answer("No", "Yes", ["Yes", "No"]))
    check("draft: the bank's wording maps onto the page's option", _same_answer("Yes", "Yes, without restriction", ["Yes, without restriction", "No"]))
    check("draft: an empty box is not an answer", not _same_answer("No", "", ["Yes", "No"]))

    # --- money questions: one number, in the box's own unit ---
    from resume_tailor.batch import _money_answer
    _pay = _P(career={}, answers={"salary_expectations": {"hourly_rate_usd": "35", "annual_salary_expectation": "$70,000",
                                                          "desired_compensation": "$30-40 per hour"}}, root=Path("."))
    check("money: an annual box gets the annual figure, not 3040",
          _money_answer(_pay, "Desired Total Annual Compensation", {"type": "text"}, "$30-40 per hour") == "70000")
    check("money: an hourly box gets the hourly figure",
          _money_answer(_pay, "Expected hourly pay rate", {"type": "text"}, "$30-40 per hour") == "35")
    check("money: a number already is left as digits",
          _money_answer(_pay, "Salary expectation", {"type": "text"}, "$70,000") == "70000")
    check("money: a dropdown of bands is not touched",
          _money_answer(_pay, "Salary expectation", {"type": "text", "options": ["$60k-$80k"]}, "$60k-$80k") == "$60k-$80k")
    check("money: a question that is not about money is not touched",
          _money_answer(_pay, "Rate your Python skill", {"type": "text"}, "Expert") == "Expert")

    # --- the résumé on its skeleton: every role, every slot, the given file name ---
    from resume_tailor.compose import document_from_base
    from resume_tailor.models import Bullet, RoleDraft
    from resume_tailor.profile import Profile as _P
    from resume_tailor.tailor import _slot_of, resume_file_name
    _career = {"personal_information": {"name": "Ada", "surname": "Lovelace", "email": "ada@x.org", "phone": "1", "github": "https://github.com/ada"},
               "education_details": [{"education_level": "B.S.", "field_of_study": "CS", "institution": "Duke", "location": "Durham, NC", "year_of_completion": "2028"}],
               "experience_details": [{"company": "Acme", "position": "Intern", "employment_period": "2026", "location": "NYC"},
                                      {"company": "Beta", "position": "RA", "employment_period": "2025", "location": "Durham"}]}
    _base = {"file_name": "Ada_Lovelace_resume.pdf", "header": {"email": "ada@duke.edu"},
             "education": {"coursework": "OS, DB", "lines": ["Teaching Assistant: CS 210"]},
             "roles": [{"id": "exp0", "title": "Intern", "org": "Acme Inc", "dates": "2026", "location": "NYC", "base": ["Did A", "Did B"]},
                       {"id": "exp1", "title": "RA", "org": "Beta Lab", "dates": "2025", "location": "Durham", "base": ["Did C"]}],
             "projects": [{"name": "Proj", "tech": "Python", "dates": "2025", "bullets": ["Built P"]}],
             "skills": [{"label": "Languages", "terms": "Python, C"}], "honors": ["Won X"]}
    _drafts = [RoleDraft(evidence_id="exp0", bullets=[Bullet(action="Did A for the posting", outcome="", source_fact_ids=["exp0.b0"], numerals_used=[], derived_numerals=[])])]
    _html = document_from_base(_career, _base, _drafts)
    check("skeleton: every role appears in the skeleton's order", _html.index("Acme Inc") < _html.index("Beta Lab"))
    check("skeleton: a role with a draft shows the tailored bullet", "Did A for the posting" in _html)
    check("skeleton: a role without a draft shows its own words", "Did C" in _html)
    check("skeleton: projects, skills, honors and the education lines are printed as written",
          all(s in _html for s in ("Built P", "Python, C", "Won X", "OS, DB", "Teaching Assistant:")))
    check("skeleton: the header e-mail override wins", "ada@duke.edu" in _html and "ada@x.org" not in _html)
    check("skeleton: no company name in the file name",
          resume_file_name(_P(career=_career, answers={}, root=Path("."), base_resume=_base)) == "Ada_Lovelace_resume.pdf")
    check("skeleton: the file name falls back to First_Last_resume.pdf",
          resume_file_name(_P(career=_career, answers={}, root=Path("."))) == "Ada_Lovelace_resume.pdf")
    check("skeleton: a reworded bullet is matched to its slot by the base id it cites",
          _slot_of(Bullet(action="x", outcome="", source_fact_ids=["exp0.r1", "exp0.b1"], numerals_used=[], derived_numerals=[]), "exp0") == 1)
    _p = _P(career=_career, answers={}, root=Path("."), base_resume=_base)
    check("skeleton: base bullets are citable facts", _p.evidence_index().get("exp0.b1", "").endswith("résumé: Did B"))

    # Two processes on one state file: the watch and a hand-run retry. Each
    # must keep the other's records, and the later write must not clobber.
    a, b = RunState.load(spath), RunState.load(spath)
    a.record("job-A", {"status": "applied"})
    a.applied_companies.add("aco")
    a.save()
    b.record("job-B", {"status": "needs_review"})
    b.record("job-2", {"status": "applied"})  # b re-records an entry a also holds; b's newer record wins
    merged = RunState.load(spath)
    check("state: records from two writers are both kept",
          merged.done.get("job-A", {}).get("status") == "applied" and merged.done.get("job-B", {}).get("status") == "needs_review")
    check("state: an entry re-recorded by the later writer takes its latest status", merged.done["job-2"]["status"] == "applied")
    check("state: applied companies merge", {"acme", "aco"} <= merged.applied_companies)
    check("state: the writer's own view is refreshed after saving", "job-A" in b.done and "job-B" in b.done)

# --- boilerplate autofill: the never-guess-on-a-real-field boundary ------

consent = {"type": "checkbox", "tag": "input"}
check("consent checkbox with agreement wording -> autofilled",
      _autofill_boilerplate("I certify the above is accurate", consent) == "yes")
check("consent checkbox, terms wording -> autofilled",
      _autofill_boilerplate("I agree to the Terms of Service", consent) == "yes")
check("an unrelated checkbox is NOT autofilled",
      _autofill_boilerplate("Subscribe me to the newsletter", consent) is None)
check("work-authorization phrased as a checkbox is NOT autofilled — this is the line that matters",
      _autofill_boilerplate("I am authorized to work in this country", consent) is None)

source_select = {"type": "select-one", "tag": "select", "options": ["Select…", "LinkedIn", "Other", "Referral"]}
check("'how did you hear about us' picks a generic present option",
      _autofill_boilerplate("How did you hear about us?", source_select) == "Other")
check("'how did you hear' prefers a job-board option over Other — that is where the postings come from",
      _autofill_boilerplate("How did you hear about this role?", {"type": "radio", "tag": "input"},
                            ["LinkedIn", "Job Board", "Other"]) == "Job Board")
check("'how did you hear' with only named sources present (no generic) is NOT guessed",
      _autofill_boilerplate("How did you hear about us?", {"type": "checkbox", "tag": "input"},
                            ["LinkedIn", "A friend", "Campus event"]) is None)
check("'how did you hear' as free text gets the honest job-board answer",
      "job board" in (_autofill_boilerplate("How did you hear about HP IQ? *", {"type": "text", "tag": "input"}) or "").lower())
check("'how you heard about this position' (no 'did') is a source field, answered with the job board even when a referral name is invited",
      "job board" in (_autofill_boilerplate("Please indicate how you heard about this position. If you were referred, please write the employee's name here.",
                                            {"type": "text", "tag": "input"}) or "").lower())
check("a REQUIRED 'who referred you' box gets an honest N/A rather than a name or a stall",
      (_autofill_boilerplate("Who referred you to this position? Enter their first and last name here.",
                             {"type": "text", "tag": "input", "required": True}) or "").startswith("N/A"))
check("an OPTIONAL 'who referred you' box is left blank",
      _autofill_boilerplate("Who referred you to this position?", {"type": "text", "tag": "input", "required": False}) is None)
check("'How did you first hear about this role?' and 'From what source did you hear of the job opening?' are source fields",
      _autofill_boilerplate("How did you first hear about this role?", {"type": "radio", "tag": "input"}, ["LinkedIn", "Job Board", "Other"]) == "Job Board"
      and "job board" in (_autofill_boilerplate("From what source did you hear of the job opening?", {"type": "text", "tag": "input"}) or "").lower())
check("'if you were referred, name them' is NOT a source field",
      _autofill_boilerplate("If you were referred by a current employee, please write their name", {"type": "text", "tag": "input"}) is None)
check("a picker with an unread list is not answered with free text",
      _autofill_boilerplate("Where did you hear about us?", {"type": "text", "tag": "input", "combobox": True}) is None)
salary_select = {"type": "select-one", "tag": "select", "options": ["Select…", "$50k", "$100k", "$150k"]}
check("a salary-range select is NOT autofilled by the source-field heuristic",
      _autofill_boilerplate("Expected salary range", salary_select) is None)

# --- submit button selection: ambiguity must refuse, not guess -----------

check("exactly one plausible submit button is chosen",
      _pick_submit_button([{"text": "Submit Application", "disabled": False, "selector": "#a"},
                           {"text": "Cancel", "disabled": False, "selector": "#b"}]) is not None)
check("'Submit' outranks an 'Apply Now' control on the same page",
      (_pick_submit_button([{"text": "Submit", "disabled": False, "selector": "#a"},
                            {"text": "Apply Now", "disabled": False, "selector": "#b"}]) or {}).get("selector") == "#a")
check("two equally plausible buttons -> refuses rather than picking one",
      _pick_submit_button([{"text": "Apply Now", "disabled": False, "selector": "#a"},
                           {"text": "Send application", "disabled": False, "selector": "#b"}]) is None)
check("zero plausible buttons -> refuses",
      _pick_submit_button([{"text": "Cancel", "disabled": False, "selector": "#a"},
                           {"text": "Back", "disabled": False, "selector": "#b"}]) is None)
check("a disabled submit-looking button is not counted as a candidate",
      _pick_submit_button([{"text": "Submit", "disabled": True, "selector": "#a"},
                           {"text": "Apply", "disabled": False, "selector": "#b"}]) is not None)
check("Greenhouse: the 'Apply' pill above the form loses to 'Submit application'",
      (_pick_submit_button([{"text": "Apply", "disabled": False, "selector": "#pill", "type": "button"},
                            {"text": "Submit application", "disabled": False, "selector": "#go", "type": "submit"}]) or {}).get("selector") == "#go")
check("two 'submit' buttons still refuse",
      _pick_submit_button([{"text": "Submit", "disabled": False, "selector": "#a", "type": "submit"},
                           {"text": "Submit and continue", "disabled": False, "selector": "#b", "type": "submit"}]) is None)
check("a lone type=submit 'Apply now' among plain 'Send' links is chosen",
      (_pick_submit_button([{"text": "Apply now", "disabled": False, "selector": "#a", "type": "submit"},
                            {"text": "Send feedback", "disabled": False, "selector": "#b", "type": "button"}]) or {}).get("selector") == "#a")


# --- two independent judges: both must pass -----------------------------------
from resume_tailor.batch import _is_essay
from resume_tailor.judge import passes, reasons, summarize
from resume_tailor.models import FitVerdict

def V(score=92, disq=False, verdict="submit", unmet=None, reason="ok"):
    return FitVerdict(score=score, disqualified=disq, unmet_hard_requirements=unmet or [], strengths=[],
                      verdict=verdict, reason=reason)

check("judge: strong verdict passes", passes(V()))
check("judge: the bar is 90 — an 85 with 'submit' is held", not passes(V(score=85)))
check("judge: the bar is configurable", passes(V(score=85), min_score=80))
check("judge: a 'hold' recommendation on a score above the bar no longer blocks (the user's call)",
      passes(V(score=95, verdict="hold")))
check("judge: disqualified fails regardless", not passes(V(score=95, disq=True)))
both = {"hiring manager": V(94), "screener": V(91)}
check("summarize shows both scores", summarize(both) == "hiring manager 94/100 pass | screener 91/100 pass", summarize(both))
one_bad = {"hiring manager": V(94), "screener": V(40, unmet=["TS/SCI clearance"], reason="Clearance required.", verdict="hold")}
check("reasons names only the failing judge and its unmet requirement",
      reasons(one_bad).startswith("screener:") and "TS/SCI" in reasons(one_bad), reasons(one_bad))
from resume_tailor.judge import cached_scores_ok, disqualified, revision_notes, weakest
check("judge: revision notes carry both judges' reasons and unmet points",
      "hiring manager scored 94" in revision_notes(one_bad) and "TS/SCI" in revision_notes(one_bad))
check("judge: weakest score and disqualification are read across both judges",
      weakest(one_bad) == 40 and not disqualified(one_bad) and disqualified({"a": V(95, disq=True, unmet=["U.S. citizenship required"])}))
check("judge: an earlier 85/95 verdict does not meet today's 90 bar; 92/95 does",
      not cached_scores_ok("hiring manager 85/100 pass | screener 95/100 pass") and cached_scores_ok("hiring manager 92/100 pass | screener 95/100 pass"))
check("judge: a recorded HOLD below the bar does not count as cached-good", not cached_scores_ok("hiring manager 95/100 pass | screener 60/100 HOLD"))
_per = {"hiring manager": 85, "screener": 90}
check("judge: per-judge bars — the hiring manager passes at 85, the screener needs 90",
      passes(V(score=85), _per, "hiring manager") and not passes(V(score=85), _per, "screener") and passes(V(score=90), _per, "screener"))
check("judge: a recorded 'hiring manager 87/100 HOLD | screener 95/100 pass' is cached-good under per-judge bars",
      cached_scores_ok("hiring manager 87/100 HOLD | screener 95/100 pass (revised ×2)", _per)
      and not cached_scores_ok("hiring manager 84/100 HOLD | screener 95/100 pass", _per))
check("judge: a missing skill is a low score, not a disqualification, so the apply-anyway rule can proceed",
      not disqualified({"screener": V(score=70, unmet=["Node"], reason="no Node")}) and disqualified({"screener": V(score=70, disq=True, unmet=["Must be a U.S. citizen"])})
      and not disqualified({"screener": V(score=70, disq=True, unmet=["Node.js"])}))
check("judge: summarize reflects per-judge bars",
      summarize({"hiring manager": V(86), "screener": V(91)}, _per) == "hiring manager 86/100 pass | screener 91/100 pass")
check("essay detection: textarea", _is_essay({"tag": "textarea", "type": "textarea", "label": "Why us?"}))
check("essay detection: long text label", _is_essay({"tag": "input", "type": "text", "label": "Tell us about a project you are proud of and what you learned from it"}))
check("essay detection: short text field is not an essay", not _is_essay({"tag": "input", "type": "text", "label": "Full name"}))

# --- location-dependent address, and the company blacklist ---------------------
from resume_tailor.profile import Profile as _P
_answers = {
    "address": {"street": "102 Wannamaker Drive", "city": "Durham", "state": "NC", "zip": "27708", "country": "United States",
                "alternates": [{"when_location_matches": ["Minnesota", "MN", "Minneapolis", "St. Paul"],
                                "street": "5873 Bayberry Dr", "city": "White Bear Lake", "state": "MN", "zip": "55110"}]},
    "work_preferences": {"open_to_relocation": "Yes"},
    "search": {"company_blacklist": ["Palantir"]},
}
_prof = _P(career={"personal_information": {"name": "A", "surname": "B"}}, answers=_answers, root=Path("/tmp"))
check("address: default is the campus address", _prof.lookup("Street Address*")[0] == "102 Wannamaker Drive")
mn = _prof.for_location("Minneapolis, MN")
check("address: a Minnesota posting switches street/city/zip",
      (mn.lookup("Street Address*")[0], mn.lookup("City*")[0], mn.lookup("Zip*")[0]) == ("5873 Bayberry Dr", "White Bear Lake", "55110"))
check("address: 'MN' is matched as a whole token only", _prof.for_location("Remote in USA") is _prof and _prof.for_location("Omni Corp") is _prof)
check("address: state name matches case-insensitively", _prof.for_location("Rochester, minnesota").lookup("Zip*")[0] == "55110")
check("address: the original profile is untouched", _prof.lookup("Zip*")[0] == "27708")
check("applicant facts carry the switched location", "White Bear Lake" in mn.applicant_facts() and "Durham" in _prof.applicant_facts())
check("blacklist: substring, case-insensitive", _prof.blacklisted("Palantir Technologies") and _prof.blacklisted("PALANTIR"))
check("blacklist: other companies pass", not _prof.blacklisted("Stripe") and not _prof.blacklisted(""))

# --- answer-bank lookup: a filler word alone must not make a match ----------------
_bank = _P(career={"personal_information": {"name": "A", "surname": "B", "phone": "555-0100", "phone_prefix": "+1"}},
           answers={"work_preferences": {"remote_work": "Yes", "hybrid_work": "Yes", "open_to_relocation": "Yes"},
                    "work_authorization": {"default_region": "us", "us_work_authorization": "Yes", "requires_us_sponsorship": "No"},
                    "education": {"graduation_year": "2028", "end_date_year": "2028"}},
           root=Path("/tmp"))
check("lookup: 'employed by TELUS' does not land on remote_work via the word 'work'",
      _bank.lookup("Have you ever been employed by, consulted for, or performed contract work for TELUS?")[0] is None)
check("lookup: 'open to remote work' still matches remote_work",
      _bank.lookup("Are you open to remote work?")[0] == "Yes")
check("lookup: 'authorized to work in the US' still matches",
      _bank.lookup("Are you authorized to work in the US?")[0] == "Yes")
check("lookup: 'willing to relocate' matches open_to_relocation on its distinctive word",
      _bank.lookup("Are you willing to relocate?")[0] == "Yes")
check("lookup: the same question buried in a long sentence is left to the grounded answerer",
      _bank.lookup("If you are not located in one of the above hubs, are you willing to relocate?")[0] is None)
check("lookup: 'End date year' matches end_date_year", _bank.lookup("End date year*")[0] == "2028")
check("lookup: single-word keys are unaffected", _bank.lookup("Phone")[0] == "+1 555-0100")

# --- word kinship: inflections yes, shared prefixes no ---------------------------------
import asyncio as _aio
from resume_tailor.batch import _resolve as _resolve_q
from resume_tailor.profile import _akin
check("akin: inflections and derivations match",
      _akin("authorized", "authorization") and _akin("graduating", "graduation") and _akin("relocate", "relocation")
      and _akin("interest", "interested") and _akin("sponsor", "sponsorship") and _akin("salary", "salaries"))
check("akin: a shared prefix alone does not",
      not _akin("interest", "international") and not _akin("person", "personal") and not _akin("state", "statement")
      and not _akin("interest", "internship") and not _akin("work", "workplace"))
_bank2 = _P(career={"personal_information": {"name": "A", "surname": "B"}},
            answers={"work_preferences": {"preferred_platform": "Backend"},
                     "address": {"state": "NC", "city": "Durham"},
                     "work_authorization": {"default_region": "us", "us_person_itar": "Yes", "requires_us_sponsorship": "No"},
                     "availability": {"target_term": "Summer 2027"}},
            root=Path("/tmp"))
check("lookup: 'why are you interested in us' is not answered from a platform key",
      _bank2.lookup("Tell us a little bit about why you are interested in TELUS Digital...")[0] is None)
check("lookup: 'TELUS International ... your role' is not answered from a platform key",
      _bank2.lookup("If you have been previously employed by TELUS International, specify the company, your role, and dates")[0] is None)
check("lookup: 'platform that interests you' still reaches preferred_platform",
      _bank2.lookup("Please select the platform that interests you the most.")[0] == "Backend")
check("lookup: 'personal statement' matches neither us_person nor state",
      _bank2.lookup("Please provide a personal statement")[0] is None)
check("lookup: a short ITAR question reaches the U.S.-person key",
      _bank2.lookup("Are you a U.S. Person as defined by ITAR?")[0] == "Yes")
check("lookup: min_score=1.0 rejects a half-matched key that 0.5 accepts",
      _bank2.lookup("Which platform interests you?", min_score=1.0)[0] is None
      and _bank2.lookup("Which platform interests you?")[0] == "Backend"
      and _bank2.lookup("City", min_score=1.0)[0] == "Durham")
check("resolve: a checkbox list still takes an option label ('Other' for how-did-you-hear)",
      _aio.run(_resolve_q("How did you hear about HP IQ? *", {"type": "checkbox", "tag": "input", "required": True},
                          ["LinkedIn", "A friend", "Other"], _bank2, "")) == "Other")

check("resolve: a consent checkbox is ticked, not filled with target_term via 'terms'",
      _aio.run(_resolve_q("I agree to the Terms of Service", {"type": "checkbox", "tag": "input", "required": False},
                          [], _bank2, "")) == "yes")

# --- lookup: a key must account for a fair share of the question ---------------------
_bank3 = _P(career={"personal_information": {"name": "A", "surname": "B", "linkedin": "https://linkedin.com/in/ab"}},
            answers={"address": {"zip": "27708", "state": "NC"},
                     "work_authorization": {"default_region": "us", "us_work_authorization": "Yes"},
                     "education": {"university": "Duke University", "degree": "B.S."}},
            root=Path("/tmp"))
check("lookup: one shared word in a long question is not a match (the referrer's name)",
      _bank3.lookup("If you were referred by a current or former intern or team member, please write their name in the box below.")[0] is None)
check("lookup: 'LinkedIn Profile URL' still matches linkedin",
      _bank3.lookup("LinkedIn Profile URL")[0] == "https://linkedin.com/in/ab")
check("lookup: an OPT/STEM question does not borrow us_work_authorization",
      _bank3.lookup("Do you have a STEM degree that would make you eligible for extended work authorization?")[0] is None)
check("lookup: 'Are you authorized to work in the US?' still matches",
      _bank3.lookup("Are you currently authorized to work in the US?")[0] == "Yes")
check("lookup: 'Legal Name (First Name Last Name)' gives the whole name",
      _bank3.lookup("Legal Name (First Name Last Name)")[0] == "A B")
check("lookup: 'First Name' alone is still the first name", _bank3.lookup("First Name*")[0] == "A")
check("lookup: 'Postal Code' is the zip", _bank3.lookup("Postal Code")[0] == "27708")
_bank4 = _P(career={"personal_information": {"name": "A", "surname": "B", "city": "Durham, NC"}},
            answers={"address": {"city": "Durham", "state": "NC"}}, root=Path("/tmp"))
check("lookup: 'Location (City)' — 'Durham' and 'Durham, NC' are one answer; the address one wins",
      _bank4.lookup("Location (City)*")[0] == "Durham")
check("lookup: 'Location' alone still gives the header location", _bank4.lookup("Location")[0] == "Durham, NC")
check("lookup: 'Which university/college do you attend?' still matches",
      _bank3.lookup("Which university/college do you attend?")[0] == "Duke University")

# --- entity gate: sentence punctuation and hyphenated compounds are not names --------
from resume_tailor.gates import build_entity_allowlist, check_entities
_allow = build_entity_allowlist("Built DukeGPT with FastAPI and microservices. Shipped React features. AI assistant.")
check("entity gate: a record word ending a sentence is still that word",
      check_entities("I built the assistant using microservices and FastAPI.", _allow) == [])
check("entity gate: 'AI-driven' passes when AI is in the record and 'driven' is plain vocabulary",
      check_entities("I bring AI-driven and real-world experience.", _allow) == [])
check("entity gate: an invented technology is still caught",
      any("Kubernetes" in v.detail for v in check_entities("I deployed it on Kubernetes.", _allow)))
check("entity gate: an invented hyphenated name is still caught",
      check_entities("I used Foo-Bar for that.", _allow) != [])

# --- the form-level plan, held to the rails ----------------------------------------------
from resume_tailor import planner
from resume_tailor.models import FieldAnswer
from resume_tailor.apply import _date_text, _parse_date
from resume_tailor.batch import _pick_next_button, _bank

_pl = _P(career={"personal_information": {"name": "A", "surname": "B", "phone": "555-0100", "phone_prefix": "+1"}},
         answers={"address": {"city": "Durham", "state": "NC"},
                  "work_preferences": {"open_to_relocation": "Yes"},
                  "work_authorization": {"default_region": "us", "requires_us_sponsorship": "No"}},
         root=Path("/tmp"))
_qs = [
    {"qid": "q1", "key": planner.question_key("Emergency contact phone", "tel"), "label": "Emergency contact phone",
     "section": "", "widget": "tel", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q2", "key": planner.question_key("Phone", "tel"), "label": "Phone",
     "section": "", "widget": "tel", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q3", "key": planner.question_key("Second office preference", "radio"), "label": "Second office preference",
     "section": "", "widget": "radio", "options": ["Charlottesville, VA", "Durham, NC", "N/A"], "required": True,
     "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q4", "key": planner.question_key("Languages you know", "checkboxes"), "label": "Languages you know",
     "section": "", "widget": "checkboxes", "options": ["Python", "Go", "Rust"], "required": False,
     "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q5", "key": planner.question_key("Why us?", "textarea"), "label": "Why us?",
     "section": "", "widget": "textarea", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q6", "key": planner.question_key("GPA", "text"), "label": "GPA",
     "section": "", "widget": "text", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
]
_plan = planner.rails([
    FieldAnswer(id="q1", answer="+1 555-0100", basis=["personal.phone"], skip=False, essay=False, reason="phone"),
    FieldAnswer(id="q2", answer="+1 555-0100", basis=["personal.phone"], skip=False, essay=False, reason="phone"),
    FieldAnswer(id="q3", answer="Charlottesville", basis=["work_preferences.open_to_relocation"], skip=False, essay=False, reason="any office"),
    FieldAnswer(id="q4", answer="Python | Rust", basis=["skill0.0"], skip=False, essay=False, reason=""),
    FieldAnswer(id="q5", answer="", basis=[], skip=False, essay=True, reason="prose"),
    FieldAnswer(id="q6", answer="3.9", basis=[], skip=False, essay=False, reason="guess"),
    FieldAnswer(id="q99", answer="x", basis=["personal.phone"], skip=False, essay=False, reason="unknown id"),
], _qs, _pl)
check("rails: the candidate's phone never answers an emergency-contact question",
      _plan[_qs[0]["key"]].answer is None)
_ref = [
    {"qid": "r1", "key": planner.question_key("Are you being referred to this role as part of the Campus Referral Program?", "select"),
     "label": "Are you being referred to this role as part of the Campus Referral Program?", "section": "", "widget": "select",
     "options": ["Yes", "No"], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "r2", "key": planner.question_key("What is the full name of your referrer?", "text"),
     "label": "What is the full name of your referrer?", "section": "", "widget": "text",
     "options": [], "required": False, "maxlength": None, "hint": "", "bank": ""},
]
_ref_plan = planner.rails([
    FieldAnswer(id="r1", answer="No", basis=["about.applied_here_before"], skip=False, essay=False, reason="not referred"),
    FieldAnswer(id="r2", answer="A B", basis=["personal.name"], skip=False, essay=False, reason="name"),
], _ref, _pl)
check("rails: 'are you being referred?' is about the candidate and keeps its No", _ref_plan[_ref[0]["key"]].answer == "No")
check("rails: the referrer's name is still about someone else", _ref_plan[_ref[1]["key"]].answer is None)
check("amount: a range becomes its midpoint", _as_amount("$30-40 per hour") == "35")
check("amount: thousands separators", _as_amount("$85,000 per year") == "85000")
check("amount: k suffix", _as_amount("40k") == "40000")
check("amount: no number -> None", _as_amount("negotiable") is None)
from datetime import datetime as _dt
check("boilerplate: 'Date — Year (YYYY)' is this year", _autofill_boilerplate("Date — Year (YYYY)*", {"type": "text", "required": True}, []) == _dt.now().strftime("%Y"))
check("boilerplate: 'Date - Month (MM)' is this month", _autofill_boilerplate("Date - Month (MM)", {"type": "text", "required": True}, []) == _dt.now().strftime("%m"))
check("boilerplate: 'Start date — Month' is not today", _autofill_boilerplate("From date — Month (MM)*", {"type": "text", "required": True}, []) is None)
from resume_tailor.apply import closest_option
check("closest: the list's shorter wording", closest_option("Computer Science and Mathematics", ["Chemistry", "Computer Engineering", "Computer Science", "Mathematics"]) == 2)
check("closest: place written out vs abbreviated", closest_option("Durham, NC, United States", ["Durham, NC, US", "Durham, NH, US", "Durham, CA, US"]) == 0)
check("closest: a tie stays open", closest_option("Durham", ["Durham, NC, US", "Durham, NH, US"]) is None)
check("closest: nothing in common stays open", closest_option("Durham, NC, United States", ["San Francisco", "New York", "Remote"]) is None)
check("closest: a lone stray word is not a match", closest_option("Bachelor of Science in Computer Science", ["Science Teacher", "Nursing"]) is None)
_opt = [
    {"qid": "o1", "key": planner.question_key("Field of Study", "select"), "label": "Field of Study", "section": "", "widget": "select",
     "options": ["Chemistry", "Computer Engineering", "Computer Science", "Mathematics"], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "o2", "key": planner.question_key("Where are you located?", "select"), "label": "Where are you located?", "section": "", "widget": "select",
     "options": ["San Francisco Bay Area", "New York", "Remote (US)"], "required": True, "maxlength": None, "hint": "", "bank": ""},
]
_opt_plan = planner.rails([
    FieldAnswer(id="o1", answer="Computer Science and Mathematics", basis=["work_preferences.open_to_relocation"], skip=False, essay=False, reason="major"),
    FieldAnswer(id="o2", answer="Durham, NC, United States", basis=["work_preferences.open_to_relocation"], skip=False, essay=False, reason="home"),
], _opt, _pl)
check("rails: an answer outside the list lands on the entry that means it", _opt_plan[_opt[0]["key"]].answer == "Computer Science")
check("rails: an answer with nothing in the list stays open for the repair pass", _opt_plan[_opt[1]["key"]].answer is None)
check("rails: the candidate's phone does answer 'Phone'", _plan[_qs[1]["key"]].answer == "+1 555-0100")
check("rails: a whole-word option match is normalised to the option's exact text",
      _plan[_qs[2]["key"]].answer == "Charlottesville, VA")
check("rails: select-all-that-apply keeps every matched option", _plan[_qs[3]["key"]].answer in ("Python | Rust",) or _plan[_qs[3]["key"]].answer is None)
check("rails: an essay is flagged, not answered", _plan[_qs[4]["key"]].essay and _plan[_qs[4]["key"]].answer is None)
check("rails: an answer resting on nothing in the profile is dropped", _plan[_qs[5]["key"]].answer is None)
_sil = [
    {"qid": "q1", "key": planner.question_key("Have you ever been employed by TELUS or any subsidiary?", "yesno"),
     "label": "Have you ever been employed by TELUS or any subsidiary?", "section": "", "widget": "yesno",
     "options": ["Yes", "No"], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q2", "key": planner.question_key("Active Security Clearance(s)", "select"),
     "label": "Active Security Clearance(s)", "section": "", "widget": "select",
     "options": ["None", "Secret", "Top Secret"], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q3", "key": planner.question_key("Have you ever been employed by TELUS?", "radio"),
     "label": "Have you ever been employed by TELUS?", "section": "", "widget": "radio",
     "options": ["Yes", "No"], "required": True, "maxlength": None, "hint": "", "bank": ""},
]
_sil_plan = planner.rails([
    FieldAnswer(id="q1", answer="No", basis=[], skip=False, essay=False, reason="nothing in the record"),
    FieldAnswer(id="q2", answer="None", basis=[], skip=False, essay=False, reason="no clearance on file"),
    FieldAnswer(id="q3", answer="Yes", basis=[], skip=False, essay=False, reason="?"),
], _sil, _pl)
check("rails: 'No' to a past-employment question stands on the record's silence",
      _sil_plan[_sil[0]["key"]].answer == "No")
check("rails: 'None' for clearances stands on the record's silence", _sil_plan[_sil[1]["key"]].answer == "None")
_puz = [
    {"qid": "q1", "key": planner.question_key("My car is dirty; the car wash is 2 blocks away. Should I walk or drive?", "text"),
     "label": "My car is dirty; the car wash is 2 blocks away. Should I walk or drive?", "section": "", "widget": "text",
     "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q2", "key": planner.question_key("What's the most interesting paper you've read this month?", "text"),
     "label": "What's the most interesting paper you've read this month?", "section": "", "widget": "text",
     "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
]
_puz_plan = planner.rails([
    FieldAnswer(id="q1", answer="Drive — the car has to be at the car wash.", basis=["reasoning"], skip=False, essay=False, reason="puzzle"),
    FieldAnswer(id="q2", answer="Attention Is All You Need", basis=["reasoning"], skip=False, essay=False, reason="?"),
], _puz, _pl)
check("rails: a puzzle about nobody in particular is answered by reasoning",
      _puz_plan[_puz[0]["key"]].answer == "Drive — the car has to be at the car wash.")
check("rails: 'reasoning' cannot answer a question about the candidate's own reading",
      _puz_plan[_puz[1]["key"]].answer is None)
check("rails: a 'Yes' with no basis is still dropped", _sil_plan[_sil[2]["key"]].answer is None)
check("rails: an unknown question id is ignored", len(_plan) == 6)
check("bank guard: 'Employer name' does not get the candidate's name",
      _bank(_pl, "Employer name")[0] is None and _bank(_pl, "Full Name")[0] == "A B")
check("bank strong: 'Location (City)' is a strong hit, a long sentence containing 'city' is not",
      _bank(_pl, "Location (City)*", strong=True)[0] == "Durham"
      and _bank(_pl, "Please list the city and state/province that you are located in today.", strong=True)[0] is None)

_dup = [
    {"qid": "q1", "key": planner.question_key("End date", "text", "Education"), "label": "End date", "section": "Education",
     "widget": "text", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
    {"qid": "q2", "key": planner.question_key("End date", "text", "Work History"), "label": "End date", "section": "Work History",
     "widget": "text", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""},
]
_dup_plan = planner.rails([
    FieldAnswer(id="q1", answer="May 2028", basis=["address.city"], skip=False, essay=False, reason=""),
    FieldAnswer(id="q2", answer="Aug 2025", basis=["address.city"], skip=False, essay=False, reason=""),
], _dup, _pl)
check("rails: the same label under two headings is two questions",
      _dup_plan[_dup[0]["key"]].answer == "May 2028" and _dup_plan[_dup[1]["key"]].answer == "Aug 2025")

from resume_tailor.apply import ApplySession as _AS
_opts = [{"i": 0, "t": "Durham, United Kingdom"}, {"i": 1, "t": "Durham, North Carolina, United States"}, {"i": 2, "t": "Raleigh, North Carolina, United States"}]
check("picker: 'Durham, NC' finds 'Durham, North Carolina, United States'", _AS._match_option("Durham, NC", _opts) == 1)
check("picker: 'Durham' alone is ambiguous between two Durhams", _AS._match_option("Durham", _opts) is None)
_fos = [{"i": 0, "t": "Computer Engineering"}, {"i": 1, "t": "Computer Science"}, {"i": 2, "t": "Mathematics"}, {"i": 3, "t": "Chemistry"}]
check("picker: Intel's Field of Study — the profile's major lands on the list's 'Computer Science'",
      _AS._match_option("Computer Science and Mathematics", _fos, loose_prefix="Computer") == 1)
check("picker: a bare major with one exact entry", _AS._match_option("Mathematics", _fos) == 2)
_terms = [{"i": 0, "t": "Winter 2026"}, {"i": 1, "t": "Spring 2027"}, {"i": 2, "t": "Summer 2027"}, {"i": 3, "t": "Spring 2028"}, {"i": 4, "t": "Summer 2028"}]
check("picker: 'May 2028' on a term list is 'Spring 2028'", _AS._match_option("May 2028", _terms) == 3)
check("picker: 'December 2027' on a term list is 'Winter 2027' when offered, else open", _AS._match_option("December 2027", _terms) is None)
from resume_tailor.batch import _SEARCH_PICKER
check("search picker: 'Where are you located?' searches as you type — its opening suggestions are not a list",
      bool(_SEARCH_PICKER.search("Where are you located?")))
check("search picker: 'Field of Study' is a fixed list", not _SEARCH_PICKER.search("Field of Study"))
from resume_tailor.batch import _group
_two_consents = [
    {"id": "c1", "type": "checkbox", "group": "Yes", "option_label": "Yes", "label": "I certify the information provided is true.", "section": "Application Questions", "required": True},
    {"id": "c2", "type": "checkbox", "group": "Yes", "option_label": "Yes", "label": "I understand my application will be processed under the Candidate Privacy Policy.", "section": "Application Questions", "required": True},
]
_g, _grouped = _group(_two_consents)
check("group: two lone consent boxes named 'Yes' under two statements are two questions", not _grouped)
_one_list = [
    {"id": "l1", "type": "checkbox", "group": "langs", "option_label": "Python", "label": "Languages you know", "section": "", "required": False},
    {"id": "l2", "type": "checkbox", "group": "langs", "option_label": "Go", "label": "Languages you know", "section": "", "required": False},
]
_g2, _grouped2 = _group(_one_list)
check("group: boxes sharing a name and a question are one list", _grouped2 == {"l1", "l2"})
from resume_tailor.apply import _relabel_greenhouse
_gh = [{"id": "a", "dom_id": "company-name-0", "label": "Company name*", "section": "Phone", "type": "text"},
       {"id": "b", "dom_id": "end-date-year-0", "label": "End date year*", "section": "Phone", "type": "text"},
       {"id": "c", "dom_id": "current-role-0_1", "name": "current-role-0", "label": "Employment", "section": "Phone", "type": "checkbox"},
       {"id": "d", "dom_id": "school--0", "label": "School*", "section": "Phone", "type": "text"},
       {"id": "e", "dom_id": "question_68958854", "label": "Are you at least 18?*", "section": "Phone", "type": "text"}]
_relabel_greenhouse(_gh)
check("greenhouse: employment fields get a Work Experience section", _gh[0]["section"] == "Work Experience 1" and _gh[1]["section"] == "Work Experience 1")
check("greenhouse: the current-role box is named", _gh[2]["label"] == "I currently work here" and _gh[2]["section"] == "Work Experience 1")
check("greenhouse: education fields get an Education section", _gh[3]["section"] == "Education 1")
check("greenhouse: other questions untouched", _gh[4]["section"] == "Phone")
from resume_tailor.batch import _bank as _bank_fn
check("bank: under an Education section the school answers", _bank_fn(_bank3, "University", {"section": "Education 1", "type": "text"}, [])[0] == "Duke University")
check("bank: under a Work Experience section the bank says nothing", _bank_fn(_bank3, "University", {"section": "Work Experience 1", "type": "text"}, [])[0] is None)
check("boilerplate: a lone required box on a disability form is never ticked as an acknowledgement",
      _autofill_boilerplate("Please check one of the boxes below:*", {"type": "checkbox", "required": True, "option_label": "Yes, I have a disability, or have had one in the past", "section": "Voluntary Self-Identification of Disability"}, []) is None)
check("boilerplate: a lone required box under a plain statement is still an acknowledgement",
      _autofill_boilerplate("Your application will be reviewed for one position at a time", {"type": "checkbox", "required": True, "option_label": "", "section": ""}, []) == "yes")
check("boilerplate: a disability checkbox group takes the decline option",
      _autofill_boilerplate("Please check one of the boxes below:*", {"type": "checkbox", "required": True, "section": "Voluntary Self-Identification of Disability"},
                            ["Yes, I have a disability, or have had one in the past", "No, I do not have a disability and have not had one in the past", "I do not want to answer"]) == "I do not want to answer")
check("boilerplate: conference attendance -> the none option",
      _autofill_boilerplate("Indicate your planned attendance at the listed conference and/or your affiliations with the below groups (check all that apply):", {"type": "checkbox", "required": True}, ["Grace Hopper Celebration", "NSBE", "None of the above"]) == "None of the above")
check("boilerplate: socio-economic question -> prefer not to say",
      _autofill_boilerplate("What is the highest level of education completed by either of your parents?", {"type": "select", "required": True}, ["Doctorate", "Bachelor's", "Prefer not to say"]) == "Prefer not to say")
check("boilerplate: language skill in a language not on the record -> none",
      _autofill_boilerplate("Please indicate your language skills in reading, written and spoken Japanese", {"type": "select", "required": True}, ["None", "Basic", "Fluent"]) == "None")
check("boilerplate: language skill in English is not answered by the none rule",
      _autofill_boilerplate("Please indicate your language skills in reading, written and spoken English", {"type": "select", "required": True}, ["None", "Basic", "Fluent"]) is None)
from resume_tailor import ats as _ats
check("ats: TikTok, EY Yello, Goldman and Apple are account-gated", all(_ats.host_kind(u) in _ats.NEEDS_ACCOUNT for u in ("https://lifeattiktok.com/search/1", "https://eyglobal.yello.co/jobs/2", "https://higher.gs.com/roles/3", "https://jobs.apple.com/en-us/details/4")))
_post = [{"qid": "p1", "key": planner.question_key("Is the position you are applying to within the state of Maryland?", "select"), "label": "Is the position you are applying to within the state of Maryland?",
          "section": "", "widget": "select", "options": ["Yes", "No"], "required": True, "maxlength": None, "hint": "", "bank": ""}]
_post_plan = planner.rails([FieldAnswer(id="p1", answer="No", basis=["posting"], skip=False, essay=False, reason="the posting is in Pittsburgh")], _post, _pl)
check("rails: a question about the posting is answered from the posting", _post_plan[_post[0]["key"]].answer == "No")
from resume_tailor.profile import sponsorship_answer
_spon = _P(career={"personal_information": {"name": "A", "surname": "B"}},
           answers={"work_authorization": {"requires_us_sponsorship": "No", "us_work_authorization": "Yes", "citizenship_status": "U.S. lawful permanent resident (green card holder)"}}, root=Path("/tmp"))
check("sponsorship: a status picker takes the candidate's own standing",
      sponsorship_answer(_spon, "Employment eligibility status*", ["U.S. Citizen", "Permanent Resident", "Yes, will require firm sponsorship", "No, will not require firm sponsorship"]) == "Permanent Resident")
check("sponsorship: a yes/no sponsorship question is No",
      sponsorship_answer(_spon, "Will you now or in the future require immigration sponsorship by our company?", ["Yes", "No"]) == "No")
check("sponsorship: the option worded 'without sponsorship' is chosen",
      sponsorship_answer(_spon, "Work authorization", ["Will require sponsorship now or in the future", "Authorized to work without sponsorship"]) == "Authorized to work without sponsorship")
check("sponsorship: 'will require' is never chosen for a permanent resident",
      sponsorship_answer(_spon, "Employment eligibility status", ["Yes, will require firm sponsorship", "Maybe"]) is None)
check("sponsorship: an unrelated question is left alone", sponsorship_answer(_spon, "Are you at least 18?", ["Yes", "No"]) is None)
_sq = [{"qid": "s1", "key": planner.question_key("Employment eligibility status", "select"), "label": "Employment eligibility status", "section": "", "widget": "select",
        "options": ["U.S. Citizen", "Permanent Resident", "Yes, will require firm sponsorship"], "required": True, "maxlength": None, "hint": "", "bank": ""}]
_sq_plan = planner.rails([FieldAnswer(id="s1", answer="Yes, will require firm sponsorship", basis=["work_authorization.us_work_authorization"], skip=False, essay=False, reason="legally allowed")], _sq, _spon)
check("rails: the model's 'will require sponsorship' is replaced by the bank's answer", _sq_plan[_sq[0]["key"]].answer == "Permanent Resident")
from resume_tailor.compose import education as _edu_html
check("compose: a GPA on the record is rendered", "GPA 3.42/4.0" in _edu_html({"education_details": [{"education_level": "B.S.", "field_of_study": "CS", "institution": "Duke", "year_of_completion": "2028", "gpa": "3.42/4.0"}]}))
check("compose: no GPA field, no GPA line", "GPA" not in _edu_html({"education_details": [{"education_level": "B.S.", "institution": "Duke"}]}))
from resume_tailor.profile import gpa_answer
_gp = _P(career={"personal_information": {"name": "A", "surname": "B"}}, answers={"education": {"gpa": "3.42"}}, root=Path("/tmp"))
check("gpa: a text field gets the exact number", gpa_answer(_gp, "What is your GPA?", []) == "3.42")
check("gpa: the band that holds 3.42", gpa_answer(_gp, "What is your overall college/university GPA?", ["3.0 - 3.49", "3.5 - 3.9", "4.0"]) == "3.0 - 3.49")
check("gpa: 'or higher' bands take the highest floor under it", gpa_answer(_gp, "GPA", ["3.0 or higher", "3.5 or higher", "3.8 or higher"]) == "3.0 or higher")
check("gpa: 'out of 4.0' points never overstate", gpa_answer(_gp, "Overall GPA", ["3.2 out of 4.0", "3.4 out of 4.0", "3.5 out of 4.0"]) == "3.4 out of 4.0")
check("gpa: an unrelated question is untouched", gpa_answer(_gp, "Years of experience", ["0-1", "2+"]) is None)
_gq = [{"qid": "g1", "key": planner.question_key("What is your GPA?", "text"), "label": "What is your GPA?", "section": "", "widget": "text", "options": [], "required": True, "maxlength": None, "hint": "", "bank": ""}]
_gq_plan = planner.rails([FieldAnswer(id="g1", answer="3.5", basis=["education.gpa"], skip=False, essay=False, reason="rounded")], _gq, _gp)
check("rails: a rounded GPA is replaced by the record's", _gq_plan[_gq[0]["key"]].answer == "3.42")
_tv = _P(career={"personal_information": {"name": "A", "surname": "B"}},
         answers={"work_authorization": {"requires_us_sponsorship": "No", "citizenship_status": "U.S. lawful permanent resident (green card holder)"}}, root=Path("/tmp"))
check("sponsorship: a visa-holder's option is never chosen for a permanent resident",
      sponsorship_answer(_tv, "Will you require our Company to provide immigration sponsorship?", ["Yes", "No – I hold a temporary visa status that provides work authorization and will not need the Company to sponsor", "No – I am a U.S. citizen or permanent resident"]) == "No – I am a U.S. citizen or permanent resident")
from resume_tailor.apply import _submit_verdict, _date_text as _dt
check("submit: Oracle's saved-draft page is not a submission",
      _submit_verdict({"form_present": False, "text": "Thank you. We saved a draft of your job application. We invite you to complete and submit it.", "errors": []}, True)[0] is False)
check("date: a Workday month box gets the month", _dt("May 2028", "MM", "text") == "05")
check("date: a Workday day box never gets a stray 2", _dt("May 2028", "DD", "text") == "01")
check("date: a Workday year box gets the year", _dt("May 2028", "YYYY", "text") == "2028")
check("picker: 'Raleigh, NC' finds Raleigh", _AS._match_option("Raleigh, NC", _opts) == 2)
_edu = _P(career={"personal_information": {"name": "A", "surname": "B"}},
          answers={"availability": {"earliest_start_date": "May 2027"},
                   "education": {"start_date_year": "2024", "graduation_year": "2028"}}, root=Path("/tmp"))
check("bank: 'Start date year' under an Education heading is the education start, not the job start",
      _bank(_edu, "Start date year*", {"type": "number", "section": "Education"})[0] == "2024")
check("bank: 'Start date' with no heading is ambiguous (job or education) and left to the planner",
      _bank(_edu, "Start date", {"type": "text", "section": ""})[0] is None)
check("bank: 'What is the earliest date you are available to start?' is the job start",
      _bank(_edu, "What is the earliest date you are available to start this position?", {"type": "text", "section": ""})[0] == "May 2027")
_edu2 = _P(career={"personal_information": {"name": "A", "surname": "B"},
                   "education_details": [{"institution": "Duke University", "education_level": "B.S.",
                                          "field_of_study": "CS", "start_date": "08/2024", "year_of_completion": "2028 (expected)"}]},
           answers={}, root=Path("/tmp"))
check("education start date is derived from the career record (month, year, and as written)",
      _edu2.flat_answers().get("education.start_date_year") == "2024"
      and _edu2.flat_answers().get("education.start_date_month") == "August"
      and _edu2.flat_answers().get("education.start_date") == "08/2024")
check("bank: under a Work History heading the bank stays silent",
      _bank(_edu, "Start date", {"type": "text", "section": "Work History"})[0] is None)
check("consent as a Yes/No choice: 'Terms & Conditions' -> Yes",
      _autofill_boilerplate("Terms & Conditions*", {"type": "radio", "tag": "input"}, ["Yes", "No"]) == "Yes")
import re as _re
check("a bare 'Date' field (a signature line) gets today's date, US style",
      bool(_re.fullmatch(r"\d{2}/\d{2}/\d{4}", _autofill_boilerplate("Date ✱", {"type": "text", "tag": "input", "required": True}) or "")))
check("'Start date' or 'Graduation date' are NOT today's date",
      _autofill_boilerplate("Start date", {"type": "text", "tag": "input", "required": True}) is None
      and _autofill_boilerplate("Graduation date", {"type": "text", "tag": "input"}) is None)
check("a required lone checkbox under a statement is an acknowledgement box",
      _autofill_boilerplate("Your application will be reviewed for one position at a time. If you're interested in multiple roles, please apply to your top choice.",
                            {"type": "checkbox", "tag": "input", "required": True}) == "yes")
check("an optional lone checkbox under a statement is left alone",
      _autofill_boilerplate("Subscribe me to the newsletter", {"type": "checkbox", "tag": "input", "required": False}) is None)
check("a required lone checkbox asking a real question is not auto-ticked",
      _autofill_boilerplate("Do you have a disability?", {"type": "checkbox", "tag": "input", "required": True}) is None)
check("consent: an 'Interview Code of Conduct' acknowledgement is agreed to",
      _autofill_boilerplate("Interview Code of Conduct *", {"type": "radio", "tag": "input"}, ["I agree", "I do not agree"]) == "I agree"
      and _autofill_boilerplate("Interview Code of Conduct *", {"type": "checkbox", "tag": "input"}) == "yes")
check("consent as a checkbox list: the 'I agree' option",
      _autofill_boilerplate("Privacy policy consent", {"type": "checkbox", "tag": "input"}, ["I agree", "I decline"]) == "I agree")
from resume_tailor.apply import _pick_option as _po
check("option: 'Yes' against two 'Yes, ...' options is not decided by list order",
      _po("Yes", [{"label": "Yes, no restrictions"}, {"label": "Yes, with time limitations"}, {"label": "No"}]) is None)
check("option: 'Yes' against one 'Yes, ...' option still lands",
      _po("Yes", [{"label": "Yes, I am authorized"}, {"label": "No"}]) == 0)
check("third-party guard: 'Preferred First Name' is about the candidate",
      not planner.THIRD_PARTY.search("Preferred First Name") and bool(planner.THIRD_PARTY.search("Referrer's name")))

# --- dates in the form a field wants ------------------------------------------------------
check("date: 'May 2028' -> ISO for a date input", _date_text("May 2028", "", "date") == "2028-05-01")
check("date: 'May 2028' -> MM/DD/YYYY when the hint says so", _date_text("May 2028", "Format: MM/DD/YYYY", "text") == "05/01/2028")
check("date: '05/01/2028' -> a month input", _date_text("05/01/2028", "", "month") == "2028-05")
check("date: 'May 1, 2028' parses with a day", _parse_date("May 1, 2028") == (2028, 5, 1))
check("date: a non-date is left alone", _date_text("Duke University", "MM/DD/YYYY", "text") == "Duke University")
check("date: no format hint, text field -> left alone", _date_text("May 2028", "", "text") == "May 2028")

# --- multi-step forms ---------------------------------------------------------------------
check("next: a lone 'Continue' is the way forward",
      (_pick_next_button([{"text": "Continue", "disabled": False, "selector": "#n"},
                          {"text": "Cancel", "disabled": False, "selector": "#c"}]) or {}).get("selector") == "#n")
check("next: 'Next' and 'Review application' together are ambiguous",
      _pick_next_button([{"text": "Next", "disabled": False, "selector": "#a"},
                         {"text": "Review application", "disabled": False, "selector": "#b"}]) is None)
check("widget: a react-select input is a picker; a yes/no pair is yesno",
      planner.widget_of({"tag": "input", "type": "text", "combobox": True}) == "picker"
      and planner.widget_of({"tag": "yesno", "type": "yesno"}) == "yesno"
      and planner.widget_of({"tag": "input", "type": "checkbox"}, "checkbox") == "checkboxes")

# --- a bullet is one sentence, starting with a capital ------------------------------------
from resume_tailor.compose import bullet_text
from resume_tailor.models import Bullet as _B
def _mk(action, outcome=""):
    return _B(action=action, outcome=outcome, source_fact_ids=["exp0.r0"], numerals_used=[], derived_numerals=[])
check("bullet: an empty action with an outcome renders the outcome as the sentence — not ', won…'",
      bullet_text(_mk("", "Won the ACM HotMobile 2026 Best Demo Award for detecting AR smart glasses")) ==
      "Won the ACM HotMobile 2026 Best Demo Award for detecting AR smart glasses")
check("bullet: action + result clause join with a comma, result lowercased",
      bullet_text(_mk("Built DukeGPT, a campus AI assistant", "Served 2,000+ monthly requests")) ==
      "Built DukeGPT, a campus AI assistant, served 2,000+ monthly requests")
check("bullet: a prepositional tail joins with a space",
      bullet_text(_mk("Deployed Kubernetes pods running backend services", "on Yandex Cloud")) ==
      "Deployed Kubernetes pods running backend services on Yandex Cloud")
check("bullet: leading punctuation and lowercase are cleaned up",
      bullet_text(_mk(", detected AR smart glasses during exams.", "")) == "Detected AR smart glasses during exams")
check("bullet: nothing in, nothing out", bullet_text(_mk("", "")) == "")
from resume_tailor.render import _wrap, PDF_OPTIONS
check("render: a compact notch lands on the body tag",
      'class="compact-1"' in _wrap("<p>x</p>", "", "t", compact=1) and 'class="compact' not in _wrap("<p>x</p>", "", "t"))
check("render: a body-wrapped document also takes the notch",
      '<body class="compact-2">' in _wrap("<body><p>x</p></body>", "", "t", compact=2))
check("render: US Letter page size", PDF_OPTIONS["format"] == "Letter")

# --- checkbox lists: one question, several boxes ------------------------------------------
from resume_tailor.batch import _group
_hadrian = [
    {"id": "rt-28", "type": "checkbox", "label": "Which term(s) are you interested in? Select all that apply.", "option_label": "Fall 2026", "group": "Fall 2026", "section": ""},
    {"id": "rt-29", "type": "checkbox", "label": "Which term(s) are you interested in? Select all that apply.", "option_label": "Spring 2027", "group": "Spring 2027", "section": ""},
    {"id": "rt-30", "type": "checkbox", "label": "Which term(s) are you interested in? Select all that apply.", "option_label": "Summer 2027", "group": "Summer 2027", "section": ""},
    {"id": "rt-31", "type": "checkbox", "label": "I agree to the privacy policy", "option_label": "I agree", "group": "I agree", "section": ""},
    {"id": "rt-40", "type": "checkbox", "label": "How did you hear?", "option_label": "LinkedIn", "group": "q1[]", "section": ""},
    {"id": "rt-41", "type": "checkbox", "label": "How did you hear?", "option_label": "Other", "group": "q1[]", "section": ""},
]
_g, _grouped = _group(_hadrian)
check("checkbox list: Ashby's differently-named boxes under one question form one group of three",
      any(len(ms) == 3 and {m["option_label"] for m in ms} == {"Fall 2026", "Spring 2027", "Summer 2027"} for ms in _g.values()))
check("checkbox list: a lone consent box stays a lone box", "rt-31" not in _grouped)
check("checkbox list: same-name boxes still group (Greenhouse)", any({m["id"] for m in ms} == {"rt-40", "rt-41"} for ms in _g.values()))

# --- what a rejected submit asks for -----------------------------------------------------
from resume_tailor.batch import _missing_labels, _demanded
_ashby_errs = ["Your form needs corrections Missing entry for required field: Which Sierra office would you prefer to work from? | "
               "Missing entry for required field: Are you legally authorized to work in the US?"]
_named = _missing_labels(_ashby_errs)
check("rejection: Ashby's 'missing entry for required field' names both questions",
      _named == {"which sierra office would you prefer to work from?", "are you legally authorized to work in the us?"}, _named)
check("rejection: 'Phone is required' names Phone", _missing_labels(["Phone is required."]) == {"phone"})
check("rejection: a named field matches its label, with or without the asterisk",
      _demanded("Which Sierra office would you prefer to work from? *", _named) and not _demanded("Email", _named))

# --- what kind of page this is ---------------------------------------------------------
from resume_tailor.apply import blocker_verdict
_form = [{"type": "text"}, {"type": "email"}, {"type": "file"}, {"type": "text"}]
check("blocker: a posting's scam warning ('please verify all openings...') above a real form is not a wall",
      blocker_verdict(_form, "We never ask for payment. For your peace of mind, please verify all openings on our careers page.") is None)
check("blocker: a reCAPTCHA footer badge under a real form is not a wall",
      blocker_verdict(_form, "Submit application. This site is protected by reCAPTCHA and the Google Privacy Policy.") is None)
check("blocker: 'please verify' on a page with no form is a wall",
      blocker_verdict([], "Please verify you are a human to continue.") == "bot_check")
check("blocker: 'verify you are human' is a wall even with fields present",
      blocker_verdict(_form, "Verify you are human by completing the action below.") == "bot_check")
check("blocker: a password field without a file input is a login wall",
      blocker_verdict([{"type": "email"}, {"type": "password"}], "Sign in to continue") == "login_required")
check("blocker: no fields at all is no form", blocker_verdict([], "Loading...") == "no_form_found")
check("blocker: Workday's already-applied page, signed in, is not a missing form",
      blocker_verdict([], "Software Engineering Intern - Summer 2027 You've already applied for this job. View My Applications",
                      "https://x.wd5.myworkdayjobs.com/en-US/careers/job/Chicago/SWE-Intern", after_apply=True) == "already_applied")
check("blocker: Google's account sign-in page is a login wall even with only an email box",
      blocker_verdict([{"type": "email"}], "Sign in to continue to Google Careers",
                      "https://accounts.google.com/v3/signin/identifier?continue=x") == "login_required")
check("blocker: a /login path with a couple of fields is a login wall",
      blocker_verdict([{"type": "text"}, {"type": "password"}], "Welcome back", "https://jobs.example.com/login") == "login_required")
check("blocker: a real form on a normal address is not a login wall",
      blocker_verdict(_form, "Apply for this job", "https://job-boards.greenhouse.io/x/jobs/1") is None)

# --- after the submit click: a refusal is read before any sign of success ------------
from resume_tailor.apply import _FAILURE_TEXT, _SUCCESS_TEXT
_ashby_refusal = ("We couldn't submit your application. Your application submission was flagged as possible spam. "
                  "If you believe this was a mistake, please submit your application again.")
check("submit: Ashby's spam banner reads as a refusal", bool(_FAILURE_TEXT.search(_ashby_refusal)))
check("submit: a refusal page is not mistaken for success", not _SUCCESS_TEXT.search(_ashby_refusal))
check("submit: a confirmation reads as success",
      bool(_SUCCESS_TEXT.search("Thank you for applying to Whatnot! Your application has been submitted."))
      and not _FAILURE_TEXT.search("Thank you for applying to Whatnot! Your application has been submitted."))
check("submit: a posting that merely mentions 'try again' in passing is not a refusal",
      not _FAILURE_TEXT.search("If the page does not load, try again later."))
from resume_tailor.apply import _submit_verdict
check("submit: a busy button with the form still up means keep waiting",
      _submit_verdict({"form_present": True, "busy": True, "text": "Submit application", "errors": []}, False) is None)
check("submit: the form still up, button idle, errors shown -> not submitted, with the errors",
      _submit_verdict({"form_present": True, "busy": False, "text": "x", "errors": ["Phone is required"]}, False)
      == (False, "submit was clicked but the form is still on the page; it says: Phone is required"))
check("submit: the form gone on a new page -> submitted",
      _submit_verdict({"form_present": False, "busy": False, "text": "Thanks", "errors": []}, True) == (True, "form gone after submit, new page"))
check("submit: a refusal banner wins even while the button looks busy",
      _submit_verdict({"form_present": False, "busy": True, "text": _ashby_refusal, "errors": []}, False)[0] is False)
check("submit: a confirmation wins even with the form still present",
      _submit_verdict({"form_present": True, "busy": False, "text": "Thank you for applying! Your application has been submitted.", "errors": []}, False)
      == (True, "confirmation shown"))

# --- yes/no widgets and pickers as the scanner reports them --------------------------
from resume_tailor.apply import _is_empty
check("a required yes/no widget with nothing pressed is empty",
      _is_empty({"type": "yesno", "tag": "yesno", "required": True, "value": "", "options": ["Yes", "No"]}))
check("a yes/no widget with a button pressed is answered",
      not _is_empty({"type": "yesno", "tag": "yesno", "required": True, "value": "No", "options": ["Yes", "No"]}))
check("essay detection: a yes/no widget is not an essay", not _is_essay({"tag": "yesno", "type": "yesno", "label": "x" * 80}))

# --- 2026-09-03: reaching the form, pickers, walls -------------------------
from resume_tailor.apply import ApplySession, _APPLY_EXCLUDE, _APPLY_TEXT, _looks_like_application
from resume_tailor.batch import _document_prompt
from resume_tailor.mailbox import extract_code
from resume_tailor.planner import _NO_LIKE, _SILENCE

_alert = [{"type": "email", "label": "Email"}, {"type": "text", "label": "Keywords"}, {"type": "number", "label": "Select how often (in days)"}]
check("looks-like: a job-alert form is not an application", not _looks_like_application(_alert))
check("looks-like: name plus email is", _looks_like_application([{"type": "text", "label": "First Name*"}, {"type": "email", "label": "Email*"}]))
check("looks-like: a résumé upload settles it", _looks_like_application([{"type": "file", "label": "Resume"}]))
check("blocker: an alert form on a job page reads as no form", blocker_verdict(_alert, "Create alert. Apply now", "https://careers.x.com/job/1") == "no_form_found")
check("blocker: after Apply, a sign-in page with no fields is a login wall",
      blocker_verdict([], "Sign in using Microsoft. Create an account", "https://apply.careers.x.com/apply?pid=1", after_apply=True) == "login_required")
check("blocker: after Apply, any fields are the form's own first step",
      blocker_verdict([{"type": "email", "label": "Email Address"}], "Get started with your email", "https://x.oraclecloud.com/apply/email", after_apply=True) is None)
check("blocker: a removed posting is gone, not a wall", blocker_verdict([], "The page you are looking for doesn't exist.", "https://x.myworkdayjobs.com/job/1") == "posting_gone")
check("blocker: Ashby's 'Job not found' is gone, not a review item", blocker_verdict([], "Job not found The job you requested was not found. View all open positions", "https://jobs.ashbyhq.com/replit/7e0d") == "posting_gone")
for text in ("Apply now »", "Apply Online", "Apply for this job online", "Apply to this job", "I’m interested", "APPLY NOW", "Apply Manually" if False else "Apply"):
    check(f"apply text: {text!r}", _APPLY_TEXT.match(text) is not None)
check("apply text: another site's autofill is excluded", _APPLY_EXCLUDE.search("Apply with LinkedIn") is not None)
_b = lambda **k: {"text": "", "disabled": False, "selector": "#x", "type": "", "in_form": False, "id": "b", **k}
check("submit: autofill buttons never count", _pick_submit_button([_b(text="Apply with LinkedIn"), _b(text="Apply", type="submit", in_form=True)])["text"] == "Apply")
check("submit: of two equal candidates the one inside the form wins",
      _pick_submit_button([_b(text="Apply Online"), _b(text="Apply Online", in_form=True)])["in_form"])
check("submit: save is not submit", _pick_submit_button([_b(text="Save"), _b(text="Submit")])["text"] == "Submit")
check("document: a 750-word prompt is an essay upload", _document_prompt("In an essay of about 750 words, please answer the following prompt: What is the hardest thing you have done?*") == "essay")
check("document: cover letter", _document_prompt("Cover Letter") == "cover letter")
check("document: a transcript is not written", _document_prompt("Academic Transcript*") is None)
check("boilerplate: an acknowledgement-only picker takes its one answer",
      _autofill_boilerplate("Reminder that you can only apply for one role.", {"required": True, "type": "text", "combobox": True}, ["I understand"]) == "I understand")
check("boilerplate: a yes/no question is not an acknowledgement",
      _autofill_boilerplate("Are you legally authorized to work?", {"required": True, "type": "text"}, ["Yes", "No"]) is None)
check("boilerplate: 'if someone referred you… first and last name' gets N/A when required",
      (_autofill_boilerplate("If someone referred you to this position, please provide their first and last name.", {"required": True, "type": "text"}, []) or "").startswith("N/A"))
check("picker: a bare score picks the tightest scale", ApplySession._match_option("1560", [{"t": "1560 out of 2400"}, {"t": "1560 out of 1600"}]) == 1)
check("picker: an ambiguous word stays ambiguous", ApplySession._match_option("Durham", [{"t": "Durham, NC"}, {"t": "Durham, NH"}]) is None)
check("mailbox: a one-time pass code is read", extract_code("Please confirm your identity using this one-time pass code: 756506") == "756506")
check("mailbox: a requisition number is not a code", extract_code("Thanks for applying! Requisition 253299") is None)
_GH = "Hi Sulaiman, Copy and paste this code into the security code field on your application: QwErTyUi After you enter the code, resubmit your application. © 2026 Greenhouse 18 West 18th Street"
check("mailbox: Greenhouse's letters-only code is read, not the footer year", extract_code(_GH) == "QwErTyUi")
check("mailbox: a plain word after 'code:' is not a code", extract_code("Use this code: Please enter it on the page. © 2026") is None)
check("mailbox: an Oracle six-digit code after a colon", extract_code("Your verification code: 483920. It expires in 10 minutes.") == "483920")
check("planner: a graduate GPA field answers by silence", bool(_SILENCE.search("GPA (Graduate)*")) and bool(_NO_LIKE.search("Other/Not Applicable")))

# --- e-mail-code walls wait for the inbox, company-wide -------------------

from resume_tailor import mailbox as _mailbox
from resume_tailor.batch import _inbox_wall
from resume_tailor.discover import _worn_out

_real_configured = _mailbox.configured
_mailbox.configured = lambda: False
try:
    with tempfile.TemporaryDirectory() as d:
        st = RunState.load(Path(d) / "state.json")
        wall = ("this site e-mailed a verification code and wants it typed in before the form — "
                "set RESUME_TAILOR_IMAP_PASSWORD (a Google app password) in ~/.resume-tailor/env and the tool will read it")
        st.record("amex-1", {"status": "needs_login", "company": "American Express", "detail": wall})
        st.record("amex-1", {"status": "needs_login", "company": "American Express", "detail": wall})
        st.record("bny-1", {"status": "needs_login", "company": "BNY",
                            "detail": "this site wants an account or a sign-in before its form"})
        amex1 = QueueEntry(id="amex-1", url="https://x/1", company_hint="American Express")
        amex2 = QueueEntry(id="amex-2", url="https://x/2", company_hint="American Express, Inc.")
        bny2 = QueueEntry(id="bny-2", url="https://x/3", company_hint="BNY")
        acme = QueueEntry(id="acme-1", url="https://x/4", company_hint="Acme")
        check("inbox wall: the posting that met the wall waits", _inbox_wall(st, amex1) == wall)
        check("inbox wall: another posting at the same company waits too, however the name is spelt",
              "RESUME_TAILOR_IMAP_PASSWORD" in (_inbox_wall(st, amex2) or ""), _inbox_wall(st, amex2))
        check("inbox wall: a plain sign-in wall does not park the company", _inbox_wall(st, bny2) is None)
        check("inbox wall: another company is not parked", _inbox_wall(st, acme) is None)
        check("inbox wall: a parked record is worn out while the inbox is unconfigured",
              _worn_out(st.done["amex-1"], inbox=False))
        check("inbox wall: ...and worth a try the moment it is, whatever its count",
              not _worn_out(dict(st.done["amex-1"], attempts=25), inbox=True))
        check("inbox wall: a wall met with the inbox configured counts as a real failure",
              _worn_out({"status": "needs_login", "attempts": 2,
                         "detail": "this site e-mailed a verification code — the inbox showed no code in time"}, inbox=True))
        check("inbox wall: a re-queue by hand is never worn out",
              not _worn_out(dict(st.done["amex-1"], detail="re-queued by hand"), inbox=False))
        st.record("amex-1", {"status": "needs_login", "company": "American Express",
                             "detail": "this site e-mailed a verification code — the inbox showed no code in time"})
        check("inbox wall: the first real attempt starts the count over",
              st.done["amex-1"]["attempts"] == 1, st.done["amex-1"]["attempts"])
        st.record("amex-3", {"status": "needs_login", "company": "American Express", "detail": "re-queued by hand"})
        check("inbox wall: a hand re-queue at a parked company is tried anyway",
              _inbox_wall(st, QueueEntry(id="amex-3", url="https://x/5", company_hint="American Express")) is None)
        _mailbox.configured = lambda: True
        check("inbox wall: nothing waits once the inbox is configured",
              _inbox_wall(st, amex2) is None and _inbox_wall(st, amex1) is None)
finally:
    _mailbox.configured = _real_configured

from resume_tailor.batch import _NETWORK_ERROR

check("network: a blank page takes the wait-and-retry path",
      bool(_NETWORK_ERROR.search("could not load the application form: the page came up blank or as a browser error")))
check("network: a real missing form does not", not _NETWORK_ERROR.search("no application form was found on this page"))

# --- résumé lint and the current employer ------------------------------------
from types import SimpleNamespace as _NS
from resume_tailor import gates as _gates
from resume_tailor.batch import _current_employer, _CURRENT_EMPLOYER
_lint_html = "<ul><li>Built a <b>note</b> platform: rich-text notes, LaTeX editing, and university community features.</li>" \
             "<li>, won the ACM HotMobile Best Demo Award</li><li>Go</li></ul>"
_lint_pdf = "Built a note platform: rich-text notes, LaTeX editing, and university comm\nwon the ACM HotMobile Best Demo Award"
_lv = _gates.check_bullets_survive(_lint_html, _lint_pdf)
check("lint: a bullet cut off in the PDF is flagged as clipped", any(v.kind == "clipped" for v in _lv))
check("lint: a bullet starting with a comma is flagged", any(v.kind == "leading-punctuation" for v in _lv))
check("lint: a two-letter item is ignored", not any(v.claim == "Go" for v in _lv))
_ok = _gates.check_bullets_survive("<li>Designed full-stack features across Next.js, PostgreSQL/Prisma &amp; the Gemini API.</li>",
                                   "Designed full-stack features across Next.js,\nPostgreSQL/Prisma & the Gemini API.")
check("lint: a bullet that survives line breaks and entities passes", _ok == [])
_prof = _NS(career={"experience_details": [
    {"position": "Software Engineer Intern", "company": "Qapps", "employment_period": "05/2026 - 06/2026"},
    {"position": "Undergraduate Teaching Assistant — CS 210", "company": "Duke University, Department of Computer Science", "employment_period": "08/2025 - Present"}]})
check("current employer: the ongoing role's organisation, not the department", _current_employer(_prof) == "Duke University")
check("current employer: none when no role is ongoing", _current_employer(_NS(career={"experience_details": [{"company": "X", "employment_period": "2024 - 2025"}]})) is None)
check("current employer: Lever's 'Current company' matches", bool(_CURRENT_EMPLOYER.search("Current company ✱")))
check("current employer: 'Company name' under a work entry does not", not _CURRENT_EMPLOYER.search("Company name*"))
check("current employer: 'current or most recent employer' matches", bool(_CURRENT_EMPLOYER.search("Please list your current or most recent employer")))
from resume_tailor.planner import _SILENCE as _SIL
check("silence: a broker licence the record does not list can be answered No", bool(_SIL.search("Do you intend to actively use a real estate or broker license that you possess?")))
check("silence: a FINRA licensing exam question can be answered No", bool(_SIL.search("Have you ever taken a FINRA or any other self-regulatory organization licensing exam?")))
check("silence: a driver's licence is never answered by silence", not _SIL.search("Do you possess a valid U.S. Driver's License?"))


width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
