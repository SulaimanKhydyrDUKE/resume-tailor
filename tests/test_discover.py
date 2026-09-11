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
check("title_terms: year before the season", title_terms("2027 Spring Term Software Engineer Co-op") == {"Spring 2027"})
check("season without a year rejected", evaluate(L(title="Software Engineer Winter Co-Op"), P) == "other term: Winter")
check("season without a year kept when summer is also named", evaluate(L(title="Summer/Fall Software Intern"), P) == "")
check("no season at all kept", evaluate(L(title="Software Engineer Co-op"), P) == "")
check("Fall 2026 + Summer 2027 accepted", evaluate(L(terms=["Fall 2026", "Summer 2027"]), P) == "")
check("AI/ML category read by default (its title gate decides)", not evaluate(L(category="AI/ML/Data"), P).startswith("category"))
check("AI/ML category rejected when the profile narrows categories to software",
      evaluate(L(category="AI/ML/Data"), Prefs(categories=["software"])).startswith("category"))
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
check("work at a startup is account-gated", ats.host_kind("https://www.workatastartup.com/jobs/95003") in ats.NEEDS_ACCOUNT)
check("tiktok careers is account-gated", ats.host_kind("https://lifeattiktok.com/search/123") in ats.NEEDS_ACCOUNT)
check("a plain careers site is 'other'", ats.host_kind("https://careers.example.com/jobs/123") == "other")

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
        L(id="tt", company_name="Tock", url="https://careers.tock.example/jobs/1", date_posted=700),          # other
        L(id="done-1", company_name="Done Co", url="https://jobs.lever.co/doneco/1"),                                                              # attempted, applied
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
    _worn = RunState(path=Path(tempfile.mkdtemp()) / "s.json",
                     done={"w1": {"status": "needs_review", "company": "Wall Co", "attempts": 3, "detail": "no application form was found on this page"},
                           "w2": {"status": "needs_review", "company": "Fresh Co", "attempts": 2, "detail": "no application form was found on this page"},
                           "w3": {"status": "error", "company": "Blip Co", "attempts": 5, "detail": "could not load posting"},
                           "w4": {"status": "needs_review", "company": "Queued Co", "attempts": 4, "detail": "re-queued by hand"}})
    _worn_listings = [L(id="w1", company_name="Wall Co", url="https://jobs.lever.co/wall/1"),
                      L(id="w2", company_name="Fresh Co", url="https://jobs.lever.co/fresh/1"),
                      L(id="w3", company_name="Blip Co", url="https://jobs.lever.co/blip/1"),
                      L(id="w4", company_name="Queued Co", url="https://jobs.lever.co/queued/1")]
    _worn_sel, _worn_why = select(_worn_listings, P, _worn)
    _worn_ids = {e.id for e in _worn_sel}
    check("retry cap: three needs_review attempts leave the deal", "w1" not in _worn_ids and _worn_why.get("needs review, three attempts") == 1)
    check("retry cap: two attempts still retried", "w2" in _worn_ids)
    check("retry cap: errors keep retrying", "w3" in _worn_ids)
    check("retry cap: a hand re-queue is exempt", "w4" in _worn_ids)
    import datetime as _dtm
    _recent = (_dtm.datetime.now().astimezone() - _dtm.timedelta(days=2)).isoformat()
    _cool = RunState(path=Path(tempfile.mkdtemp()) / "s.json",
                     done={"a1": {"status": "applied", "company": "Amex Co", "when": _recent, "attempts": 1}})
    _cool_listings = [L(id="a1", company_name="Amex Co", url="https://jobs.lever.co/amex/1?utm_source=x"),
                      L(id="a2", company_name="Amex Co", url="https://jobs.lever.co/amex/1?utm_source=y"),
                      L(id="a3", company_name="Amex Co", url="https://jobs.lever.co/amex/2"),
                      L(id="b1", company_name="Other Co", url="https://jobs.lever.co/other/1")]
    _cool_prefs = Prefs(positions=["Software Engineer Intern"], apply_once_at_company=False, company_cooldown_days=7)
    _cool_sel, _cool_why = select(_cool_listings, _cool_prefs, _cool)
    _cool_ids = {e.id for e in _cool_sel}
    check("dedupe: the same link under another tracking tag is not a new posting", "a2" not in _cool_ids and _cool_why.get("same link already attempted", 0) >= 1)
    check("cooldown: a company applied to this week rests", "a3" not in _cool_ids and _cool_why.get("applied at this company within 7 days", 0) >= 1)
    check("cooldown: other companies are unaffected", "b1" in _cool_ids)


