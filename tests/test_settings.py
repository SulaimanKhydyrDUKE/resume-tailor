"""The Settings tab's writes, offline: the env file edited in place, YAML
saved with its comments, the sources setting, the basics fanned out to three
files, the skeleton save that links roles to the record, and the résumé
intake's text extraction and numeral check. No server, no model, no network."""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml

from resume_tailor import settings as S
from resume_tailor.discover import sources_from_profile

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, cond, detail))


root = Path(tempfile.mkdtemp())

# --- the env file --------------------------------------------------------------
(root / "env").write_text(
    "# resume-tailor credentials\n"
    "RESUME_TAILOR_PROVIDER=openai          # openai | anthropic\n"
    "OPENAI_API_KEY=sk-live-1234567890abcdef\n"
    "# ANTHROPIC_API_KEY=\n"
    "RESUME_TAILOR_MODEL=gpt-5.2\n"
    "RESUME_TAILOR_AUDIT_MODEL=PASTE-YOUR-KEY-HERE\n")
view = S.env_view(root)
f = {x["key"]: x for x in view["fields"]}
check("env view: a secret is reported set with its last four characters, never its value",
      f["OPENAI_API_KEY"]["set"] and f["OPENAI_API_KEY"]["hint"] == "cdef" and f["OPENAI_API_KEY"]["value"] == "" and "sk-live" not in json.dumps(view))
check("env view: a plain key shows its value; a placeholder reads as unset",
      f["RESUME_TAILOR_MODEL"]["value"] == "gpt-5.2" and not f["RESUME_TAILOR_AUDIT_MODEL"]["set"])
S.write_env({"RESUME_TAILOR_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-abcdefghijkl", "RESUME_TAILOR_MODEL": None,
             "RESUME_TAILOR_JUDGE_MODEL": "claude-sonnet-5"}, root)
text = (root / "env").read_text()
check("env write: the value changes in place and the trailing comment stays", "RESUME_TAILOR_PROVIDER=anthropic          # openai | anthropic" in text)
check("env write: a commented-out key comes back to life on its own line", "ANTHROPIC_API_KEY=sk-ant-abcdefghijkl" in text and "# ANTHROPIC_API_KEY=" not in text)
check("env write: clearing comments the line out rather than deleting it", "# RESUME_TAILOR_MODEL=gpt-5.2" in text)
check("env write: a new key is appended under a dashboard header", text.rstrip().endswith("# set from the dashboard\nRESUME_TAILOR_JUDGE_MODEL=claude-sonnet-5"))
check("env write: the first comment line and the untouched key survive", text.startswith("# resume-tailor credentials\n") and "OPENAI_API_KEY=sk-live-1234567890abcdef" in text)
check("env write: mode 600", oct((root / "env").stat().st_mode & 0o777) == "0o600")
check("env read: what llm._load_env_file would see", S.read_env(root)["RESUME_TAILOR_PROVIDER"] == "anthropic" and "RESUME_TAILOR_MODEL" not in S.read_env(root))
try:
    S.write_env({"bad key": "x"}, root)
    check("env write: refuses a name that is not a variable", False)
except ValueError:
    check("env write: refuses a name that is not a variable", True)

# --- YAML round trip: comments kept, strings PyYAML would misread quoted ----------
(root / "answers.yaml").write_text(
    "# the answer bank\n"
    "education:\n"
    "  graduation_date: \"May 2028\"   # month and year\n"
    "  gpa: \"3.42\"\n"
    "  currently_enrolled: \"Yes\"\n"
    "consents:\n"
    "  email_updates: \"Yes\"\n"
    "search:\n"
    "  terms: [\"Summer 2027\"]\n"
    "  positions:\n"
    "    - Software Engineer Intern\n"
    "  min_judge_score: 90\n"
    "  apply_below_bar: false\n")
