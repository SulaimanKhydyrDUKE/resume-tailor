"""Standalone CLI. No browser, no job board, no login — paste a description,
get a tailored PDF.

    resume-tailor init
    resume-tailor tailor jd.txt
    cat jd.txt | resume-tailor tailor -
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

from .profile import DEFAULT_PROFILE_DIR, Profile, ProfileError
from .queue import load_queue
from .render import available_styles

EXAMPLES = Path(__file__).resolve().parents[2] / "profile"


def _with_source(args: argparse.Namespace) -> argparse.Namespace:
    if not getattr(args, "source", None):
        from .discover import DEFAULT_URL

        args.source = DEFAULT_URL
    return args


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir or DEFAULT_PROFILE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    made = []
    for name in ("career", "answers", "queue"):
        dest, src = root / f"{name}.yaml", EXAMPLES / f"{name}.example.yaml"
        if dest.exists():
            print(f"  kept    {dest}")
            continue
        if not src.is_file():
            print(f"  ERROR   example missing: {src}", file=sys.stderr)
            return 1
        shutil.copy(src, dest)
        made.append(dest)
        print(f"  created {dest}")
    env_dest, env_src = root / "env", EXAMPLES / "env.example"
    if env_dest.exists():
        print(f"  kept    {env_dest}")
    elif env_src.is_file():
        shutil.copy(env_src, env_dest)
        env_dest.chmod(0o600)
        made.append(env_dest)
        print(f"  created {env_dest}  (your API key goes in here)")
    if made:
        print(
            "\nFill these in before tailoring anything. career.yaml is the only "
            "source the resume can draw on:\n  what is not in it cannot appear "
            "on the resume, by design.\n\n"
            "queue.yaml is only needed for `resume-tailor batch` — one entry per "
            "posting to apply to unattended."
        )
    return 0


async def _tailor(args: argparse.Namespace) -> int:
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        jd = sys.stdin.read() if args.job == "-" else Path(args.job).read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: couldn't read {args.job}: {e}", file=sys.stderr)
        return 1
    if len(jd.strip()) < 120:
        print("error: that job description is too short to tailor against.", file=sys.stderr)
        return 1

    from .llm import get_llm
    from .tailor import tailor

    try:
        llm = get_llm()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Tailoring with {llm.model} via {llm.name}…", file=sys.stderr)
    result = await tailor(
        profile=profile, job_description=jd, style=args.style, out_dir=args.out,
        log=lambda m: print(f"  {m}", file=sys.stderr),
    )

    cov = result.coverage
    print(f"\n  {result.pdf_path}  ({result.pages} page{'s' if result.pages != 1 else ''})")
    print(f"  {result.job.role_title}" + (f" at {result.job.company}" if result.job.company else ""))
    print(f"\n  Positioning: {result.selection.positioning}")
    if cov.get("hard_requirements_pct") is not None:
        print(f"\n  Hard requirements covered:  {cov['hard_requirements_pct']}%")
    if cov.get("preferred_pct") is not None:
        print(f"  Preferred covered:          {cov['preferred_pct']}%")
    if cov.get("hard_missing"):
        print(f"  Not matched: {', '.join(cov['hard_missing'])}")

    if result.selection.gaps:
        print("\n  Gaps this posting asks for that your record does not cover:")
        for g in result.selection.gaps:
            print(f"    - {g}")

    if result.dropped_claims:
        print("\n  CLAIMS THE AUDIT COULD NOT VERIFY — dropped before render, not sent:")
        for c in result.dropped_claims:
            print(f"    - {c}")

    for w in result.warnings:
        print(f"\n  ! {w}")

    if args.html:
        Path(args.html).write_text(result.html, encoding="utf-8")
        print(f"\n  html: {args.html}")
    return 0


async def _batch(args: argparse.Namespace) -> int:
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        n = len(load_queue(args.queue))
    except Exception as e:
        print(f"error reading {args.queue}: {e}", file=sys.stderr)
        return 1

    from .batch import run_batch
    from .llm import get_llm

    try:
        get_llm()  # fail on a missing credential now, not after the browser is open
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(
        f"{n} entries in queue. "
        f"{'DRY RUN — will not click submit.' if args.dry_run else 'Will submit unattended — no per-application approval.'}",
        file=sys.stderr,
    )

    def report(o):
        line = f"  [{o.status:16}] {o.company or '?':24} {o.role or ''}"
        if getattr(o, "fit", ""):
            line += f"  [{o.fit}]"
        print(line if not o.detail else f"{line}  — {o.detail}", file=sys.stderr)

    summary = await run_batch(
        profile=profile, queue_path=args.queue, out_dir=args.out,
        dry_run=args.dry_run, headless=args.headless, pause_seconds=args.pause,
        retry_all=args.retry_all, on_progress=report,
    )
    print(f"\n{summary['counts']}")
    print(f"report: {summary['report']}")
    print(f"state:  {summary['state']}  (rerun the same command to resume — done entries are skipped)")
    return 0


def _discover(args: argparse.Namespace) -> int:
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    from .discover import Prefs, describe, refresh, select
    from .queue import RunState

    out = Path(args.out)
    listings, changed = refresh(out, args.source)
    state = RunState.load(out / "batch-state.json")
    all_entries, excluded = select(listings, Prefs.from_profile(profile), state)
    entries = all_entries[: args.limit] if args.limit else all_entries
    by_id = {l.get("id") or l.get("url"): l for l in listings}
    shown = f" (showing {len(entries)})" if len(entries) < len(all_entries) else ""
    print(f"{len(listings)} listings ({'fresh' if changed else 'cached'}); {len(all_entries)} to apply to{shown}:\n")
    for e in entries:
        print("  " + describe(e, by_id.get(e.id)))
    print("\nleft out:", ", ".join(f"{n} {r}" for r, n in sorted(excluded.items(), key=lambda kv: -kv[1])))
    print("\n[form] = fills without help   [account] = will stop at a login wall until you log in there once")
    return 0


async def _watch(args: argparse.Namespace) -> int:
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    from .batch import run_batch
    from .discover import Prefs, describe, refresh, select
    from .llm import get_llm
    from .queue import RunState

    try:
        get_llm()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out = Path(args.out)
    prefs = Prefs.from_profile(profile)

    def report(o):
        line = f"  [{o.status:16}] {o.company or '?':24} {o.role or ''}"
        if getattr(o, "fit", ""):
            line += f"  [{o.fit}]"
        print(line if not o.detail else f"{line}  — {o.detail}", file=sys.stderr)

    while True:
        listings, changed = refresh(out, args.source)
        state = RunState.load(out / "batch-state.json")
        entries, excluded = select(listings, prefs, state, limit=args.max_per_run)
        stamp = time.strftime("%H:%M")
        print(f"[{stamp}] {len(listings)} listings ({'updated' if changed else 'unchanged'}), "
              f"{len(entries)} new to apply to", file=sys.stderr)
        if entries:
            by_id = {l.get("id") or l.get("url"): l for l in listings}
            for e in entries:
                print("    " + describe(e, by_id.get(e.id)), file=sys.stderr)
            summary = await run_batch(
                profile=profile, entries=entries, out_dir=out, dry_run=args.dry_run,
                headless=not args.show, pause_seconds=args.pause, on_progress=report,
            )
            print(f"[{stamp}] this pass: {summary['counts']}", file=sys.stderr)
        if args.once:
            break
        print(f"  next check in {args.interval} min", file=sys.stderr)
        await asyncio.sleep(args.interval * 60)
    return 0


PROJECT_ROOT = EXAMPLES.parent
PID_FILE = DEFAULT_PROFILE_DIR / "watch.pid"
LOG_FILE = DEFAULT_PROFILE_DIR / "watch.log"


def _watch_pid() -> int | None:
    import os

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_start(args: argparse.Namespace) -> int:
    """Run `watch` detached — its own session, logging to ~/.resume-tailor/watch.log —
    so it survives this terminal and this editor closing."""
    import os
    import subprocess

    if pid := _watch_pid():
        print(f"already running (pid {pid}). `resume-tailor status` to see it, `resume-tailor stop` to end it.")
        return 1
    cmd = [sys.executable, "-m", "resume_tailor.cli", "watch",
           "--interval", str(args.interval), "--max-per-run", str(args.max_per_run),
           "--pause", str(args.pause), "--out", str(PROJECT_ROOT / args.out)]
    if args.show:
        cmd.append("--show")
    if args.dry_run:
        cmd.append("--dry-run")
    # A leftover placeholder key would shadow an `ant auth login` profile; it is
    # irrelevant to the OpenAI provider either way.
    env = {k: v for k, v in os.environ.items() if not (k == "ANTHROPIC_API_KEY" and v in ("", "YOUR_KEY_HERE"))}
    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", buffering=1)
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    PID_FILE.write_text(str(proc.pid))
    print(f"started watch (pid {proc.pid}) — {'DRY RUN, nothing will be submitted' if args.dry_run else 'applying for real'}")
    print(f"  every {args.interval} min, up to {args.max_per_run} postings per pass")
    print(f"  log:    {LOG_FILE}\n  status: resume-tailor status\n  stop:   resume-tailor stop")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    import os
    import signal
    import time

    pid = _watch_pid()
    if not pid:
        print("no watch running")
        PID_FILE.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.25)
        if _watch_pid() is None:
            break
    else:
        os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped watch (pid {pid})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from collections import Counter

    from .queue import RunState

    pid = _watch_pid()
    print(f"watch: {f'running (pid {pid})' if pid else 'not running'}")
    state = RunState.load(PROJECT_ROOT / args.out / "batch-state.json")
    if not state.done:
        print("no attempts recorded yet")
        return 0
    counts = Counter(v.get("status", "?") for v in state.done.values())
    print(f"attempted: {len(state.done)}   companies applied to: {len(state.applied_companies)}")
    for status, n in counts.most_common():
        print(f"  {status:18} {n}")
    print("\nlast 10:")
    for v in list(state.done.values())[-10:]:
        line = f"  [{v.get('status', '?'):16}] {(v.get('company') or '?')[:22]:22} {(v.get('role') or '')[:40]}"
        if v.get("fit"):
            line += f"  [{v['fit']}]"
        print(line)
    if LOG_FILE.is_file():
        print("\nlog tail:")
        for line in LOG_FILE.read_text(errors="replace").splitlines()[-6:]:
            print("  " + line)
    return 0


DASH_PID_FILE = DEFAULT_PROFILE_DIR / "dashboard.pid"
DASH_LOG_FILE = DEFAULT_PROFILE_DIR / "dashboard.log"


def _dashboard_pid() -> int | None:
    import os

    try:
        pid = int(DASH_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def find_entry(out_dir: Path, needle: str):
    """A posting from the run state by id, or by a company-name fragment
    when exactly one matches; the listing cache supplies its URL."""
    import json

    from .discover import to_entry

    state_path = out_dir / "batch-state.json"
    done = json.loads(state_path.read_text(encoding="utf-8")).get("done", {}) if state_path.is_file() else {}
    cache = out_dir / "listings-cache.json"
    listings = {l.get("id") or l.get("url"): l for l in json.loads(cache.read_text(encoding="utf-8"))} if cache.is_file() else {}
    if needle in listings:
        return to_entry(listings[needle])
    hits = [eid for eid, rec in done.items() if needle.lower() in (rec.get("company") or "").lower()]
    if len(hits) == 1 and hits[0] in listings:
        return to_entry(listings[hits[0]])
    if len(hits) > 1:
        raise SystemExit(f"{len(hits)} postings match {needle!r}; give the id instead: " + ", ".join(hits[:6]))
    raise SystemExit(f"no posting matches {needle!r} (an id from the dashboard, or a company name)")


async def _review(args: argparse.Namespace) -> int:
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    from .batch import review_by_hand
    from .llm import get_llm

    try:
        get_llm()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = PROJECT_ROOT / args.out
    entry = find_entry(out, args.posting)
    print(f"opening {entry.company_hint} — {entry.title}\n  {entry.apply_url}", file=sys.stderr, flush=True)

    def report(o):
        line = f"  [{o.status:16}] {o.company or '?':24} {o.role or ''}"
        if getattr(o, "fit", ""):
            line += f"  [{o.fit}]"
        print(line if not o.detail else f"{line}  — {o.detail}", file=sys.stderr, flush=True)

    await review_by_hand(profile, entry, out_dir=out, on_progress=report)
    return 0


async def _login(args: argparse.Namespace) -> int:
    """Open the site's sign-in page in a visible Chrome; once the user is
    through, fill and submit the posting in that window and keep the cookies."""
    try:
        profile = Profile.load(args.profile)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    from .batch import login_and_apply
    from .llm import get_llm

    try:
        get_llm()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = PROJECT_ROOT / args.out
    entry = find_entry(out, args.posting)
    print(f"opening {entry.company_hint} — {entry.title}", file=sys.stderr, flush=True)

    def report(o):
        print(f"  [{o.status:16}] {o.company or '?':24} {o.role or ''}  — {o.detail[:200]}", file=sys.stderr, flush=True)

    o = await login_and_apply(profile, entry, out_dir=out, on_progress=report)
    return 0 if o.status in ("applied", "awaiting_approval") else 1


async def _submit(args: argparse.Namespace) -> int:
    """Send one posting the user has approved on the dashboard: the same
    headless fill as the batch, then the submit — no approval gate this time."""
    from dataclasses import asdict
    from datetime import datetime

    from .apply import DEFAULT_PROFILE_DIR, ApplySession
    from .batch import _process_one, _safe
    from .llm import get_llm
    from .queue import RunState

    try:
        profile = Profile.load(args.profile)
        get_llm()
    except (ProfileError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = PROJECT_ROOT / args.out
    entry = find_entry(out, args.posting)
    shots = out / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    state = RunState.load(out / "batch-state.json")
    apply_once = bool(profile.answers.get("search", {}).get("apply_once_at_company", True))
    session = ApplySession(headless=True, profile_dir=DEFAULT_PROFILE_DIR.parent / "approve-profiles" / _safe(entry.id)[:16])
    print(f"submitting {entry.company_hint} — {entry.title}", file=sys.stderr, flush=True)
    try:
        o = await _process_one(session, profile, entry, out, shots, apply_once, state, dry_run=False,
                               judge_gate=False, approved=True)
    finally:
        await session.stop()
    o.when = datetime.now().astimezone().isoformat(timespec="seconds")
    state.record(entry.id, asdict(o))
    print(f"  [{o.status}] {o.company or '?'} — {o.detail[:200]}", file=sys.stderr, flush=True)
    return 0 if o.status == "applied" else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the applications page over the watch's output directory — in
    this terminal, or detached with --detach (end it with --stop)."""
    import os
    import signal
    import subprocess

    url = f"http://127.0.0.1:{args.port}/"
    if args.stop:
        pid = _dashboard_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            print(f"stopped dashboard (pid {pid})")
        else:
            print("no dashboard running")
        DASH_PID_FILE.unlink(missing_ok=True)
        return 0
    if args.detach:
        if pid := _dashboard_pid():
            print(f"dashboard already running (pid {pid}): {url}")
            return 0
        DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        log = open(DASH_LOG_FILE, "a", buffering=1)
        proc = subprocess.Popen(
            [sys.executable, "-m", "resume_tailor.cli", "dashboard", "--port", str(args.port), "--out", args.out],
            cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        DASH_PID_FILE.write_text(str(proc.pid))
        print(f"dashboard (pid {proc.pid}): {url}\n  stop:  resume-tailor dashboard --stop")
        if args.open:
            import webbrowser

            webbrowser.open(url)
        return 0
    from .dashboard import serve

    serve(PROJECT_ROOT / args.out, port=args.port, open_browser=args.open)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="resume-tailor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create career.yaml and answers.yaml")
    pi.add_argument("--dir", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pi.set_defaults(func=lambda a: cmd_init(a))

    pt = sub.add_parser("tailor", help="tailor a resume to a job description")
    pt.add_argument("job", help="path to a file with the job description, or - for stdin")
    pt.add_argument("--style", default="clean", choices=sorted(available_styles()) or ["clean"])
    pt.add_argument("--out", default="output", help="output directory")
    pt.add_argument("--html", help="also write the raw HTML here")
    pt.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pt.set_defaults(func=lambda a: asyncio.run(_tailor(a)))

    pb = sub.add_parser("batch", help="apply to every posting in a queue file, unattended")
    pb.add_argument("queue", help="path to a queue YAML file (see profile/queue.example.yaml)")
    pb.add_argument("--dry-run", action="store_true", help="fill and screenshot every form but never click submit")
    pb.add_argument("--headless", action="store_true", help="hide the browser window")
    pb.add_argument("--pause", type=float, default=15.0, help="seconds between applications")
    pb.add_argument("--retry-all", action="store_true", help="re-attempt entries already recorded, not just failed ones")
    pb.add_argument("--out", default="output", help="output directory (PDFs, screenshots, report, state)")
    pb.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pb.set_defaults(func=lambda a: asyncio.run(_batch(a)))

    pd = sub.add_parser("discover", help="list new Summer 2027 postings from SimplifyJobs that match your search settings")
    pd.add_argument("--limit", type=int, default=60, help="show at most this many")
    pd.add_argument("--source", default=None, help="listings.json URL (default: SimplifyJobs Summer2027)")
    pd.add_argument("--out", default="output")
    pd.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pd.set_defaults(func=lambda a: _discover(_with_source(a)))

    pw = sub.add_parser("watch", help="poll SimplifyJobs and apply to new matching postings, unattended")
    pw.add_argument("--once", action="store_true", help="one pass, then exit (for cron)")
    pw.add_argument("--interval", type=float, default=30.0, help="minutes between checks")
    pw.add_argument("--max-per-run", type=int, default=25, help="cap applications per pass")
    pw.add_argument("--dry-run", action="store_true", help="tailor and fill, never click submit")
    pw.add_argument("--show", action="store_true", help="show the browser window (hidden by default)")
    pw.add_argument("--pause", type=float, default=20.0, help="seconds between applications")
    pw.add_argument("--source", default=None, help="listings.json URL (default: SimplifyJobs Summer2027)")
    pw.add_argument("--out", default="output")
    pw.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pw.set_defaults(func=lambda a: asyncio.run(_watch(_with_source(a))))

    pst = sub.add_parser("start", help="run `watch` detached, surviving this terminal; see `status`, end with `stop`")
    pst.add_argument("--interval", type=int, default=30, help="minutes between passes over the feed")
    pst.add_argument("--max-per-run", type=int, default=25, help="postings attempted per pass")
    pst.add_argument("--pause", type=float, default=15.0, help="seconds between applications")
    pst.add_argument("--show", action="store_true", help="show the browser window (hidden by default)")
    pst.add_argument("--dry-run", action="store_true", help="fill and screenshot, never submit")
    pst.add_argument("--out", default="output")
    pst.set_defaults(func=cmd_start)

    psp = sub.add_parser("stop", help="stop the detached watch")
    psp.set_defaults(func=cmd_stop)

    pss = sub.add_parser("status", help="what the watch has done: counts by outcome, the last ten, the log tail")
    pss.add_argument("--out", default="output")
    pss.set_defaults(func=cmd_status)

    ps = sub.add_parser("styles", help="list resume themes")
    ps.set_defaults(func=lambda a: (print("\n".join(sorted(available_styles()))), 0)[1])

    pdash = sub.add_parser("dashboard", help="a local web page of every application: outcome, judges, the answers given, the resume sent")
    pdash.add_argument("--port", type=int, default=8765)
    pdash.add_argument("--out", default="output")
    pdash.add_argument("--open", action="store_true", help="open it in your browser")
    pdash.add_argument("--detach", action="store_true", help="run it in the background, surviving this terminal")
    pdash.add_argument("--stop", action="store_true", help="stop a detached dashboard")
    pdash.set_defaults(func=cmd_dashboard)

    plg = sub.add_parser("login", help="open a site's sign-in wall in Chrome; after you log in, the tool fills and submits that posting")
    plg.add_argument("posting", help="an id from the dashboard, or a company name")
    plg.add_argument("--out", default="output")
    plg.add_argument("--profile", default=None)
    plg.set_defaults(func=lambda a: asyncio.run(_login(a)))
    psb = sub.add_parser("submit", help="fill and submit one posting you approved on the dashboard (no approval gate)")
    psb.add_argument("posting", help="an id from the dashboard, or a company name")
    psb.add_argument("--out", default="output")
    psb.add_argument("--profile", default=None)
    psb.set_defaults(func=lambda a: asyncio.run(_submit(a)))
    prv = sub.add_parser("review", help="open one posting's form in a visible Chrome window, filled in, for you to check and submit")
    prv.add_argument("posting", help="a posting id from the dashboard, or a company name")
    prv.add_argument("--out", default="output")
    prv.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    prv.set_defaults(func=lambda a: asyncio.run(_review(a)))

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
