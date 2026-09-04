"""The discovery filter and ATS conventions, offline.

Synthetic listings shaped exactly like SimplifyJobs' listings.json. What these
prove: the term/category/title/location/degree rules each fire on the case
they exist for and nothing else, attempted postings are not re-selected, and
the attempt order puts fillable forms before login walls.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resume_tailor import ats
from resume_tailor.discover import Prefs, evaluate, is_us, select, title_terms, to_entry
from resume_tailor.queue import RunState

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


def L(**kw):
    base = dict(id="x", company_name="Acme", title="Software Engineer Intern", active=True, is_visible=True,
                terms=["Summer 2027"], category="Software", locations=["San Jose, CA"],
                degrees=["Bachelor's"], url="https://job-boards.greenhouse.io/acme/jobs/1", date_posted=100)
    base.update(kw)
    return base


P = Prefs()

# --- is_us ------------------------------------------------------------------
check("state suffix -> US", is_us(["Huntsville, AL"]) is True)
check("NYC alias -> US", is_us(["NYC"]) is True)
check("SF alias -> US", is_us(["SF"]) is True)
check("Remote in USA -> US", is_us(["Remote in USA"]) is True)
check("London, UK -> not US", is_us(["London, UK"]) is False)
check("Toronto, ON, Canada -> not US", is_us(["Toronto, ON, Canada"]) is False)
check("mixed US + abroad -> US (any US office counts)", is_us(["London, UK", "Seattle, WA"]) is True)
check("no locations -> unknown (kept)", is_us([]) is None)
check("unrecognised city -> unknown (kept)", is_us(["Springfield"]) is None)

# --- evaluate ---------------------------------------------------------------
check("baseline listing accepted", evaluate(L(), P) == "", evaluate(L(), P))
check("inactive rejected", evaluate(L(active=False), P) == "inactive")
check("wrong term rejected", evaluate(L(terms=["Summer 2026"]), P) == "other term")
check("title naming Fall 2026 rejected even when the row says Summer 2027",
      evaluate(L(title="Software Engineer Intern - Fall 2026"), P).startswith("other term"))
check("title naming Spring 2027 rejected", evaluate(L(title="Software Development Intern - Spring 2027"), P).startswith("other term"))
check("title naming Summer/Fall 2027 kept (one half fits)", evaluate(L(title="SWE Intern - Summer/Fall 2027"), P) == "")
check("bare year in title kept", evaluate(L(title="2026 Software Engineering Intern"), P) == "")
check("title_terms: seasons and years", title_terms("Co-op - Fall 2026") == {"Fall 2026"})
check("title_terms: split seasons", title_terms("Intern - Winter/Spring 2026") == {"Winter 2026", "Spring 2026"})
check("title_terms: short year and no space", title_terms("SWE Intern Summer2026 / Fall '27") == {"Summer 2026", "Fall 2027"})
check("title_terms: autumn -> Fall", title_terms("Autumn 2027 Intern") == {"Fall 2027"})
check("title_terms: nothing named", title_terms("Software Engineer Intern") == set())
check("Fall 2026 + Summer 2027 accepted", evaluate(L(terms=["Fall 2026", "Summer 2027"]), P) == "")
check("AI/ML category rejected", evaluate(L(category="AI/ML/Data"), P).startswith("category"))
check("'Software Engineering' category accepted", evaluate(L(category="Software Engineering"), P) == "")
check("hardware title rejected", evaluate(L(title="Hardware Engineer Intern"), P).startswith("title"))
check("product specialist rejected", evaluate(L(title="Product Specialist Intern"), P).startswith("title"))
check("'Full Stack Developer Co-op' accepted", evaluate(L(title="Full Stack Developer Co-op"), P) == "")
check("'Systems Engineer Intern - Summer Games' accepted", evaluate(L(title="Systems Engineer Intern - Summer Games"), P) == "")
check("London-only rejected", evaluate(L(locations=["London, UK"]), P) == "outside US")
check("PhD-only rejected", evaluate(L(degrees=["PhD"]), P) == "PhD-only")
check("PhD + Bachelor's accepted", evaluate(L(degrees=["PhD", "Bachelor's"]), P) == "")
check("company blacklist honoured", evaluate(L(company_name="Wayfair"), Prefs(company_blacklist=["wayfair"])) == "company blacklist")
check("title blacklist honoured", evaluate(L(title="Software Engineer Intern - Clearance"), Prefs(title_blacklist=["clearance"])) == "title blacklist")
check("require_us=False keeps London", evaluate(L(locations=["London, UK"]), Prefs(require_us=False)) == "")

# --- ats --------------------------------------------------------------------
check("lever posting -> /apply", ats.apply_url_for("https://jobs.lever.co/palantir/abc") == "https://jobs.lever.co/palantir/abc/apply")
check("lever /apply idempotent", ats.apply_url_for("https://jobs.lever.co/palantir/abc/apply").endswith("/abc/apply"))
check("ashby -> /application", ats.apply_url_for("https://jobs.ashbyhq.com/co/xyz").endswith("/xyz/application"))
check("greenhouse unchanged", ats.apply_url_for("https://job-boards.greenhouse.io/acme/jobs/1") == "https://job-boards.greenhouse.io/acme/jobs/1")
check("workday is account-gated", ats.host_kind("https://bah.wd1.myworkdayjobs.com/x") in ats.NEEDS_ACCOUNT)
check("tiktok careers is 'other'", ats.host_kind("https://lifeattiktok.com/search/123") == "other")

# --- select: dedup, company rule, ordering, limit ----------------------------
with tempfile.TemporaryDirectory() as d:
    state = RunState.load(Path(d) / "state.json")
    state.record("done-1", {"status": "applied", "company": "Old Co"})
    state.record("retry-1", {"status": "needs_login"})
    state.mark_applied("Old Co")
    # Distinct companies, so the per-company collapse does not interfere with
    # what this block tests (ordering, dedup, retry, limit).
    listings = [
        L(id="wd", company_name="Booz", url="https://bah.wd1.myworkdayjobs.com/x", date_posted=900),        # account-gated, newest
        L(id="gh-old", company_name="Acme", date_posted=100),                                                # form
        L(id="gh-new", company_name="Beta", url="https://job-boards.greenhouse.io/beta/jobs/2", date_posted=500),  # form, newer
        L(id="tt", company_name="TikTok", url="https://lifeattiktok.com/search/1", date_posted=700),          # other
        L(id="done-1", company_name="Done Co"),                                                              # attempted, applied
        L(id="retry-1", company_name="Retry Co", url="https://jobs.lever.co/co/r"),                          # attempted, retryable
        L(id="dup-co", company_name="Old Co", url="https://jobs.lever.co/oldco/1"),                          # company already applied
        L(id="abroad", company_name="Abroad Co", locations=["Berlin, Germany"]),
    ]
    entries, excluded = select(listings, P, state)
    ids = [e.id for e in entries]
    # retry-1 was attempted before: never-attempted postings lead (forms, then
    # other hosts, then account-gated), and a retry is dealt in after every
    # three of them — retries must neither crowd out new postings nor wait
    # behind the whole pool.
    check("order: never-attempted first (forms, then other, then account-gated), a retry after three",
          ids == ["gh-new", "gh-old", "tt", "retry-1", "wd"], ids)
    check("applied entry not re-selected", "done-1" not in ids)
    check("needs_login entry IS re-selected (retryable)", "retry-1" in ids)
    check("apply_once_at_company honoured", "dup-co" not in ids)
    check("exclusion tally explains the rest",
          excluded.get("already attempted") == 1 and excluded.get("already applied at company") == 1 and excluded.get("outside US") == 1, excluded)
    limited, _ = select(listings, P, state, limit=2)
    check("limit respected", len(limited) == 2)
    state.record("abbvie-1", {"status": "fit_rejected", "company": "AbbVie", "role": "2027 BTS Intern - Data & Software Engineering"})
    twice = listings + [L(id="abbvie-2", company_name="AbbVie", title="2027 BTS Intern - Data & Software Engineering", url="https://jobs.lever.co/abbvie/2"),
                        L(id="abbvie-3", company_name="AbbVie", title="Software Engineering Intern, Platform", url="https://jobs.lever.co/abbvie/3")]
    ids2, excluded2 = select(twice, P, state)
    check("a second listing of a role the judges already held is not tailored again",
          "abbvie-2" not in ids2 and excluded2.get("same role already judged") == 1)
    check("a different role at that company still goes through", "abbvie-3" in [e.id for e in ids2])
    state.record("nisc-1", {"status": "fit_rejected", "company": "NISC", "role": "Software Developer Intern"})
    state.record("nisc-2", {"status": "fit_rejected", "company": "NISC", "role": "Software Development Intern"})
    thrice = twice + [L(id="nisc-3", company_name="NISC", title="Software Engineering Intern", url="https://jobs.lever.co/nisc/3")]
    ids3, excluded3 = select(thrice, P, state)
    check("a company the judges have held twice is not tailored for a third time",
          "nisc-3" not in [e.id for e in ids3] and excluded3.get("company held twice by the judges") == 1)
    check("a company held once still gets its other roles tried", "abbvie-3" in [e.id for e in ids3])
    from resume_tailor.ats import host_kind
    check("ats: a careers site naming its system in the query string is that system (account-gated)",
          host_kind("https://careers.westinghousenuclear.com/job/x/1422595200/?ats=successfactors") == "successfactors"
          and host_kind("https://careers.example.com/job/1") == "other"
          and host_kind("https://careers.cvent.com/jobs/10806?icims=1") == "icims")
    # A posting a dry run filled completely — resume and verdicts cached —
    # is the surest submission there is, and goes before anything new.
    state.record("ready-1", {"status": "ready_not_submitted", "pdf": "/x.pdf", "fit": "ok"})
    ready_ids = [e.id for e in select(listings + [L(id="ready-1", company_name="Ready Co", date_posted=50)], P, state)[0]]
    check("order: a dry-run-verified posting goes first, then three new, then the retry",
          ready_ids == ["ready-1", "gh-new", "gh-old", "tt", "retry-1", "wd"], ready_ids)
    e = to_entry(L(id="lv", url="https://jobs.lever.co/co/abc", company_name="Co", title="SWE Intern"))
    check("entry carries id, company, title and the form url",
          (e.id, e.company_hint, e.title, e.apply_url) == ("lv", "Co", "SWE Intern", "https://jobs.lever.co/co/abc/apply"))

# --- one posting per company, chosen by position preference ------------------
from resume_tailor.discover import one_per_company, position_score

PREF = Prefs(positions=["Software Engineer Intern", "Backend Engineer Intern", "Platform Engineer Intern",
                        "Full-stack Engineer Intern", "Frontend Engineer Intern"])
check("position_score: backend beats frontend by list order",
      position_score("Backend Software Engineer Intern", PREF.positions) < position_score("Frontend Software Engineer Intern", PREF.positions))
check("position_score: 'Full Stack' matches the 'Full-stack' preference",
      position_score("Full Stack Developer Co-op", PREF.positions) == 3)
check("position_score: generic-only match ranks after every specific position",
      position_score("Mobile Software Engineer Intern", PREF.positions) == len(PREF.positions))
check("position_score: specific match keeps its list position",
      position_score("Backend Software Engineer Intern", PREF.positions) == 1)
check("position_score: with the generic entry FIRST, backend still beats mobile",
      position_score("Backend Software Engineer Intern", PREF.positions) < position_score("Mobile Software Engineer Intern", PREF.positions))
check("position_score: nothing matches -> last",
      position_score("Robotics Intern", PREF.positions) == 2 * len(PREF.positions))
verkada = [
    L(id="v-mobile", company_name="Verkada", title="Mobile Software Engineer Intern", date_posted=400),
    L(id="v-sec", company_name="Verkada", title="Security Software Engineer Intern", date_posted=400),
    L(id="v-front", company_name="Verkada", title="Frontend Software Engineer Intern", date_posted=400),
    L(id="v-back", company_name="Verkada", title="Backend Software Engineer Intern", date_posted=300),
    L(id="other", company_name="Other Co", title="Software Engineer Intern"),
]
kept = one_per_company(verkada, Prefs(positions=["Backend Engineer Intern", "Software Engineer Intern"]))
check("one per company: Verkada collapses to its backend posting",
      sorted(l["id"] for l in kept) == ["other", "v-back"], [l["id"] for l in kept])
generic_first = Prefs(positions=["Software Engineer Intern", "Backend Engineer Intern", "Full-stack Engineer Intern"])
kept2 = one_per_company(verkada, generic_first)
check("one per company with the user's actual order (generic first) still picks Backend, not Mobile",
      sorted(l["id"] for l in kept2) == ["other", "v-back"], [l["id"] for l in kept2])
with tempfile.TemporaryDirectory() as d:
    sel, ex = select(verkada, Prefs(positions=["Backend Engineer Intern"]), RunState.load(Path(d) / "s.json"))
    check("select applies the company collapse and reports it",
          [e.id for e in sel] == ["v-back", "other"] or [e.id for e in sel] == ["other", "v-back"], [e.id for e in sel])
    check("collapsed duplicates counted as 'same company, later pass'", ex.get("same company, later pass") == 3, ex)

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