S.save_answers_section("education", {"graduation_date": "May 2029", "gpa": "3.5", "currently_enrolled": "No", "graduation_year": "2029", "sat_score": "1560"}, [], root)
text = (root / "answers.yaml").read_text()
check("yaml save: the header comment and the inline comment survive", text.startswith("# the answer bank\n") and "# month and year" in text)
check("yaml save: a new key and the new values are there", "graduation_year" in text and "May 2029" in text)
back = yaml.safe_load(text)
check("yaml save: PyYAML reads the year, the GPA, the score and the No back as strings",
      back["education"]["graduation_year"] == "2029" and back["education"]["gpa"] == "3.5" and back["education"]["sat_score"] == "1560"
      and back["education"]["currently_enrolled"] == "No", str(back["education"]))
check("yaml save: a section not saved is untouched", back["consents"] == {"email_updates": "Yes"})
check("yaml save: a backup was written first", any(p.name.startswith("answers.yaml.bak-") for p in root.iterdir()))
S.save_answers_section("search", {"terms": ["Summer 2027", "Fall 2027"], "positions": ["Backend Intern"], "min_judge_score": {"hiring manager": 80, "screener": 85},
                                  "apply_below_bar": True, "sources": {"jobright-ba": False}, "extra_sources": [["mine", "https://raw.githubusercontent.com/x/y/main/README.md"]]}, [], root)
text = (root / "answers.yaml").read_text()
back = yaml.safe_load(text)
check("yaml save: a flow list stays on one line and keeps its new items", "terms: [Summer 2027, Fall 2027]" in text, text)
check("yaml save: a number becomes a map, a bool flips, lists of pairs land", back["search"]["min_judge_score"] == {"hiring manager": 80, "screener": 85}
      and back["search"]["apply_below_bar"] is True and back["search"]["extra_sources"] == [["mine", "https://raw.githubusercontent.com/x/y/main/README.md"]], str(back["search"]))
check("yaml save: writing the same content again writes nothing", S.save_rt(root / "answers.yaml", S.load_rt(root / "answers.yaml")) is False)
check("scalar: the strings PyYAML would turn into something else are quoted, plain phrases are not",
      all(type(S.scalar(v)).__name__ == "DoubleQuotedScalarString" for v in ("Yes", "no", "2028", "3.42", "1e3", "on", "", " x", "a: b", "$35"))
      and all(isinstance(S.scalar(v), str) and type(S.scalar(v)) is str for v in ("May 2028", "Software Engineer Intern", "Decline to self-identify")))

# --- the sources setting, as discovery reads it -----------------------------------
feed, tables = sources_from_profile(back)
check("sources: a built-in switched off is left out; the owner's list is added; the feed stays on",
      feed and [n for n, _ in tables] == ["jobright-swe", "speedyapply", "vanshb03", "mine"], str(tables))
check("sources: no setting at all means every built-in", sources_from_profile({}) == (True, __import__("resume_tailor.discover", fromlist=["TABLE_SOURCES"]).TABLE_SOURCES))
check("sources: the feed can be switched off", sources_from_profile({"search": {"sources": {"simplify": False}}})[0] is False)
out = root / "out"
out.mkdir()
(out / "discover-state.json").write_text(json.dumps({"count": 1000, "table_sources": {"jobright-swe": 100, "speedyapply": 300}}))
sv = S.sources_view(back, out)
check("sources view: counts per list, the feed's by subtraction, the off switch and the extra list shown",
      [s["name"] for s in sv] == ["simplify", "jobright-swe", "jobright-ba", "speedyapply", "vanshb03", "mine"] and sv[0]["count"] == 600
      and sv[2]["enabled"] is False and sv[3]["count"] == 300 and sv[5]["builtin"] is False, str(sv))