# --- the AI/ML/Data category: engineering titles in, quant and research out ---
from resume_tailor.discover import evaluate as _eval, Prefs as _Prefs
def _l(title, category):
    return {"id": "x", "url": "https://boards.greenhouse.io/x/jobs/1", "company_name": "X", "title": title,
            "locations": ["Durham, NC"], "date_posted": 1700000000, "terms": ["Summer 2027"], "category": category,
            "active": True, "is_visible": True, "degrees": []}
check("ai category: a machine learning engineer intern is worth reading", _eval(_l("Machine Learning Engineer Intern", "AI/ML/Data"), _Prefs()) == "")
check("ai category: a data science intern is worth reading", _eval(_l("Data Science Intern - Summer 2027", "AI/ML/Data"), _Prefs()) == "")
check("ai category: a quant title stays out", _eval(_l("Quantitative Researcher Intern", "AI/ML/Data"), _Prefs()).startswith("title"))
check("ai category: a marketing analytics title stays out", _eval(_l("Marketing Data Intern", "AI/ML/Data"), _Prefs()).startswith("title"))
check("ai category: hardware is still not read", _eval(_l("Hardware Engineer Intern", "Hardware Engineering"), _Prefs()).startswith("category"))
check("ai category: a profile can narrow the categories", _eval(_l("Machine Learning Engineer Intern", "AI/ML/Data"), _Prefs(categories=["software"])).startswith("category"))

# --- sharding: every company on one worker; every listing on some worker ---
from resume_tailor.discover import shard_of, select as _select, Prefs as _Prefs
_pool = [{"id": f"s{i}", "url": f"https://boards.greenhouse.io/x/jobs/{i}", "company_name": c, "title": "Software Engineer Intern",
          "locations": ["Durham, NC"], "date_posted": 1700000000 + i, "terms": ["Summer 2027"], "category": "Software Engineering",
          "active": True, "is_visible": True, "degrees": []}
         for i, c in enumerate(["Acme", "Acme Inc.", "The Acme Company", "Beta Corp", "Gamma", "Delta Labs", "Epsilon", "Zeta", "Eta", "Theta"])]
check("shard: a company's spellings land on one worker",
      len({shard_of(l, 4) for l in _pool if "acme" in l["company_name"].lower()}) == 1)
_by_shard = {k: {e.id for e in _select(_pool, _Prefs(apply_once_at_company=False), None, shard=(k, 4))[0]} for k in range(4)}
check("shard: the four workers' slices are disjoint", all(not (_by_shard[a] & _by_shard[b]) for a in range(4) for b in range(4) if a < b))
check("shard: the four workers' slices cover the pool", set().union(*_by_shard.values()) == {l["id"] for l in _pool})
check("shard: an unsharded select is the whole pool", len(_select(_pool, _Prefs(apply_once_at_company=False), None)[0]) == len(_pool))
_excl = _select(_pool, _Prefs(apply_once_at_company=False), None, shard=(0, 4))[1]
check("shard: the rest are counted as other workers' companies", _excl.get("other workers' companies", 0) == len(_pool) - len(_by_shard[0]))

# --- the Software category vouches for plain titles; fit orders the pool; login walls stop retrying ---
from resume_tailor.discover import _worn_out as _worn, LOGIN_RETRY_CAP as _LCAP
check("gate: Software category vouches for 'Technology Intern'", evaluate(L(title="Technology Intern"), P) == "")
check("gate: Software category vouches for 'Computer Science Intern'", evaluate(L(title="Computer Science Intern"), P) == "")
check("gate: a Software-category operations title still stays out", evaluate(L(title="Customer Operations Intern"), P) == "title: not software")

_fit = [L(id="ds", company_name="DataCo", title="Data Science Intern", category="AI/ML/Data", date_posted=900),
        L(id="ba", company_name="BizCo", title="Business Analyst Intern", category="Analyst", date_posted=800),
        L(id="swe", company_name="SoftCo", title="Software Engineer Intern", date_posted=100)]
_order = [e.id for e in select(_fit, Prefs(apply_once_at_company=False), None)[0]]
check("order: software roles lead, analyst next, AI/ML/Data last, however new", _order == ["swe", "ba", "ds"], str(_order))
check("gate: the AI/ML/Data category vouches for its own titles", evaluate(L(title="Research Scientist Intern", category="AI/ML/Data"), P) == "")
check("gate: a quant title in the AI/ML/Data category still stays out",
      evaluate(L(title="Quantitative Research Intern", category="AI/ML/Data"), P) == "title: not an engineering role")
