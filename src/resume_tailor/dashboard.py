"""A window on the loop: what was attempted, what came of it, and what was sent.

`resume-tailor dashboard` serves a single-page app (React, shipped with the
package) and a small JSON API over the files the watch already writes —
batch-state.json, the listings cache, the PDFs and screenshots under output/ —
so nothing runs twice and nothing is copied. Local only: it binds to
127.0.0.1. The reads never write; the POSTs do exactly what a button says —
open a posting in Chrome, record a status by hand — and the Settings tab
edits the files under ~/.resume-tailor through settings.py, which is the
one place the tool writes its own configuration.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .profile import DEFAULT_PROFILE_DIR
from .settings import child_env

STATIC = Path(__file__).resolve().parent / "static" / "dashboard.html"
_SHOT_KINDS = ("post-submit", "pre-submit", "needs-review", "dry-run")


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s)[:60].strip("-") or "entry"


def _file_url(out_dir: Path, path: str | None) -> str | None:
    """A file under the output directory, as the URL this server serves it at."""
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(out_dir.resolve())
    except ValueError:
        return None
    return "/files/" + rel.as_posix()


def _alive(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def watch_status(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    pid_file, log = profile_dir / "watch.pid", profile_dir / "watch.log"
    # One pid file per worker (watch-w0.pid …) when the loop runs as several;
    # the plain watch.pid is worker 0 either way.
    worker_pids = {p.name: _alive(p) for p in sorted(profile_dir.glob("watch-w*.pid"))}
    if pid_file.is_file() and "watch-w0.pid" not in worker_pids:
        worker_pids["watch.pid"] = _alive(pid_file)
    up = [p for p in worker_pids.values() if p]
    pid, running = (up[0] if up else None), bool(up)
    tail: list[str] = []
    last_pass = None
    worker_logs: dict[str, list[str]] = {}
    if log.is_file():
        lines = log.read_text(errors="replace").splitlines()[-4000:]
        tail = lines[-15:]
        last_pass = next((line for line in reversed(lines) if re.match(r"^\[\d\d:\d\d\] ", line)), None)
        # The last few lines each worker wrote: its pass header, its queue,
        # its outcomes — the tag it prefixes ("[w2] ", "[fresh] ") says whose.
        for line in lines:
            m = re.match(r"^\s*\[(w\d+|fresh)\] ", line)
            if m:
                worker_logs.setdefault(m.group(1), []).append(line.strip())
        worker_logs = {k: v[-8:] for k, v in worker_logs.items()}
    # A retry pass (the overnight supervisor's first act) runs with the loop
    # deliberately stopped; the page should say so rather than "stopped".
    pass_running, pass_pid = False, None
    try:
        cur = json.loads((profile_dir / "current.json").read_text(encoding="utf-8"))
        cpid = int(cur.get("pid") or 0)
        if cpid and cur.get("stage") not in ("", "idle"):
            os.kill(cpid, 0)
            pass_running, pass_pid = (cpid != pid), cpid
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    workers_label = f" ({len(up)} of {len(worker_pids)} workers)" if len(worker_pids) > 1 else ""
    fresh_pid = _alive(profile_dir / "fresh.pid") if (profile_dir / "fresh.pid").is_file() else None
    # One row per process the supervisor runs: alive or not, by pid file.
    processes = []
    for name, p in sorted(worker_pids.items()):
        m = re.match(r"watch-w(\d+)\.pid", name)
        processes.append({"worker": m.group(1) if m else "0", "pid": p, "alive": bool(p)})
    if (profile_dir / "fresh.pid").is_file():
        processes.append({"worker": "fresh", "pid": fresh_pid, "alive": bool(fresh_pid)})
    return {"running": running, "pid": pid if running else None, "last_pass": last_pass, "log_tail": tail,
            "pass_running": pass_running, "pass_pid": pass_pid, "workers": len(up), "workers_expected": len(worker_pids),
            "processes": processes, "worker_logs": worker_logs, "fresh_running": bool(fresh_pid),
            "label": ("loop running" + workers_label + (" + fresh lane" if fresh_pid else "")) if running
                     else ("retry pass running (the loop resumes when it ends)" if pass_running else "loop stopped")}


def pool_status(out_dir: Path) -> dict:
    """How many discovered postings pass the filter and have not been
    attempted — what the loop still has in front of it — from the cache on
    disk, without fetching."""
    try:
        from .discover import Prefs, select
        from .profile import Profile
        from .queue import RunState

        cache_path = out_dir / "listings-cache.json"
        if not cache_path.is_file():
            return {}
        listings = json.loads(cache_path.read_text(encoding="utf-8"))
        prefs = Prefs.from_profile(Profile.load())
        state = RunState.load(out_dir / "batch-state.json")
        entries, _ = select(listings, prefs, state)
        new = [e for e in entries if e.id not in state.done]
        return {"cached": len(listings), "queued": len(entries), "new": len(new),
                "sources": {l.get("source") or "simplify" for l in listings} and
                           dict(Counter(l.get("source") or "simplify" for l in listings))}
    except Exception:
        return {}


def build_index(out_dir: str | Path, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    """Everything the page shows, from the files on disk right now."""
    out_dir = Path(out_dir)
    state_path = out_dir / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"done": {}}
    listings: dict[str, dict] = {}
    cache = out_dir / "listings-cache.json"
    if cache.is_file():
        for l in json.loads(cache.read_text(encoding="utf-8")):
            listings[l.get("id") or l.get("url") or ""] = l
    shots: dict[str, dict[str, str]] = {}
    shots_dir = out_dir / "screenshots"
    if shots_dir.is_dir():
        for p in shots_dir.glob("*.png"):
            for kind in _SHOT_KINDS:
                if p.stem.endswith("-" + kind):
                    shots.setdefault(p.stem[: -len(kind) - 1], {})[kind] = _file_url(out_dir, str(p))
    apps = []
    open_now = reviews_open()
    for eid, rec in (state.get("done") or {}).items():
        l = listings.get(eid, {})
        apps.append({
            "reviewing": open_now.get(_safe(eid)),
            "id": eid,
            "company": rec.get("company") or l.get("company_name") or "",
            "role": rec.get("role") or l.get("title") or "",
            "status": rec.get("status") or "?",
            "detail": rec.get("detail") or "",
            "fit": rec.get("fit") or "",
            "when": rec.get("when") or "",
            "url": l.get("url") or "",
            "locations": l.get("locations") or [],
            "date_posted": l.get("date_posted"),
            "pdf": _file_url(out_dir, rec.get("pdf")),
            "screenshots": shots.get(_safe(eid), {}),
            "answers": rec.get("answers") or [],
            "coverage": rec.get("coverage") or {},
            "worker": rec.get("worker") or "",
        })
    apps.sort(key=lambda a: a["when"], reverse=True)
    # What the inbox says came of each company, and the two flows drawn
    # from it (see mailscan.flows). Read from results.json; never scanned here.
    try:
        from .mailscan import flows, load_results
        from .queue import company_key
        flow_data = flows(out_dir)
        results = load_results(out_dir).get("companies") or {}
        for a in apps:
            co = results.get(company_key(a["company"]))
            a["stage"] = (co or {}).get("stage") or ""
            a["mail"] = sorted((co or {}).get("timeline") or [], key=lambda t: t.get("when") or "", reverse=True)[:12]
            a["addresses"] = (co or {}).get("addresses") or []
    except Exception as e:
        flow_data, results = {"error": str(e)}, {}
    # What each live process is doing: current.json for a lone loop or a
    # retry pass, current-w<k>.json per worker. `now` is the freshest of them.
    workers_now: list[dict] = []
    for current in sorted(profile_dir.glob("current*.json")):
        try:
            if time.time() - current.stat().st_mtime >= 1800:
                continue
            info = json.loads(current.read_text(encoding="utf-8"))
            if info.get("stage") == "idle":
                continue
            os.kill(int(info.get("pid") or 0), 0)  # the process that wrote it must still be there
            info["_mtime"] = current.stat().st_mtime
            workers_now.append(info)
        except Exception:
            continue
    workers_now.sort(key=lambda i: -i.get("_mtime", 0))
    for info in workers_now:
        info.pop("_mtime", None)
    now = workers_now[0] if workers_now else None
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "now": now,
        "workers_now": workers_now,
        "watch": watch_status(profile_dir),
        "pool": pool_status(out_dir),
        "counts": dict(Counter(a["status"] for a in apps)),
        "applied_companies": state.get("applied_companies") or [],
        "applications": apps,
        "flows": flow_data,
    }


REVIEWS_DIR = DEFAULT_PROFILE_DIR / "reviews"


_PORTAL_HOST = re.compile(r"jobright|careers?|jobs?|recruit|apply|talent|hire|workday|icims|greenhouse|lever|ashby|oracle|taleo|"
                          r"successfactors|smartrecruiters|avature|jibeapply|yello|workatastartup|bytedance|tiktok|ycombinator", re.I)


def portal_hosts(names: list[str], allowed: list[str] | None = None) -> list[str]:
    """The saved-login hosts that are job portals: an ATS the tool knows, a
    host the owner allows accounts on, or one whose name says careers.
    Ad-tech and analytics cookies (most of the directory) are left out."""
    from . import ats

    allowed = [str(a).strip().lower() for a in (allowed or []) if str(a).strip()]
    out = []
    for name in names:
        host = name[:-5] if name.endswith(".json") else name
        host = host.lower().strip(".")
        if not host or "." not in host:
            continue
        if ats.host_kind("https://" + host + "/") not in ("", "other") or any(host == a or host.endswith("." + a) or a in host for a in allowed) \
                or _PORTAL_HOST.search(host):
            if not re.search(r"doubleclick|googlesyndication|adsrvr|adnxs|criteo|taboola|outbrain|quantserve|scorecardresearch|"
                             r"demdex|bluekai|rubiconproject|pubmatic|openx|casalemedia|linkedin\.com$|facebook|twitter|youtube|google\.com$", host):
                out.append(host)
    return sorted(dict.fromkeys(out))


def build_files(out_dir: str | Path, logins_dir: Path | None = None, answers: dict | None = None) -> dict:
    """Every tailored résumé and generated document on disk, from the state
    file, newest first, and the portals with a saved sign-in. The site
    password is never in this response."""
    from .apply import ApplySession

    out_dir = Path(out_dir)
    state_path = out_dir / "batch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"done": {}}
    listings: dict[str, dict] = {}
    cache = out_dir / "listings-cache.json"
    if cache.is_file():
        try:
            for l in json.loads(cache.read_text(encoding="utf-8")):
                listings[l.get("id") or ""] = l
        except Exception:
            pass
    resumes: list[dict] = []
    seen: set[str] = set()
    for eid, rec in (state.get("done") or {}).items():
        pdf = rec.get("pdf") or ""
        if not pdf or pdf in seen or not Path(pdf).is_file():
            continue
        seen.add(pdf)
        docs_dir = Path(pdf).parent / "documents"
        documents = [{"name": d.name, "url": _file_url(out_dir, str(d))} for d in sorted(docs_dir.iterdir())] if docs_dir.is_dir() else []
        l = listings.get(eid, {})
        resumes.append({"id": eid, "company": rec.get("company") or l.get("company_name") or "", "role": rec.get("role") or l.get("title") or "",
                        "status": rec.get("status") or "?", "when": rec.get("when") or "", "fit": rec.get("fit") or "",
                        "url": l.get("url") or rec.get("url") or "", "pdf": _file_url(out_dir, pdf), "name": Path(pdf).name,
                        "folder": Path(pdf).parent.name, "documents": documents, "resume_version": rec.get("resume_version") or 0})
    resumes.sort(key=lambda r: r["when"], reverse=True)
    if answers is None:
        try:
            from .profile import Profile
            answers = Profile.load().answers
        except Exception:
            answers = {}
    allowed = list((answers.get("search") or {}).get("create_accounts_on") or [])
    ldir = logins_dir if logins_dir is not None else ApplySession.LOGINS_DIR
    names = [p.name for p in ldir.glob("*.json")] if ldir.is_dir() else []
    return {"resumes": resumes, "counts": {"resumes": len(resumes), "documents": sum(len(r["documents"]) for r in resumes)},
            "logins": {"hosts": portal_hosts(names, allowed), "saved_sites": len(names), "allowed": allowed}}


def reveal_logins() -> dict:
    """The site account's e-mail(s) and password, for signing in by hand.
    Read at the moment the page's Reveal button asks."""
    import os

    from .llm import _load_env_file
    from .profile import Profile

    _load_env_file()
    profile = Profile.load()
    email = str(profile.career.get("personal_information", {}).get("email") or "")
    flat = profile.flat_answers()
    others = sorted({str(v) for k, v in flat.items() if "email" in k.lower() and "@" in str(v)} - {email})
    return {"email": email, "other_emails": others, "site_password": os.environ.get("RESUME_TAILOR_SITE_PASSWORD") or "",
            "mailbox_user": os.environ.get("RESUME_TAILOR_IMAP_USER") or "",
            "note": "The site password is the one the tool used for every portal account it created; older accounts were made with the earlier e-mail."}


