"""Instagram stories as a source: the pure parts — unwrapping the redirect,
reading sticker addresses out of story JSON, a link-in-bio page, a posting's
facts, the store and the listings it yields."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from resume_tailor.igstories import (unwrap, links_from_story_json, links_from_html, posting_facts, is_wrapper,
                                     record_link, load_links, save_links, to_listings, listing_id, pages_from_profile)
from types import SimpleNamespace as NS

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))

check("unwrap: the address behind l.instagram.com",
      unwrap("https://l.instagram.com/?u=https%3A%2F%2Fjobs.lever.co%2Facme%2F123%3Futm_source%3Dig&e=AT1abc") == "https://jobs.lever.co/acme/123")
check("unwrap: tracking parameters go, real ones stay",
      unwrap("https://boards.greenhouse.io/x/jobs/1?gh_jid=1&utm_medium=story&fbclid=z") == "https://boards.greenhouse.io/x/jobs/1?gh_jid=1")
check("unwrap: a plain address is untouched", unwrap("https://careers.example.com/intern") == "https://careers.example.com/intern")
_wd = "https://fox.wd1.myworkdayjobs.com/en-US/Domestic/job/New-York-New-York-USA/Summer-2027-FOX-Technology-Internship-Program---New-York--NY_R50033968"
check("unwrap: a Workday apply link is the posting", unwrap(_wd + "/apply/useMyLastApplication") == _wd)
from resume_tailor.igstories import workday_facts
check("workday: title and location read off the address",
      workday_facts(_wd) == {"title": "Summer 2027 FOX Technology Internship Program - New York, NY", "location": "New York New York USA"})
check("workday: not a Workday address, nothing", workday_facts("https://jobs.lever.co/x/1") == {})
payload = {"reels_media": [{"items": [
    {"story_link_stickers": [{"story_link": {"url": "https://l.instagram.com/?u=https%3A%2F%2Fjobs.ashbyhq.com%2Fnetic%2Fabc"}}]},
    {"story_cta": [{"links": [{"webUri": "https://www.linkedin.com/jobs/view/4321"}]}]},
    {"story_link_stickers": [{"story_link": {"url": "https://www.instagram.com/p/xyz/"}}]}]}]}
links = links_from_story_json(payload)
check("story json: current and older sticker shapes both yield addresses, unwrapped",
      links == ["https://jobs.ashbyhq.com/netic/abc", "https://www.linkedin.com/jobs/view/4321"])
check("story json: an instagram address is not a posting", all("instagram.com" not in u for u in links))
html = '''<a href="https://www.instagram.com/page/">ig</a><a href="/privacy">p</a>
<a href="https://boards.greenhouse.io/acme/jobs/77?utm_source=linktree">Acme SWE intern</a>
<a href="https://l.instagram.com/?u=https%3A%2F%2Fjobs.lever.co%2Fbeta%2F9">Beta</a><a href="https://linktr.ee/page">self</a>'''
check("link page: outward posting links only, unwrapped and cleaned",
      links_from_html(html, "https://linktr.ee/page") == ["https://boards.greenhouse.io/acme/jobs/77", "https://jobs.lever.co/beta/9"])
check("wrapper hosts: linktree and bit.ly are followed, a job board is not",
      is_wrapper("https://linktr.ee/x") and is_wrapper("https://bit.ly/3x") and not is_wrapper("https://jobs.lever.co/x/1"))
ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"Software Engineer Intern","hiringOrganization":{"@type":"Organization","name":"Acme Robotics"}}</script>'
def _ct(f): return {"company": f["company"], "title": f["title"]}
check("facts: JSON-LD JobPosting gives company and title",
      _ct(posting_facts(ld, "https://boards.greenhouse.io/acmerobotics/jobs/1")) == {"company": "Acme Robotics", "title": "Software Engineer Intern"})
og = '<meta property="og:title" content="Data Intern - Summer 2027"><meta property="og:site_name" content="Beta Corp">'
check("facts: Open Graph when there is no JSON-LD", _ct(posting_facts(og, "https://x.com/j")) == {"company": "Beta Corp", "title": "Data Intern - Summer 2027"})
check("facts: the page title split on a dash, company from the tail",
      _ct(posting_facts("<title>ML Intern &ndash; Gamma Labs</title>", "https://gamma.com/j")) == {"company": "Gamma Labs", "title": "ML Intern"})
check("facts: a hyphen inside a word is not a company separator",
      posting_facts('<meta property="og:title" content="Field-Deployed Software Engineering Intern">', "https://job-boards.greenhouse.io/gitai/jobs/1")["title"] == "Field-Deployed Software Engineering Intern")
check("facts: careerpuck's board slug names the company, not 'App'",
      posting_facts("<title>Job</title>", "https://app.careerpuck.com/job-board/lyft/job/88?gh_jid=88")["company"] == "Lyft")
check("facts: a company guessed off the host is marked as such",
      posting_facts("<title>Job</title>", "https://app.careerpuck.com/job-board/lyft/job/88")["company_from_host"] is True)
check("facts: the Greenhouse board name stands in for a nameless page",
      posting_facts("<title>Job Application</title>", "https://boards.greenhouse.io/deltaco/jobs/5")["company"] == "Deltaco")
check("facts: a JSON-LD company is not marked as guessed", posting_facts(ld, "https://x.com/j").get("company_from_host") is False)
d = Path(tempfile.mkdtemp())
data = load_links(d)
check("store: new address recorded", record_link(data, "ducksjobs", "https://l.instagram.com/?u=https%3A%2F%2Fjobs.lever.co%2Facme%2F1", label="Apply", frame=2, via="href", facts={"company": "Acme", "title": "SWE Intern"}))
check("store: the same address again is not new", not record_link(data, "ducksjobs", "https://jobs.lever.co/acme/1"))
record_link(data, "ducksjobs", "https://linktr.ee/somepage", via="wire")
record_link(data, "ducksjobs", "https://careers.zeta.com/roles/9", via="click", facts={"company": "Zeta", "title": "Platform Engineer"})
save_links(d, data)
ls = to_listings(d)
check("listings: one per posting, link pages left out", [l["url"] for l in ls] == ["https://jobs.lever.co/acme/1", "https://careers.zeta.com/roles/9"])
check("listings: source names the page", ls[0]["source"] == "instagram:ducksjobs" and ls[0]["company_name"] == "Acme" and ls[0]["title"] == "SWE Intern")
record_link(data, "added", "https://job-boards.greenhouse.io/gitai/jobs/5", via="hand", facts={"company": "Gitai", "title": "Field-Deployed Software Engineering Intern", "location": "Los Angeles, CA"})
save_links(d, data)
added = next(l for l in to_listings(d) if "gitai" in l["url"])
check("listings: a by-hand link is sourced 'added by hand' with its location", added["source"] == "added by hand" and added["locations"] == ["Los Angeles, CA"])
from resume_tailor.discover import Prefs as _P, evaluate as _ev
check("discovery: a by-hand posting skips the title gate", _ev(dict(added, title="Applied Scientist Intern"), _P(positions=["Software Engineer Intern"])) == "")
check("discovery: but not the company blacklist", _ev(dict(added, company_name="Palantir"), _P(company_blacklist=["Palantir"])) == "company blacklist")
check("discovery: an Instagram feed listing still meets the title gate", _ev(dict(added, source="instagram:x", title="Applied Scientist Intern", category="Software Engineering"), _P()) != "")
check("listings: a title without 'intern' still reads as an internship for the filter", "Internship" in ls[1]["title"])
check("listings: ids are stable", ls[0]["id"] == listing_id("https://jobs.lever.co/acme/1?utm_source=x") and ls[0]["id"].startswith("ig:"))
check("listings: the listing has what discovery expects", all(k in ls[0] for k in ("id", "url", "company_name", "title", "locations", "date_posted", "source", "active", "terms", "category")))
check("pages: handles come from search.instagram_pages, @ and urls stripped",
      pages_from_profile(NS(answers={"search": {"instagram_pages": ["@ducksjobs", "https://www.instagram.com/internships.daily/"]}})) == ["ducksjobs", "internships.daily"])
check("pages: none configured is an empty list", pages_from_profile(None) == [])

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