check("gate: the Product category admits product management interns", evaluate(L(title="Product Management Intern", category="Product"), P) == "")
check("gate: the Product category admits product manager interns", evaluate(L(title="Product Manager Intern - Summer 2027", category="Product"), P) == "")
check("gate: a product specialist stays out", evaluate(L(title="Product Specialist Intern", category="Product"), P) == "title: not an engineering role")
check("gate: a profile can leave Product out", evaluate(L(title="Product Manager Intern", category="Product"), Prefs(categories=["software"])) == "category: Product")
_pm = [L(id="pm", company_name="PmCo", title="Product Manager Intern", category="Product", date_posted=950)] + _fit
check("order: product comes after AI/ML/Data", [e.id for e in select(_pm, Prefs(apply_once_at_company=False), None)[0]] == ["swe", "ba", "ds", "pm"])
import os as _os
_os.environ["RESUME_TAILOR_APPLY_ONCE_AT_COMPANY"] = "0"
try:
    from resume_tailor.profile import apply_once_at_company as _once
    check("apply once: the environment can switch the rule off", _once({"search": {"apply_once_at_company": True}}) is False)
    _os.environ["RESUME_TAILOR_APPLY_ONCE_AT_COMPANY"] = "1"
    check("apply once: or on", _once({"search": {"apply_once_at_company": False}}) is True)
    del _os.environ["RESUME_TAILOR_APPLY_ONCE_AT_COMPANY"]
    check("apply once: unset, the profile decides (on unless set)", _once({}) is True and _once({"search": {"apply_once_at_company": False}}) is False)
finally:
    _os.environ.pop("RESUME_TAILOR_APPLY_ONCE_AT_COMPANY", None)
_held = RunState(path=Path(tempfile.mkdtemp()) / "state.json")
_held.record("h1", {"status": "fit_rejected", "company": "Shure"}); _held.record("h2", {"status": "fit_rejected", "company": "Shure"})
_third = [L(id="h3", company_name="Shure", title="Cloud Software Engineer Intern")]
check("held twice: blocks the company only under the one-per-company rule",
      [e.id for e in select(_third, Prefs(apply_once_at_company=True), _held)[0]] == []
      and [e.id for e in select(_third, Prefs(apply_once_at_company=False), _held)[0]] == ["h3"])
_days = [L(id="old-swe", date_posted=100),
         L(id="new-pm", company_name="PmCo", title="Product Manager Intern", category="Product", date_posted=100 + 3 * 86400)]
check("order: a newer day goes first whatever the fit", [e.id for e in select(_days, Prefs(apply_once_at_company=False), None)[0]] == ["new-pm", "old-swe"])
_same = RunState(path=Path(tempfile.mkdtemp()) / "state.json")
_same.record("a1", {"status": "applied", "company": "Acme"})
_twice = [L(id="a1", url="https://job-boards.greenhouse.io/acme/jobs/9"), L(id="a2", url="https://job-boards.greenhouse.io/acme/jobs/9")]
_ids, _why = select(_twice, Prefs(apply_once_at_company=False), _same)
check("same link: a second id for an attempted link is not attempted again", [e.id for e in _ids] == [] and _why.get("same link already attempted") == 1)
_dup = RunState(path=Path(tempfile.mkdtemp()) / "state.json")
_dup.record("sn1", {"status": "applied", "company": "Sierra Nevada", "role": "Software Engineer Intern"})
_pair = [L(id="sn1", company_name="Sierra Nevada", url="https://sn.wd1.myworkdayjobs.com/a"),
         L(id="sn2", company_name="Sierra Nevada", url="https://sn.wd1.myworkdayjobs.com/b")]
check("same title: a second posting with the same title is attempted when the per-company rule is off",
      [e.id for e in select(_pair, Prefs(apply_once_at_company=False), _dup)[0]] == ["sn2"]
      and [e.id for e in select(_pair, Prefs(apply_once_at_company=True), _dup)[0]] == [])
_wall = RunState(path=Path(tempfile.mkdtemp()) / "state.json")
for _ in range(_LCAP + 1):
    _wall.record("wall", {"status": "needs_login", "detail": "this site wants an account or a sign-in"})
check("retry cap: a login wall is parked after repeated attempts", _worn(_wall.done["wall"]))
check("retry cap: a login wall is left out of the pool",
      [e.id for e in select([L(id="wall", company_name="Wall Co", url="https://wall.wd1.myworkdayjobs.com/j")], P, _wall)[0]] == [])
_wall.record("wall", {"status": "needs_login", "detail": "re-queued by hand"})
check("retry cap: a hand re-queue lifts it", not _worn(_wall.done["wall"]))
_once = RunState(path=Path(tempfile.mkdtemp()) / "state.json")
_once.record("wall1", {"status": "needs_login", "detail": "this site wants an account"})
check("retry cap: one login wall is still retried", not _worn(_once.done["wall1"]))

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