def open_for_review(out_dir: Path, entry_id: str) -> int:
    """Start `resume-tailor review <id>` detached: a visible Chrome window with
    the form filled in, left open for the user. Returns the process id and
    leaves a marker so the page can show which postings have a window open."""
    import subprocess
    import sys

    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DEFAULT_PROFILE_DIR / "review.log", "a", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, "-m", "resume_tailor.cli", "review", entry_id, "--out", str(out_dir)],
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=child_env())
    (REVIEWS_DIR / f"{_safe(entry_id)}.json").write_text(
        json.dumps({"pid": proc.pid, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    return proc.pid


def open_for_login(out_dir: Path, entry_id: str) -> int:
    """Start `resume-tailor login <id>` detached: a visible Chrome on the
    site's sign-in wall; after the user is through, the tool fills and
    submits in that window. Marked like a review window."""
    import subprocess
    import sys

    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DEFAULT_PROFILE_DIR / "login.log", "a", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, "-m", "resume_tailor.cli", "login", entry_id, "--out", str(out_dir)],
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=child_env())
    (REVIEWS_DIR / f"{_safe(entry_id)}.json").write_text(
        json.dumps({"pid": proc.pid, "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "login"}), encoding="utf-8")
    return proc.pid


def open_for_submit(out_dir: Path, entry_id: str) -> int:
    """Start `resume-tailor submit <id>` detached: the headless fill and the
    submit for a posting the user has just approved. Returns the process id."""
    import subprocess
    import sys

    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DEFAULT_PROFILE_DIR / "approve.log", "a", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, "-m", "resume_tailor.cli", "submit", entry_id, "--out", str(out_dir)],
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=child_env())
    return proc.pid


def reviews_open() -> dict[str, dict]:
    """Postings with a review window open right now: marker files whose
    process is still alive, keyed by the same safe id the markers use."""
    out: dict[str, dict] = {}
    if not REVIEWS_DIR.is_dir():
        return out
    for p in REVIEWS_DIR.glob("*.json"):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
            os.kill(int(info["pid"]), 0)
            out[p.stem] = info
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return out


def mark(out_dir: Path, entry_id: str, status: str) -> dict:
    """Record a status the user settled by hand — applied after finishing a
    form themselves, or skipped — on top of what the record already holds."""
    from .queue import RunState

    if status not in ("applied", "skipped", "needs_review", "by_hand"):
        raise ValueError(f"cannot mark a posting {status!r}")
    state = RunState.load(out_dir / "batch-state.json")
    rec = dict(state.done.get(entry_id) or {"entry_id": entry_id})
    rec.update({"status": status, "detail": f"marked {status} by hand from the dashboard",
                "when": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if status == "applied" and rec.get("company"):
        state.mark_applied(rec["company"])
    state.record(entry_id, rec)
    return rec


class _Handler(BaseHTTPRequestHandler):
    out_dir: Path = Path("output")

    def log_message(self, fmt, *args):  # quiet: the terminal is for the loop's output
        pass

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        try:
            if path.startswith("/api/review/"):
                pid = open_for_review(self.out_dir, path[len("/api/review/"):])
                return self._send(json.dumps({"started": True, "pid": pid}).encode("utf-8"), "application/json", 202)
            if path.startswith("/api/login/"):
                pid = open_for_login(self.out_dir, path[len("/api/login/"):])
                return self._send(json.dumps({"started": True, "pid": pid}).encode("utf-8"), "application/json", 202)
            if path.startswith("/api/approve/"):
                pid = open_for_submit(self.out_dir, path[len("/api/approve/"):])
                return self._send(json.dumps({"started": True, "pid": pid}).encode("utf-8"), "application/json", 202)
            if path.startswith("/api/mark/"):
                rec = mark(self.out_dir, path[len("/api/mark/"):], str(body.get("status", "")))
                return self._send(json.dumps({"ok": True, "status": rec["status"]}).encode("utf-8"), "application/json")
            if path.startswith("/api/settings/"):
                return self._send(json.dumps(self._settings_post(path[len("/api/settings/"):], body)).encode("utf-8"), "application/json")
        except Exception as e:
            return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 400)
        self._send(b"not found", "text/plain", 404)

    def _settings_post(self, action: str, body: dict) -> dict:
        """The Settings tab's writes, each to the file it names (settings.py).
        Every one answers with what was saved, or raises for a 400."""
        from . import settings

        if action == "env":
            updates = body.get("updates") or {}
            if not isinstance(updates, dict) or not updates:
                raise ValueError("nothing to save")
            settings.write_env({str(k): (None if v is None else str(v)) for k, v in updates.items()})
            return {"ok": True, "saved": sorted(updates)}
        if action == "answers":
            return {"ok": True, **settings.save_answers_section(str(body.get("section") or ""), body.get("data") or {},
                                                                  [str(x) for x in (body.get("removed") or [])])}
        if action == "basics":
            return {"ok": True, **settings.save_basics(body)}
        if action == "resume":
            return {"ok": True, **settings.save_skeleton(body.get("skeleton") or {})}
        if action == "resume/upload":
            import base64

            try:
                data = base64.b64decode(str(body.get("data") or ""), validate=True)
            except Exception as e:
                raise ValueError("the upload did not arrive whole") from e
            dest = settings.save_upload(str(body.get("name") or ""), data)
            result = settings.run_intake(dest, self.out_dir)
            result["upload"] = dest.name
            return {"ok": True, **result}
        if action == "restart":
            return {"ok": True, **settings.restart_workers(self.out_dir)}
        raise ValueError(f"unknown settings action {action!r}")

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in ("/", "/index.html"):
            return self._send(STATIC.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/index":
            try:
                body = json.dumps(build_index(self.out_dir)).encode("utf-8")
            except Exception as e:  # a half-written state file mid-save, most likely
                return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 500)
            return self._send(body, "application/json")
        if path == "/api/files":
            # Every tailored résumé and generated document, plus which portals
            # hold a saved sign-in. No secret in this response (see /api/logins/reveal).
            try:
                body = json.dumps(build_files(self.out_dir)).encode("utf-8")
            except Exception as e:
                return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 500)
            return self._send(body, "application/json")
        if path == "/api/logins/reveal":
            # The site account's e-mail and password, only when the page's
            # Reveal button asks: never part of the index or the files list.
            try:
                body = json.dumps(reveal_logins()).encode("utf-8")
            except Exception as e:
                return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 500)
            return self._send(body, "application/json")
        if path == "/api/settings":
            # Everything the Settings tab edits, read fresh from the files;
            # secrets come back as set/unset with their last four characters.
            try:
                from .settings import settings_view
                body = json.dumps(settings_view(self.out_dir)).encode("utf-8")
            except Exception as e:
                return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 500)
            return self._send(body, "application/json")
        if path == "/api/calendar":
            # Both calendars (calendar.py): applications by the day they went
            # out, and the dates the inbox set. Read from disk; nothing scanned here.
            try:
                from .calendar import build_calendar
                body = json.dumps(build_calendar(self.out_dir)).encode("utf-8")
            except Exception as e:
                return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 500)
            return self._send(body, "application/json")
        if path.startswith("/files/"):
            root = self.out_dir.resolve()
            target = (root / path[len("/files/"):]).resolve()
            if not (str(target).startswith(str(root) + os.sep) and target.is_file()):
                return self._send(b"not found", "text/plain", 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(target.read_bytes(), ctype)
        self._send(b"not found", "text/plain", 404)

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(out_dir: str | Path, port: int = 8765, open_browser: bool = False) -> None:
    handler = type("DashboardHandler", (_Handler,), {"out_dir": Path(out_dir)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"dashboard: {url}   reading {Path(out_dir).resolve()}   (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