# --- the basics: one form, three files ---------------------------------------------
(root / "career.yaml").write_text(
    "# the record\n"
    "personal_information:\n"
    "  name: \"Ada\"\n"
    "  surname: \"Lovelace\"\n"
    "  email: \"ada@example.edu\"   # on every form\n"
    "education_details:\n"
    "  - education_level: \"B.S.\"\n"
    "    institution: \"Duke University\"\n"
    "    field_of_study: \"Mathematics\"\n"
    "    year_of_completion: \"May 2028\"\n"
    "    coursework:\n"
    "      - Linear Algebra\n"
    "experience_details:\n"
    "  - position: \"Intern\"\n"
    "    company: \"Acme Corp\"\n"
    "    employment_period: \"05/2026 - 08/2026\"\n"
    "    key_responsibilities:\n"
    "      - responsibility: \"Built the thing\"\n")
(root / "resume").mkdir()
(root / "resume" / "base.yaml").write_text(
    "# the skeleton\n"
    "file_name: Ada_Lovelace_resume.pdf\n"
    "style: jake\n"
    "education:\n"
    "  degree: B.S. in Mathematics\n"
    "  graduation: May 2028\n"
    "  # gpa: off by request\n"
    "roles:\n"
    "  - id: exp0\n"
    "    title: Intern\n"
    "    org: Acme\n"
    "    base:\n"
    "      - \"Built the thing for 20,000 users\"\n"
    "projects: []\n"
    "skills:\n"
    "  - label: Languages\n"
    "    terms: Python\n"
    "honors: []\n")
b = S.basics_view(root)
check("basics view: name, school, graduation, GPA and the résumé's GPA switch",
      b["personal"]["name"] == "Ada" and b["education"]["institution"] == "Duke University" and b["education"]["year_of_completion"] == "May 2028"
      and b["gpa"] == "3.5" and b["show_gpa_on_resume"] is False, str(b))
b["personal"]["email"] = "ada@duke.edu"
b["education"]["year_of_completion"] = "December 2029"
b["education"]["coursework"] = ["Linear Algebra", "Operating Systems"]
b["gpa"] = "3.7"
b["show_gpa_on_resume"] = True
res = S.save_basics(b, root)
career = yaml.safe_load((root / "career.yaml").read_text())
answers = yaml.safe_load((root / "answers.yaml").read_text())
base = yaml.safe_load((root / "resume" / "base.yaml").read_text())
check("basics save: all three files written", sorted(res["saved"]) == ["answers.yaml", "career.yaml", "resume/base.yaml"], str(res))
check("basics save: the record's e-mail, graduation and coursework changed, comment kept",
      career["personal_information"]["email"] == "ada@duke.edu" and career["education_details"][0]["year_of_completion"] == "December 2029"
      and career["education_details"][0]["coursework"] == ["Linear Algebra", "Operating Systems"] and "# on every form" in (root / "career.yaml").read_text())
check("basics save: every graduation spelling forms ask for, from one field",
      answers["education"]["graduation_date"] == "December 2029" and answers["education"]["graduation_year"] == "2029"
      and answers["education"]["expected_graduation"] == "December 2029" and answers["education"]["graduation_month_and_year"] == "December 2029", str(answers["education"]))
check("basics save: the GPA reaches both bank keys and the résumé line when switched on",
      answers["education"]["gpa"] == "3.7" and answers["education"]["cumulative_gpa"] == "3.7" and base["education"]["gpa"] == "3.7" and base["education"]["graduation"] == "December 2029")
b["show_gpa_on_resume"] = False
S.save_basics(b, root)
base = yaml.safe_load((root / "resume" / "base.yaml").read_text())
check("basics save: switching the résumé GPA off removes the key and keeps the bank's number",
      "gpa" not in base["education"] and yaml.safe_load((root / "answers.yaml").read_text())["education"]["gpa"] == "3.7"
      and "# gpa: off by request" in (root / "resume" / "base.yaml").read_text())
check("graduation parts", S.graduation_parts("May 2028") == ("May", "2028") and S.graduation_parts("05/2028") == ("May", "2028")
      and S.graduation_parts("2028") == ("", "2028") and S.graduation_parts("Dec. 2027") == ("December", "2027"))

