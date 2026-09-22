"""The outreach filters, offline: which inbox addresses are worth writing to,
which belong to the company at all, and what a drafted sentence may say.
No network, no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import resume_tailor.outreach as outreach
from resume_tailor.outreach import (NOREPLY, NetworkDown, _about_company, _broker_page, _campus_links, _cited_broker, _greeting,
                                    _harvest, _name_near, _network_error, _person, _personal_local, _ranked, _role_label,
                                    _sentence_problems, _through_outages, _unligate, _writable, find_addresses, load_log,
                                    save_log, search)

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

# --- a person before a mailbox: what counts as a person --------------------------
for addr, name in (("dominique.burns@bti360.com", "Dominique Burns"), ("john_smith@acme.com", "John Smith"),
                   ("maria-lopez@acme.com", "Maria Lopez")):
    check(f"personal mailbox: {addr} -> {name}", _personal_local(addr) == name, _personal_local(addr))
for addr in ("careers@acme.com", "hr.lplfinancial@lplfinancial.com", "talent.scout@stryker.com", "campus.recruiting@acme.com",
             "us.interns@acme.com", "north.america@acme.com", "new.grad@acme.com", "early.careers@acme.com", "dboren@acme.com",
             "software.engineering@acme.com", "college.relations@acme.com", "join.us@acme.com", "summer.analyst@acme.com",
             "data.science@acme.com", "hello.world@acme.com", "info.us@acme.com", "bootstrap-icons@1.10.5", "js-cookie@3.0.5",
             "first.last@no-reply.acme.com"):
    check(f"not a personal mailbox: {addr}", _personal_local(addr) == "", _personal_local(addr))
for label in ("Human Resources", "WEX Human Resources", "Dee Zee Careers", "Early Careers Team", "Student Programs",
              "Corporate Staffing", "ERCOT Human Resources"):
    check(f"a label is not a person: {label!r}", not _person(label, "ERCOT"))

near = "Questions about the internship? Contact David Boren, University Recruiter, at dboren@acme.com or call the office."
check("name beside its own mailbox is attached", _name_near("dboren@acme.com", near) == "David Boren", _name_near("dboren@acme.com", near))
check("name beside a shared mailbox is not", _name_near("careers@acme.com", "Reach Jane Roe at careers@acme.com") == "")
check("a label beside the mailbox is not a name", _name_near("dboren@acme.com", "Contact Human Resources at dboren@acme.com") == "")
check("first name alone identifies the mailbox", _name_near("akshay@ambrook.com", "Write to Akshay Kumar, akshay@ambrook.com") == "Akshay Kumar")

found = []
_harvest("<h2>University Recruiting</h2><p>Contact Dominique Burns, University Recruiting Lead, at "
         "<a href=\"mailto:dominique.burns@acme.com\">dominique.burns@acme.com</a> or the team at careers@acme.com.</p>",
         "https://acme.com/careers/students", found)
by = {f["address"]: f for f in found}
check("harvest: the named recruiter ranks 1 with her name",
      by.get("dominique.burns@acme.com", {}).get("rank") == 1 and by["dominique.burns@acme.com"]["name"] == "Dominique Burns", str(found))
check("harvest: the shared mailbox on the same page stays rank 2", by.get("careers@acme.com", {}).get("rank") == 2, str(found))
found = []
_harvest("<p>Our team: Priya Natarajan, University Relations — <a href=\"mailto:pnatarajan@acme.com\">pnatarajan@acme.com</a></p>",
         "https://acme.com/about/team", found)
check("harvest: a name printed beside a flast mailbox on a plain page is kept, rank 1",
      found and found[0]["rank"] == 1 and found[0]["name"] == "Priya Natarajan", str(found))

check("ranked: a label-named entry an earlier pass called a person is a team mailbox",
      _ranked({"address": "hresources@ercot.com", "name": "Human Resources", "rank": 1}, "ERCOT")["rank"] == 2)
r = _ranked({"address": "dominique.burns@acme.com", "name": "", "source": "page: x", "rank": 2}, "Acme")
check("ranked: a personal mailbox cached at rank 2 comes up to 1 with its name", r["rank"] == 1 and r["name"] == "Dominique Burns", str(r))
check("ranked: a confirmation reply-to keeps rank 0",
      _ranked({"address": "a.b@acme.com", "name": "Ann Bee", "rank": 0}, "Acme")["rank"] == 0)
check("ranked: an address on a page that would not load is left alone",
      _ranked({"address": "ann.bee@acme.com", "name": "", "source": "web, page unreachable: x", "rank": 3}, "Acme")["rank"] == 3)

check("greeting: a named person by first name", _greeting({"address": "x@acme.com", "name": "Dominique Burns"}, "Acme") == "Dominique")
check("greeting: a personal mailbox by its first name", _greeting({"address": "john.smith@acme.com", "name": ""}, "Acme") == "John")
check("greeting: a shared mailbox as the team", _greeting({"address": "careers@acme.com", "name": "Talent Team"}, "Acme") == "Acme recruiting team")

cand = {"key": "acme", "company": "Acme", "role": "SWE Intern", "id": "1", "url": "https://acme.com/jobs/1", "pdf": ""}
log = {"sent": {}, "lookups": {"acme": {"at": __import__("time").time(), "addresses": [
    {"address": "careers@acme.com", "name": "", "source": "page: x", "rank": 2},
    {"address": "dominique.burns@acme.com", "name": "", "source": "page: y", "rank": 2}]}}, "skipped": {}}
got = find_addresses(cand, {}, log, use_web=False)
check("find_addresses: the personal mailbox in a stale cache goes first, as a person",
      got and got[0]["address"] == "dominique.burns@acme.com" and got[0]["rank"] == 1 and got[0]["name"] == "Dominique Burns", str(got))

# search(): the person-targeted web search runs only when no person was found
_real = {k: getattr(outreach, k) for k in ("from_inbox", "_company_site", "from_pages", "from_web")}
calls = []
outreach.from_inbox = lambda results, key: []
outreach._company_site = lambda cand, results: ""
outreach.from_pages = lambda url, site="": [{"address": "careers@acme.com", "name": "", "source": "page: x", "rank": 2}]
outreach.from_web = lambda company, url, people=False: (calls.append(people) or
                                                       [{"address": "ann.bee@acme.com", "name": "Ann Bee", "source": "web: z", "rank": 1}])
got = search(cand, {}, use_web=True)
check("search: a team mailbox alone triggers the person search, and the person goes ahead",
      calls == [True] and [g["address"] for g in got] == ["careers@acme.com", "ann.bee@acme.com"], f"{calls} {got}")
calls.clear()
outreach.from_pages = lambda url, site="": [{"address": "ann.bee@acme.com", "name": "Ann Bee", "source": "page: x", "rank": 1}]
got = search(cand, {}, use_web=True)
check("search: a person already found means no web search at all", calls == [] and len(got) == 1, f"{calls} {got}")
calls.clear()
outreach.from_pages = lambda url, site="": []
got = search(cand, {}, use_web=True)
check("search: nothing found -> the general search, then no person search once it finds one", calls == [False], str(calls))
calls.clear()
outreach.from_web = lambda company, url, people=False: (calls.append(people) or [])
got = search(cand, {}, use_web=True)
check("search: nothing anywhere -> general then person search, both", calls == [False, True] and got == [], str(calls))
calls.clear()
outreach.from_pages = lambda url, site="": [{"address": "careers@acme.com", "name": "", "source": "page: x", "rank": 2}]
got = search(cand, {}, use_web=False)
check("search: with the web off, no search of either kind", calls == [] and len(got) == 1, str(calls))
for k, v in _real.items():  # the real functions back, for the tests below
    setattr(outreach, k, v)


# --- connection failures: wait for the network, never blame the company --------
import smtplib
import socket
import tempfile
from types import SimpleNamespace


class APIConnectionError(Exception):  # the OpenAI SDK's class, by name
    pass


for e in (socket.gaierror(8, "nodename nor servname provided, or not known"),
          smtplib.SMTPServerDisconnected("Connection unexpectedly closed: The read operation timed out"),
          APIConnectionError("Connection error."), TimeoutError("timed out"), ConnectionResetError(54, "reset"),
          OSError(8, "nodename nor servname provided, or not known")):
    check(f"network error: {type(e).__name__}: {str(e)[:40]}", _network_error(e))
for e in (ValueError("the sentence names Google, which the résumé does not"), smtplib.SMTPAuthenticationError(535, b"bad"),
          smtplib.SMTPRecipientsRefused({}), RuntimeError("no mailbox credentials (RESUME_TAILOR_IMAP_PASSWORD)"),
          KeyError("addresses")):
    check(f"not a network error: {type(e).__name__}", not _network_error(e))

outreach.NETWORK_WAITS = (0, 0)
outreach.SEND_PAUSE = 0


def flaky(fail_times, exc):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return "ok"
    return fn, calls


fn, calls = flaky(2, socket.gaierror(8, "nodename nor servname"))
check("through outages: recovers after two failures", _through_outages(fn) == "ok" and calls["n"] == 3)
fn, calls = flaky(99, socket.gaierror(8, "nodename nor servname"))
try:
    _through_outages(fn)
    check("through outages: raises NetworkDown when every try fails", False)
except NetworkDown:
    check("through outages: raises NetworkDown when every try fails", calls["n"] == len(outreach.NETWORK_WAITS) + 1)
fn, calls = flaky(99, ValueError("the sentence names Google"))
try:
    _through_outages(fn)
    check("through outages: a company failure raises at once", False)
except ValueError:
    check("through outages: a company failure raises at once", calls["n"] == 1)


# --- the run loop itself, with the model and the mail server stubbed ------------
import resume_tailor.mailscan as mailscan
from resume_tailor.profile import Profile

mailscan.load_results = lambda out_dir: {}
Profile.load = staticmethod(lambda root=None: SimpleNamespace(career={}))
outreach.find_addresses = lambda cand, results, log, use_web=True: [{"address": "careers@acme.com", "name": "", "source": "test", "rank": 2}]
outreach._resume_text = lambda pdf: "Duke University, DukeGPT"


def scenario(compose_fails=None, send_fails=None, people_only=False, find_fails=None):
    """Run three companies a, b, c; the given exception is raised by compose
    or send the given number of times for company a. Returns the log and the
    call counts."""
    tmp = Path(tempfile.mkdtemp())
    cands = []
    for k in "abc":
        pdf = tmp / f"{k}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        cands.append({"key": k, "company": k.upper() + " Corp", "role": "SWE Intern Summer 2027", "id": k, "url": "https://x/" + k, "pdf": str(pdf)})
    outreach.candidates = lambda out_dir: cands
    n = {"compose": {}, "send": {}, "find": {}}

    def find(cand, results, log, use_web=True):
        k = cand["key"]
        n["find"][k] = n["find"].get(k, 0) + 1
        if find_fails and k == "a" and n["find"][k] <= find_fails[1]:
            raise find_fails[0]
        return [{"address": "careers@acme.com", "name": "", "source": "test", "rank": 2}]
    outreach.find_addresses = find

    def compose(cand, to, linkedin, resume_text):
        k = cand["key"]
        n["compose"][k] = n["compose"].get(k, 0) + 1
        if compose_fails and (k == "a" or compose_fails[2]) and n["compose"][k] <= compose_fails[1]:
            raise compose_fails[0]
        return f"SWE Intern, Summer 2027 - quick hello", "Hi"

    def send(to_addr, subject, body, pdf, display_name="x"):
        k = [c["key"] for c in cands if c["pdf"] == pdf][0]
        n["send"][k] = n["send"].get(k, 0) + 1
        if send_fails and k == "a" and n["send"][k] <= send_fails[1]:
            raise send_fails[0]
        return "<mid>"
    outreach.compose, outreach.send = compose, send
    done = outreach.run(tmp, dry_run=False, max_send=15, force=True, use_web=False, people_only=people_only)
    return load_log(tmp), n, done


dns = socket.gaierror(8, "nodename nor servname provided, or not known")
log, n, done = scenario(compose_fails=(dns, 1, False))
check("run: one DNS failure on the note is waited out, all three sent", sorted(log["sent"]) == ["a", "b", "c"] and not log["skipped"],
      f"sent={sorted(log['sent'])} skipped={log['skipped']}")
check("run: the waited-out company was tried twice", n["compose"]["a"] == 2)

log, n, done = scenario(compose_fails=(APIConnectionError("Connection error."), 99, True))
check("run: the network stays down -> stop after OUTAGE_STOP companies, nothing sent",
      not log["sent"] and sorted(log["skipped"]) == ["a", "b"] and "c" not in n["compose"],
      f"sent={sorted(log['sent'])} skipped={log['skipped']} compose={n['compose']}")
check("run: a lost company is marked 'network down', not 'could not write the note'",
      all(v.startswith("network down:") for v in log["skipped"].values()), str(log["skipped"]))
check("run: each lost company got every wait", n["compose"]["a"] == len(outreach.NETWORK_WAITS) + 1)

log, n, done = scenario(compose_fails=(ValueError("the sentence names Google, which the résumé does not"), 99, False))
check("run: a rejected sentence skips that company at once and the run goes on",
      sorted(log["sent"]) == ["b", "c"] and log["skipped"]["a"].startswith("could not write the note") and n["compose"]["a"] == 1,
      f"sent={sorted(log['sent'])} skipped={log['skipped']} compose={n['compose']}")

log, n, done = scenario(send_fails=(smtplib.SMTPServerDisconnected("Connection unexpectedly closed: The read operation timed out"), 1))
check("run: one SMTP timeout is waited out and the e-mail still goes", sorted(log["sent"]) == ["a", "b", "c"] and n["send"]["a"] == 2,
      f"sent={sorted(log['sent'])} send={n['send']}")

log, n, done = scenario(send_fails=(smtplib.SMTPRecipientsRefused({"careers@acme.com": (550, b"no such user")}), 99))
check("run: a refused recipient is 'send failed' once, no retry, run goes on",
      sorted(log["sent"]) == ["b", "c"] and log["skipped"]["a"].startswith("send failed") and n["send"]["a"] == 1,
      f"sent={sorted(log['sent'])} skipped={log['skipped']} send={n['send']}")


log, n, done = scenario(people_only=True)
check("run --people: a company with only a shared mailbox is skipped, nothing composed",
      not log["sent"] and all(v.startswith("no named recruiter") for v in log["skipped"].values()) and not n["compose"],
      f"sent={sorted(log['sent'])} skipped={log['skipped']} compose={n['compose']}")


log, n, done = scenario(find_fails=(NetworkDown("web search: Connection error."), 1))
check("run: an outage inside the address search is waited out, all three sent",
      sorted(log["sent"]) == ["a", "b", "c"] and n["find"]["a"] == 2, f"sent={sorted(log['sent'])} find={n['find']}")
log, n, done = scenario(find_fails=(NetworkDown("web search: Connection error."), 99))
check("run: an address search that stays down loses the company, and the run goes on to the next",
      sorted(log["sent"]) == ["b", "c"] and log["skipped"]["a"].startswith("network down"), f"sent={sorted(log['sent'])} skipped={log['skipped']}")

# from_web: a connection failure raises NetworkDown; any other failure is "nothing found"
import openai
_real_openai = openai.OpenAI


class _APIConnectionError(Exception):
    pass


_APIConnectionError.__name__ = "APIConnectionError"


def _fake_client(exc):
    class Responses:
        def create(self, **kw):
            raise exc

    class Client:
        def __init__(self, **kw):
            self.responses = Responses()
    return Client


openai.OpenAI = _fake_client(_APIConnectionError("Connection error."))
try:
    outreach.from_web("Acme", "https://acme.com/jobs")
    check("from_web: a connection failure raises NetworkDown", False, "returned instead of raising")
except NetworkDown:
    check("from_web: a connection failure raises NetworkDown", True)
except Exception as e:
    check("from_web: a connection failure raises NetworkDown", False, f"raised {type(e).__name__}")
openai.OpenAI = _fake_client(ValueError("malformed"))
check("from_web: any other failure is nothing found, not an outage", outreach.from_web("Acme", "https://acme.com/jobs") == [])
openai.OpenAI = _real_openai


width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
