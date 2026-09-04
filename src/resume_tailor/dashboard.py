"""A window on the loop: what was attempted, what came of it, and what was sent.

`resume-tailor dashboard` serves a single-page app (React, shipped with the
package) and a small read-only JSON API over the files the watch already
writes — batch-state.json, the listings cache, the PDFs and screenshots under
output/ — so nothing runs twice and nothing is copied. Local only: it binds
to 127.0.0.1 and never writes.
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


def watch_status(profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    pid_file, log = profile_dir / "watch.pid", profile_dir / "watch.log"
    pid, running = None, False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        running = True
    except (OSError, ValueError):
        pass
    tail: list[str] = []
    last_pass = None
    if log.is_file():
        lines = log.read_text(errors="replace").splitlines()
        tail = lines[-15:]
        last_pass = next((line for line in reversed(lines) if re.match(r"^\[\d\d:\d\d\] ", line)), None)
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
    return {"running": running, "pid": pid if running else None, "last_pass": last_pass, "log_tail": tail,
            "pass_running": pass_running, "pass_pid": pass_pid,
            "label": "loop running" if running else ("retry pass running (the loop resumes when it ends)" if pass_running else "loop stopped")}


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
        })
    apps.sort(key=lambda a: a["when"], reverse=True)
    now = None
    current = profile_dir / "current.json"
    try:
        if current.is_file() and time.time() - current.stat().st_mtime < 1800:
            now = json.loads(current.read_text(encoding="utf-8"))
            if now.get("stage") == "idle":
                now = None
            else:
                try:
                    os.kill(int(now.get("pid") or 0), 0)
                except (OSError, ValueError):
                    now = None  # the process that wrote it is gone
    except Exception:
        now = None
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "now": now,
        "watch": watch_status(profile_dir),
        "pool": pool_status(out_dir),
        "counts": dict(Counter(a["status"] for a in apps)),
        "applied_companies": state.get("applied_companies") or [],
        "applications": apps,
    }


REVIEWS_DIR = DEFAULT_PROFILE_DIR / "reviews"


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
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
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
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
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
        cwd=out_dir.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
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
        except Exception as e:
            return self._send(json.dumps({"error": str(e)}).encode("utf-8"), "application/json", 400)
        self._send(b"not found", "text/plain", 404)

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