# --- the skeleton: saved with its comments, a new role linked into the record -------------
sk = S.skeleton_view(root)
check("skeleton view: the file's roles and the record's entries to match them to",
      sk["skeleton"]["roles"][0]["id"] == "exp0" and sk["career_roles"][0]["id"] == "exp0" and "jake" in sk["styles"] and sk["skeleton"]["header"]["phone"] == "", str(sk)[:300])
skel = sk["skeleton"]
skel["roles"].append({"id": "", "title": "Research Assistant", "org": "Some Lab", "location": "Durham, NC", "dates": "2027", "base": ["Did research on 3 things", ""]})
skel["projects"] = [{"name": "Noted", "tech": "Next.js", "dates": "2026", "link": "", "bullets": ["Co-built a note platform", " "]}]
skel["honors"] = ["First place, something"]
skel["education"]["gpa"] = ""
res = S.save_skeleton(skel, root)
career = yaml.safe_load((root / "career.yaml").read_text())
base = yaml.safe_load((root / "resume" / "base.yaml").read_text())
check("skeleton save: the new role was appended to career.yaml with its bullets as responsibilities and got exp1",
      res["added_roles"] == ["exp1"] and career["experience_details"][1]["company"] == "Some Lab"
      and career["experience_details"][1]["key_responsibilities"] == [{"responsibility": "Did research on 3 things"}], str(res))
check("skeleton save: base.yaml holds both roles, the project without its blank bullet, the honor; the header comment stays",
      [r["id"] for r in base["roles"]] == ["exp0", "exp1"] and base["projects"][0]["bullets"] == ["Co-built a note platform"]
      and base["honors"] == ["First place, something"] and (root / "resume" / "base.yaml").read_text().startswith("# the skeleton\n"), str(base))
check("skeleton save: an empty GPA does not print", "gpa" not in base["education"])
from resume_tailor.profile import Profile
prof = Profile.load(root)
idx = prof.evidence_index()
check("skeleton save: the evidence index now cites the new role's skeleton bullet", idx.get("exp1.b0", "").endswith("Did research on 3 things"), str(sorted(idx)))

# --- intake: text from files, the numeral check, role matching ---------------------------
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Ada Lovelace</w:t></w:r></w:p><w:p><w:r><w:t>Built 20,000 things &amp; more</w:t></w:r></w:p></w:body></w:document>")
check("extract: a .docx gives its paragraphs, one per line, entities unescaped", S.extract_text("cv.docx", buf.getvalue()).strip() == "Ada Lovelace\nBuilt 20,000 things & more")
check("extract: a .tex loses its commands and keeps the words",
      "Built the thing" in S.extract_text("cv.tex", b"\\section{Experience}\n\\item Built the thing % comment\n\\textbf{Intern}") and "textbf" not in S.extract_text("cv.tex", b"\\textbf{Intern}"))
draft = {"roles": [{"title": "Intern", "org": "Acme Corporation", "dates": "05/2026", "bullets": ["Built the thing for 20,000 users", "Cut latency 40%"]}],
         "projects": [{"bullets": ["Shipped 3 features"]}], "honors": [], "education": {"gpa": "3.42", "graduation": "May 2028"}}
flags = S.numeral_flags(draft, "Intern, Acme Corporation, 05/2026\nBuilt the thing for 20,000 users\nShipped 3 features\nGPA 3.42 · May 2028")
check("numeral flags: only the bullet whose number the file lacks is flagged", len(flags) == 1 and flags[0].startswith("role 1 bullet 2: 40"), str(flags))
check("match roles: the drafted role finds the record entry by employer", S.match_roles(draft["roles"], career) == ["exp0"])
check("match roles: an unknown employer gets no id", S.match_roles([{"title": "X", "org": "Nowhere Ltd"}], career) == [""])
sk2 = S.draft_to_skeleton({"header": {"name": "Ada", "phone": "1", "email": "", "location": "", "linkedin": "", "github": "", "website": ""},
                           "education": {"school": "Duke", "degree": "B.S.", "location": "", "graduation": "May 2028", "gpa": "", "coursework": "", "lines": []},
                           "roles": draft["roles"], "projects": [], "skills": [], "honors": []}, career)
