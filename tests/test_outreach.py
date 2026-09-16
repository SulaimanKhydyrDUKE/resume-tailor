"""The outreach filters, offline: which inbox addresses are worth writing to,
which belong to the company at all, and what a drafted sentence may say.
No network, no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor.outreach import (NOREPLY, _about_company, _broker_page, _campus_links, _cited_broker, _harvest, _person, _role_label,
                                    _sentence_problems, _unligate, _writable, load_log, save_log)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


# --- writable: a mailbox a person or a recruiting team reads -----------------
for addr in ("talent@dvtrading.co", "dominique.burns@bti360.com", "careers@pdtpartners.com", "talent.scout@stryker.com",
             "hr.lplfinancial@lplfinancial.com", "campusus@imc.com", "recruitingservices@aoins.com"):
    check(f"writable: {addr}", _writable(addr))
for addr in ("69e25fca-1c38-4c0c-a41d-b29a0aa9e0e7@pinpoint.email", "do_not_reply@keybank.com", "hntb@default.com",
             "equifax-careers-n2yxkfylr7@talentcrm.equifax.com", "pluto@mail.talentpluto.com",
             "windowsinsiderprogram@e-mails.microsoft.com", "dellrecruiting@recruiting.dell.com",
             "hiltonrecruitment@hrsystem.hilton.com", "globalhrdefaultworkeremail@otis.com", "atwork.systems@allegion.com",
             "replies@microsoft.com", "u003e@anduril.com", "assessment@email.roblox.com", "opportunities@careeralerts.regions.com",
             "citizenscareers@orc.mycitizenshr.com", "x@hire.lever.co", "accessibility@mckesson.ca", "ethics@acme.com",
             "investorrelations@acme.com", "abc@", ""):
    check(f"not writable: {addr!r}", not _writable(addr))
check("a hyphenated first name is not a token", _writable("anna-katharina@example.com"))
check("a dotted name with digits is fine", _writable("j.smith2@example.com"))

# --- about the company: own domain, or plainly about an application ----------
check("own domain counts", _about_company("akshay@ambrook.com", "Follow-up from Ambrook", "Ambrook"))
check("own domain, name squashed", _about_company("talent@dvtrading.co", "", "DV Trading"))
check("own domain, name inside the root", _about_company("hrrecruitingassistant@directs.com", "", "Direct Supply"))
check("application subject counts at another domain",
      _about_company("recruiting@scm-lp.com", "Thank you for applying to Stevens Capital", "Stevens Capital Management"))
check("a newsletter at another domain does not",
      not _about_company("cassie@devpost.com", "HACKATHONS just for you, Sulaiman", "Post Consumer Brands"))
check("a UPS mailing is not TikTok", not _about_company("jobs@recruiting.ups.com", "The New @UPSjobs TikTok Is Live", "TikTok"))
check("a two-letter root never matches by accident", not _about_company("x@co.uk", "", "Cobalt"))

# --- the sentence guard --------------------------------------------------------
RESUME = ("Built DukeGPT, serving 2,000+ monthly requests across 9 backend services.\n"
          "Deployed microservices with FastAPI and MCP servers, cut API token usage 40% via caching.\n"
          "Duke Oﬃce of Information Technology")
check("a sentence in the résumé's own terms passes",
      _sentence_problems("Most recently I built DukeGPT at Duke Office of Information Technology, serving 2,000+ monthly requests.", RESUME) == [])
check("the ligature in 'Oﬃce' still matches 'Office'",
      _sentence_problems("Lately I worked at Duke Office of Information Technology on DukeGPT.", RESUME) == [])
p = _sentence_problems("Most recently I built DukeGPT, serving 5,000 monthly requests.", RESUME)
check("a number the résumé does not state is caught", any("5000" in x for x in p), p)
p = _sentence_problems("Most recently I built DukeGPT for Roblox using FastAPI.", RESUME)
check("a name the résumé does not print is caught", any("Roblox" in x for x in p), p)
check("a percentage the résumé states passes", _sentence_problems("Lately I cut API token usage 40% via caching on DukeGPT.", RESUME) == [])
check("an empty sentence is no sentence", _sentence_problems("", RESUME) == ["no usable sentence"])
check("a runaway paragraph is no sentence", _sentence_problems("word " * 45, RESUME) == ["no usable sentence"])

# --- a search that cites a people-search site has found nothing ----------------
check("a data-broker page is not a publication", _broker_page("https://www.signalhire.com/profiles/x%27s-email/1"))
check("a Growjo page is not a publication", _broker_page("https://growjo.com/employee/Someone-1"))
check("the company's own careers page is", not _broker_page("https://careers.whatnot.com/home"))
check("a Greenhouse board page is", not _broker_page("https://job-boards.eu.greenhouse.io/imc/jobs/4914883101"))

# --- the careers-site crawl: which links to follow, what to keep -------------
HTML = """<nav><a href="/students">Students &amp; Grads</a> <a href="/about">About</a>
<a href="https://careers.acme.com/university-recruiting">University Recruiting</a>
<a href="https://other.example.org/campus">Campus (elsewhere)</a> <a href="/brochure-intern.pdf">Intern brochure</a>
<a href="/careers#interns">Interns</a></nav>
<p>Questions? Email <a href="mailto:campus.recruiting@acme.com">campus.recruiting@acme.com</a> or press@acme.com.</p>"""
links = _campus_links(HTML, "https://careers.acme.com/")
check("campus links: students and university pages on the site", links[:2] == ["https://careers.acme.com/students", "https://careers.acme.com/university-recruiting"], links)
check("campus links: another site, a PDF and the page itself are skipped",
      not any("example.org" in l or l.endswith(".pdf") for l in links) and "https://careers.acme.com/careers" in links, links)
found = []
_harvest(HTML, "https://careers.acme.com/", found)
check("harvest: the recruiting mailbox is kept, the press one is not", [f["address"] for f in found] == ["campus.recruiting@acme.com"], found)
found = []
_harvest("<p>Reach the team at earlycareers@acme.com</p>", "https://careers.acme.com/students", found)
check("harvest: any writable address on a students page counts", [f["address"] for f in found] == ["earlycareers@acme.com"], found)
found = []
_harvest("<p>Reach the team at jane.doe@acme.com</p>", "https://careers.acme.com/", found)
check("harvest: a bare name on a general page, with no recruiting context, is not", found == [], found)

# --- the subject names the term once ------------------------------------------
for role, want in (("Tech & Data Program Summer 2027 - Software Engineer Intern", "Tech & Data Program - Software Engineer Intern"),
                   ("Summer 2027 Internship - Data Analytics - Michigan", "Internship - Data Analytics - Michigan"),
                   ("Intern - IT (Summer 2027)", "Intern - IT"), ("AI Engineer Intern - Summer 2027", "AI Engineer Intern"),
                   ("Summer 2027 Internship: Data Engineering", "Internship: Data Engineering"),
                   ("Software Engineering Intern", "Software Engineering Intern"), ("Fall '26 SWE Intern", "SWE Intern"), ("", "internship")):
    check(f"role label: {role!r}", _role_label(role) == want, _role_label(role))

# --- two processes share the log without losing each other's entries ----------
import tempfile
with tempfile.TemporaryDirectory() as d:
    out = Path(d)
    a = load_log(out); b = load_log(out)            # a lookup pass and a send, loaded from the same empty file
    a["lookups"]["acme"] = {"at": 1, "addresses": []}; save_log(out, a)
    b["sent"]["beta"] = {"to": "x@beta.com"}; b["skipped"]["gamma"] = "no address"; save_log(out, b)
    final = load_log(out)
    check("log merge: the lookup written first survives the send's save", "acme" in final["lookups"], final)
    check("log merge: the send's entry is there too", final["sent"].get("beta", {}).get("to") == "x@beta.com", final)
    check("log merge: the saver's own dict sees the merged state", "acme" in b["lookups"], list(b["lookups"]))

# --- the first version of this file's cases, kept ------------------------------
check("an assessment mailbox is never written to", bool(NOREPLY.search("assessment@email.roblox.com")))
check("an accommodations mailbox is never written to", bool(NOREPLY.search("candidateaccommodations@rivian.com")))
check("a campus recruiting mailbox is", not NOREPLY.search("universityrecruiting@stokespace.com"))
check("a careers mailbox is", not NOREPLY.search("careers@stokespace.com"))
check("'Roblox Assessment' is not a person", not _person("Roblox Assessment", "Roblox"))
check("'GuideWell Talent Acquisition' is not a person", not _person("GuideWell Talent Acquisition", "GuideWell Mutual"))
check("a two-word capitalised name is a person", _person("Akshay Kumar", "Ambrook"))
check("a lowercase mailbox label is not", not _person("campusus imc", "IMC"))
for bad in ("targetworkdayprogram@target.com", "myworkday@thehartford.com", "rs.workday@medtronic.com", "hrsupport_na@micron.com",
            "accommodations@adobe.com", "recruitmentoperationsservicing@aexp.com", "seeyourself@thecignagroup.com", "contact@anduril.com"):
    check(f"never written to: {bad}", not _writable(bad))
for good in ("targetcareers@target.com", "careers@talos.com", "universityrecruiting@x.com"):
    check(f"a recruiting address: {good}", _writable(good))
check("ligatures are read as letters in the fact guard", "office" in _unligate("Duke O\ufb03ce of IT").lower())

# --- what the second lookup pass let through, and must not again -------------
for addr in ("bootstrap-icons@1.10.5", "js-cookie@3.0.5", "helpdesk@bah.com", "askhr@medtronic.com", "us-askhr@abb.com",
             "applyassistance@danaher.com", "leaveadministration@cna.com", "ipadmin@gevernova.com", "hrsharedservices@brunswick.com",
             "amgencareers@careers.pure.cloud", "team@basepowercompany.com", "admin@acme.com"):
    check(f"not writable: {addr}", not _writable(addr))
for addr in ("campusrecruiting@ntrs.com", "early.careers@aig.com", "earlycareers@talos.com", "internships@etched.com",
             "recruiting@jumptrading.com", "corporatetalentacquisition@oshkoshcorp.com", "hrrecruitingassistant@directs.com"):
    check(f"writable: {addr}", _writable(addr))
check("a directory listing is not a publication", _broker_page("https://www.allbiz.com/business/clearwater-analytics-1"))
check("a VC news site is not a publication", _broker_page("https://www.vcnewsdaily.com/One%20Finance/venture-funding.php"))
check("a cached web source citing a directory is dropped", _cited_broker("web: https://www.allbiz.com/business/dee-zee-oem-plant-515-2"))
check("a cached web source citing the company is kept", not _cited_broker("web: https://www.aig.com/campus"))
check("an inbox source is kept", not _cited_broker("inbox: Thank you for applying"))

# --- an accommodation-request line on a posting ----------------------------------
found = []
_harvest("<p>If you need an accommodation to apply, e-mail uswaptdo@td.com.</p>", "https://td.wd3.myworkdayjobs.com/x/job/y", found)
check("harvest: an accommodation mailbox with an opaque name is dropped", found == [], found)
found = []
_harvest("<p>For accommodation requests contact corporatetalentacquisition@oshkoshcorp.com.</p>", "https://oshkosh.wd5.myworkdayjobs.com/x", found)
check("harvest: a talent-acquisition mailbox is kept even in that context", [f["address"] for f in found] == ["corporatetalentacquisition@oshkoshcorp.com"], found)
found = []
_harvest("<p>Questions about your application? jobs@countryfinancial.com</p>", "https://countryfinancial.wd5.myworkdayjobs.com/x", found)
check("harvest: a jobs@ mailbox counts as recruiting by name", [f["address"] for f in found] == ["jobs@countryfinancial.com"], found)

found = []
_harvest("<p>Questions about applying? Write to uswaptdo@td.com</p>", "https://td.wd3.myworkdayjobs.com/x/job/y", found)
check("harvest: on a posting page an opaque mailbox is dropped whatever the words around it", found == [], found)
found = []
_harvest("<p>Questions about applying? Write to hrhire@acme.com</p>", "https://www.acme.com/careers/hiring-process", found)
check("harvest: on the company's own page the words around it still count", [f["address"] for f in found] == ["hrhire@acme.com"], found)

# --- a named human vs a mailbox label ------------------------------------------
check("_person: a named human", _person("Dominique Burns", "BTI360"))
check("_person: a team label is not", not _person("Netic Hiring Team", "Netic"))
check("_person: the company's own name is not", not _person("Ambrook Recruiting", "Ambrook"))

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