check("draft to skeleton: base.yaml's shape with the matched id and the file's bullets", sk2["roles"][0]["id"] == "exp0" and sk2["roles"][0]["base"][1] == "Cut latency 40%" and sk2["name"] == "Ada")
try:
    S.save_upload("../evil.sh", b"x", root)
    check("upload: only résumé kinds are accepted, under the uploads folder", False)
except ValueError:
    dest = S.save_upload("../My Résumé (v2).pdf", b"%PDF-1.4", root)
    check("upload: only résumé kinds are accepted, under the uploads folder", dest.parent == root / "resume" / "uploads" and dest.name == "My-R-sum-v2-.pdf", dest.name)

# --- processes ------------------------------------------------------------------------
import os
os.environ["RESUME_TAILOR_MODEL"] = "stale"
os.environ["OPENAI_API_KEY"] = "stale"
os.environ["RESUME_TAILOR_OUTPUT"] = "keep"
env = S.child_env()
check("child env: a child reads the env file afresh — tool variables and keys are dropped, the file locations kept",
      "RESUME_TAILOR_MODEL" not in env and "OPENAI_API_KEY" not in env and env.get("RESUME_TAILOR_OUTPUT") == "keep" and "PATH" in env)
snap = S.workers_snapshot(root)
check("workers snapshot: no pid files means no workers and nothing stale", snap["workers"] == 0 and snap["stale"] == [] and snap["supervisor_pid"] is None)
(root / "watch-w0.pid").write_text(str(os.getpid()))
os.utime(root / "watch-w0.pid", (1, 1))
snap = S.workers_snapshot(root)
check("workers snapshot: files newer than the workers' start are named", snap["alive"] == 1 and set(snap["stale"]) == {"env", "answers.yaml", "career.yaml", "resume/base.yaml"}, str(snap))

# --- the Stop / Start buttons ---------------------------------------------------
import subprocess
import time

(root / "watch-w0.pid").unlink()
sup = subprocess.Popen(["sleep", "60"])           # stands in for the overnight supervisor
caf = subprocess.Popen(["sleep", "60"])           # and for its caffeinate
(root / "overnight.pid").write_text(str(sup.pid))
(root / "caffeinate.pid").write_text(str(caf.pid))
calls = []
res = S.stop_workers(root, stop_loop=lambda: calls.append("stop"))
for _ in range(40):
    if sup.poll() is not None and caf.poll() is not None:
        break
    time.sleep(0.05)
check("stop: the supervisor and its caffeinate are ended and their pid files dropped",
      sup.poll() is not None and caf.poll() is not None and not (root / "overnight.pid").exists() and not (root / "caffeinate.pid").exists(), str(res))
check("stop: the worker stop runs once, after the supervisor is gone, and the note says what stopped",
      calls == ["stop"] and res["supervisor"] is True and "the supervisor" in res["note"] and res["stopped"] == 0, str(res))
res = S.stop_workers(root, stop_loop=lambda: calls.append("stop"))
check("stop: with nothing running it is harmless", res["supervisor"] is False and calls == ["stop", "stop"] and res["stopped"] == 0, str(res))

(root / "overnight.pid").write_text(str(os.getpid()))
res = S.start_workers(out, root)
check("start: pressed while the supervisor lives, nothing is launched", res["started"] is False and "Already running" in res["note"], str(res))
(root / "overnight.pid").unlink()
(root / "watch-w0.pid").write_text(str(os.getpid()))
res = S.start_workers(out, root)
check("start: pressed while a worker lives, nothing is launched", res["started"] is False and "1 worker" in res["note"], str(res))
(root / "watch-w0.pid").unlink()

marker = root / "supervisor-ran"
(root / "overnight.sh").write_text(f'#!/bin/bash\necho $$ > "{root}/overnight.pid"\necho "up $RESUME_TAILOR_MODEL $OPENAI_API_KEY" > "{marker}"\n')
res = S.start_workers(out, root)
for _ in range(100):
    if marker.is_file():
        break
    time.sleep(0.05)
check("start: the supervisor script beside the profile files is launched detached", res["started"] is True and res["launcher"] == "supervisor" and marker.is_file(), str(res))
check("start: the supervisor reads the env file afresh — the tool variables of this process do not reach it",
      marker.is_file() and marker.read_text().strip() == "up", marker.read_text() if marker.is_file() else "no marker")
check("start: the supervisor wrote its own pid file and logs to overnight.log", (root / "overnight.pid").is_file() and (root / "overnight.log").is_file())
(root / "overnight.pid").unlink(); (root / "overnight.sh").unlink()

ran = []
real_run = S.subprocess.run
S.subprocess.run = lambda cmd, **kw: (ran.append(cmd), type("P", (), {"returncode": 0})())[1]
try:
    res = S.start_workers(out, root, workers=3)
finally:
    S.subprocess.run = real_run
check("start: without a supervisor script, `start` runs the asked number of workers and the fresh lane",
      res["started"] is True and res["launcher"] == "start" and len(ran) == 1 and ran[0][-5:] == ["--workers", "3", "--fresh", "--out", str(out)] and "resume_tailor.cli" in ran[0], str(ran))

# --- the launchd agent ----------------------------------------------------------
calls = []
real_launchctl, real_path = S._launchctl, S.launch_agent_path
S._launchctl = lambda *a: (calls.append(a), type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1]
S.launch_agent_path = lambda: root / "LaunchAgents" / "com.resume-tailor.loop.plist"
try:
    (root / "overnight.sh").write_text("#!/bin/bash\necho hi\n")
    res = S.autostart(True, out, root)
    plist = (root / "LaunchAgents" / "com.resume-tailor.loop.plist").read_text()
    check("autostart on: the agent file names the supervisor script, runs at login and is kept alive",
          res["autostart"] and str(root / "overnight.sh") in plist and "<key>RunAtLoad</key><true/>" in plist and "<key>KeepAlive</key><true/>" in plist, res.get("note"))
    check("autostart on: enabled and bootstrapped", calls[-2][0] == "enable" and calls[-1][0] == "bootstrap", str(calls))
    calls.clear()
    res = S.stop_workers(root, stop_loop=lambda: None)
    check("stop under launchd: the agent is booted out and disabled so it stays stopped", [c[0] for c in calls] == ["bootout", "disable"], str(calls))
    calls.clear()
    res = S.start_workers(out, root)
    check("start under launchd: the agent is enabled and bootstrapped, not the script run by hand",
          res["launcher"] == "launchd" and res["started"] and [c[0] for c in calls] == ["enable", "bootstrap"], str(res))
    calls.clear()
    res = S.autostart(False, out, root)
    check("autostart off: booted out and the file removed", not res["autostart"] and calls[0][0] == "bootout" and not (root / "LaunchAgents" / "com.resume-tailor.loop.plist").exists())
    (root / "overnight.sh").unlink()
    try:
        S.autostart(True, out, root); check("autostart on without a supervisor script is refused", False)
    except ValueError:
        check("autostart on without a supervisor script is refused", True)
finally:
    S._launchctl, S.launch_agent_path = real_launchctl, real_path

view = S.settings_view(out, root)
check("settings view: every part present, no secret in it", set(view) >= {"env", "sources", "answers", "basics", "resume", "workers"} and "sk-ant-abcdefghijkl" not in json.dumps(view))

width = max(len(n) for n, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
