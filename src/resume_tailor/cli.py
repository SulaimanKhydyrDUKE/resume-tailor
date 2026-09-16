"""Standalone CLI. No browser, no job board, no login — paste a description,
get a tailored PDF.

    resume-tailor init
    resume-tailor tailor jd.txt
    cat jd.txt | resume-tailor tailor -
"""
from __future__ import annotations

import argparse
import asyncio
import re
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

    import json
    import os

    out = Path(args.out)
    prefs = Prefs.from_profile(profile)
    shard = _parse_shard(getattr(args, "shard", None))
    fresh = bool(getattr(args, "fresh", False))
    tag = ""
    profile_dir = None
    if shard is not None:
        k, n = shard
        os.environ["RESUME_TAILOR_SHARD"] = f"{k}/{n}"
        tag = f"[w{k}] "
        # Worker 0 keeps the browser profile the user logged into by hand;
        # the others get their own (saved site cookies reach every one).
        profile_dir = DEFAULT_PROFILE_DIR / ("browser-profile" if k == 0 else f"browser-profile-w{k}")
    if fresh:
        # The fresh lane: every few minutes, every source; a posting that was
        # not there at the last look is announced and applied to at once,
        # ahead of the workers' next pass. What was already listed when the
        # lane started is the workers' backlog, not news.
        os.environ["RESUME_TAILOR_SHARD"] = "fresh"
        tag = "[fresh] "
        profile_dir = DEFAULT_PROFILE_DIR / "browser-profile-fresh"
    seen_path = out / "fresh-seen.json"
    seen: set[str] | None = None
    if fresh and seen_path.is_file():
        try:
            seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
        except Exception:
            seen = None
    retries_first = bool(getattr(args, "retries_first", False))

    def report(o):
        line = f"  {tag}[{o.status:16}] {o.company or '?':24} {o.role or ''}"
        if getattr(o, "fit", ""):
            line += f"  [{o.fit}]"
        print(line if not o.detail else f"{line}  — {o.detail}", file=sys.stderr, flush=True)

    while True:
        try:
            listings, changed = refresh(out, args.source)
        except OSError as e:
            # No cache and no network: a worker that dies here takes the
            # supervisor's restart to come back. Wait for the network instead.
            print(f"{tag}[{time.strftime('%H:%M')}] listings unavailable ({str(e)[:80]}); trying again in 2 min", file=sys.stderr, flush=True)
            time.sleep(120)
            continue
        state = RunState.load(out / "batch-state.json")
        stamp = time.strftime("%H:%M")
        if fresh:
            ids_now = {str(l.get("id") or l.get("url")) for l in listings}
            if seen is None:
                seen = ids_now
                seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
                print(f"{tag}[{stamp}] tracker armed: {len(seen)} listings on file; anything new is applied to within "
                      f"{args.interval:g} min", file=sys.stderr, flush=True)
                _notify("resume-tailor tracker armed", f"{len(seen)} listings on file; new postings are applied to within {args.interval:g} minutes")
            else:
                new_ids = ids_now - seen
                seen |= ids_now
                seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
                entries, excluded = select(listings, prefs, state, limit=args.max_per_run)
                entries = [e for e in entries if e.id in new_ids]
                print(f"{tag}[{stamp}] {len(listings)} listings ({'updated' if changed else 'unchanged'}), "
                      f"{len(new_ids)} new on the sources, {len(entries)} worth applying to", file=sys.stderr, flush=True)
                if entries:
                    by_id = {l.get("id") or l.get("url"): l for l in listings}
                    for e in entries:
                        print(f"    {tag}" + describe(e, by_id.get(e.id)), file=sys.stderr, flush=True)
                    _notify(f"{len(entries)} new internship posting{'s' if len(entries) > 1 else ''}",
                            "; ".join(f"{e.company_hint}: {e.title}"[:70] for e in entries[:3]) + (" …" if len(entries) > 3 else ""))
                    summary = await run_batch(
                        profile=profile, entries=entries, out_dir=out, dry_run=args.dry_run,
                        headless=not args.show, pause_seconds=args.pause, on_progress=report,
                        profile_dir=profile_dir,
                    )
                    counts = summary["counts"]
                    print(f"{tag}[{stamp}] fresh pass: {counts}", file=sys.stderr, flush=True)
                    _notify(f"applied to {counts.get('applied', 0)} of {len(entries)} new posting{'s' if len(entries) > 1 else ''}",
                            ", ".join(f"{k} {v}" for k, v in counts.items()))
            if args.once:
                break
            await asyncio.sleep(args.interval * 60)
            continue
        entries, excluded = select(listings, prefs, state, limit=args.max_per_run, shard=shard,
                                   retries_first=retries_first)
        retries_first = False  # the first pass deals the earlier review items; later ones interleave
        print(f"{tag}[{stamp}] {len(listings)} listings ({'updated' if changed else 'unchanged'}), "
              f"{len(entries)} to apply to" + (f" (worker {shard[0] + 1} of {shard[1]})" if shard else ""),
              file=sys.stderr, flush=True)
        if entries:
            by_id = {l.get("id") or l.get("url"): l for l in listings}
            for e in entries:
                print(f"    {tag}" + describe(e, by_id.get(e.id)), file=sys.stderr, flush=True)
            summary = await run_batch(
                profile=profile, entries=entries, out_dir=out, dry_run=args.dry_run,
                headless=not args.show, pause_seconds=args.pause, on_progress=report,
                profile_dir=profile_dir,
            )
            print(f"{tag}[{stamp}] this pass: {summary['counts']}", file=sys.stderr, flush=True)
        if args.once:
            break
        # A full pass means the pool holds more: back within a minute rather
        # than the full interval, which is for the quiet hours.
        wait = args.interval if len(entries) < (args.max_per_run or 0) else min(args.interval, 1.0)
        print(f"  {tag}next check in {wait:g} min", file=sys.stderr, flush=True)
        await asyncio.sleep(wait * 60)
    return 0


def _notify(title: str, text: str) -> None:
    """A desktop notification (macOS Notification Center); silent elsewhere
    and on any failure — the log carries the same line either way."""
    import json
    import subprocess

    if sys.platform != "darwin":
        return
    try:
        script = f'display notification {json.dumps(text[:200])} with title {json.dumps(title[:80])} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def _parse_shard(text: str | None) -> tuple[int, int] | None:
    """"1/4" -> (1, 4): this process is worker 1 of 4."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    if not m or int(m.group(2)) < 1 or not (0 <= int(m.group(1)) < int(m.group(2))):
        raise SystemExit(f"--shard wants K/N with 0 <= K < N, not {text!r}")
    return int(m.group(1)), int(m.group(2))


PROJECT_ROOT = EXAMPLES.parent
PID_FILE = DEFAULT_PROFILE_DIR / "watch.pid"
LOG_FILE = DEFAULT_PROFILE_DIR / "watch.log"


def _pid_file(worker: int) -> Path:
    return PID_FILE.with_name(f"watch-w{worker}.pid")


def _alive(path: Path) -> int | None:
    """The pid a file names, when that process is still there."""
    import os

    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _watch_pid() -> int | None:
    return _alive(PID_FILE)


def _workers() -> dict[int, int | None]:
    """Worker number -> its pid when alive (None when its file is stale),
    from the per-worker pid files; the plain watch.pid is worker 0."""
    out: dict[int, int | None] = {}
    for p in sorted(DEFAULT_PROFILE_DIR.glob("watch-w*.pid")):
        m = re.fullmatch(r"watch-w(\d+)\.pid", p.name)
        if m:
            out[int(m.group(1))] = _alive(p)
    if PID_FILE.is_file() and 0 not in out:
        out[0] = _alive(PID_FILE)
    return out


FRESH_PID_FILE = DEFAULT_PROFILE_DIR / "fresh.pid"


def _fresh_pid() -> int | None:
    return _alive(FRESH_PID_FILE) if FRESH_PID_FILE.is_file() else None


def _status_line() -> str:
    """The first line of `status`: what the supervisor reads to decide
    whether every worker is up."""
    workers = _workers()
    fresh = _fresh_pid()
    fresh_note = (f"; fresh lane running (pid {fresh})" if fresh
                  else ("; fresh lane down" if FRESH_PID_FILE.is_file() else ""))
    if not workers:
        return "watch: not running" + fresh_note
    up = {k: pid for k, pid in workers.items() if pid}
    n = max(workers) + 1
    if not up:
        return f"watch: not running (0 of {n} workers)" + fresh_note
    pids = ", ".join(str(up[k]) for k in sorted(up))
    return f"watch: running, {len(up)} of {n} workers (pids {pids})" + fresh_note


def cmd_start(args: argparse.Namespace) -> int:
    """Run `watch` detached — its own session, logging to ~/.resume-tailor/watch.log —
    so it survives this terminal and this editor closing."""
    import os
    import subprocess

    workers = max(1, int(getattr(args, "workers", 1) or 1))
    running = {k: pid for k, pid in _workers().items() if pid}
    if workers == 1 and running:
        print(f"already running ({_status_line()}). `resume-tailor status` to see it, `resume-tailor stop` to end it.")
        return 1
    # A leftover placeholder key would shadow an `ant auth login` profile; it is
    # irrelevant to the OpenAI provider either way.
    env = {k: v for k, v in os.environ.items() if not (k == "ANTHROPIC_API_KEY" and v in ("", "YOUR_KEY_HERE"))}
    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    started = []
    for k in range(workers):
        if k in running:
            continue  # this worker is up; only the missing ones are launched
        cmd = [sys.executable, "-m", "resume_tailor.cli", "watch",
               "--interval", str(args.interval), "--max-per-run", str(args.max_per_run),
               "--pause", str(args.pause), "--out", str(PROJECT_ROOT / args.out)]
        if workers > 1:
            cmd += ["--shard", f"{k}/{workers}"]
        if getattr(args, "retries_first", False):
            cmd.append("--retries-first")
        if args.show:
            cmd.append("--show")
        if args.dry_run:
            cmd.append("--dry-run")
        log = open(LOG_FILE, "a", buffering=1)
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        if workers > 1:
            _pid_file(k).write_text(str(proc.pid))
        if k == 0:
            PID_FILE.write_text(str(proc.pid))
        started.append((k, proc.pid))
    if getattr(args, "fresh", False) and not _fresh_pid():
        # The fresh lane: every source every few minutes, new postings applied to at once.
        cmd = [sys.executable, "-m", "resume_tailor.cli", "watch", "--fresh",
               "--interval", str(getattr(args, "fresh_interval", 5)), "--max-per-run", "10",
               "--pause", "5", "--out", str(PROJECT_ROOT / args.out)]
        if args.dry_run:
            cmd.append("--dry-run")
        log = open(LOG_FILE, "a", buffering=1)
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        FRESH_PID_FILE.write_text(str(proc.pid))
        started.append(("fresh", proc.pid))
    if not started:
        print(f"every worker is up ({_status_line()})")
        return 0
    who = ", ".join(f"worker {k} pid {pid}" for k, pid in started) if workers > 1 or len(started) > 1 else f"pid {started[0][1]}"
    print(f"started watch ({who}) — {'DRY RUN, nothing will be submitted' if args.dry_run else 'applying for real'}")
    print(f"  every {args.interval} min, up to {args.max_per_run} postings per pass"
          + (f", {workers} workers by company" if workers > 1 else ""))
    print(f"  log:    {LOG_FILE}\n  status: resume-tailor status\n  stop:   resume-tailor stop")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    import os
    import signal
    import time

    def clear_pid_files() -> None:
        PID_FILE.unlink(missing_ok=True)
        FRESH_PID_FILE.unlink(missing_ok=True)
        for p in DEFAULT_PROFILE_DIR.glob("watch-w*.pid"):
            p.unlink(missing_ok=True)

    alive = {k: pid for k, pid in _workers().items() if pid}
    if _fresh_pid():
        alive["fresh"] = _fresh_pid()
    if not alive:
        print("no watch running")
        clear_pid_files()
        return 0
    for pid in alive.values():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        time.sleep(0.25)
        if not any(_alive(p) for p in [PID_FILE, FRESH_PID_FILE, *DEFAULT_PROFILE_DIR.glob("watch-w*.pid")] if p.is_file()):
            break
    else:
        for pid in alive.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    clear_pid_files()
    print("stopped watch (" + ", ".join(f"worker {k} pid {pid}" for k, pid in sorted(alive.items(), key=lambda kv: str(kv[0]))) + ")")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from collections import Counter

    from .queue import RunState

    print(_status_line())
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
    from .profile import apply_once_at_company
    apply_once = apply_once_at_company(profile.answers)
    session = ApplySession(headless=not bool(getattr(args, "show", False)),
                           profile_dir=DEFAULT_PROFILE_DIR.parent / "approve-profiles" / _safe(entry.id)[:16])
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
    pw.add_argument("--shard", default=None, help="K/N: this process is worker K of N; it takes every N-th company")
    pw.add_argument("--retries-first", action="store_true", help="first pass: the earlier review items before any new posting")
    pw.add_argument("--fresh", action="store_true", help="the fresh lane: poll every source each interval and apply at once to postings that just appeared")
    pw.add_argument("--out", default="output")
    pw.add_argument("--profile", help=f"profile directory (default {DEFAULT_PROFILE_DIR})")
    pw.set_defaults(func=lambda a: asyncio.run(_watch(_with_source(a))))

    pst = sub.add_parser("start", help="run `watch` detached, surviving this terminal; see `status`, end with `stop`")
    pst.add_argument("--interval", type=int, default=30, help="minutes between passes over the feed")
    pst.add_argument("--max-per-run", type=int, default=25, help="postings attempted per pass")
    pst.add_argument("--pause", type=float, default=15.0, help="seconds between applications")
    pst.add_argument("--workers", type=int, default=1, help="parallel loops, each owning every N-th company (default 1)")
    pst.add_argument("--retries-first", action="store_true", help="first pass: the earlier review items before any new posting")
    pst.add_argument("--fresh", action="store_true", help="also run the fresh lane: every source every few minutes, new postings applied to at once, with a desktop notification")
    pst.add_argument("--fresh-interval", type=float, default=5, help="minutes between the fresh lane's polls (default 5)")
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

    pm = sub.add_parser("mail", help="read the inbox for what came of each application (assessments, interviews, offers, rejections)")
    pm.add_argument("action", choices=["scan", "results", "watch"], help="scan once, print the results, or scan every --interval minutes")
    pm.add_argument("--out", default="output")
    pm.add_argument("--since", default="03-Sep-2026", help="IMAP date, e.g. 03-Sep-2026")
    pm.add_argument("--interval", type=float, default=30.0, help="minutes between scans (watch)")
    pm.add_argument("--limit", type=int, default=None, help="scan at most this many new messages (scan)")
    pm.add_argument("--no-model", action="store_true", help="rules only; do not ask the model about unplaced mail")
    pm.set_defaults(func=lambda a: __import__("resume_tailor.mailscan", fromlist=["run_cli"]).run_cli(a))

    po = sub.add_parser("outreach", help="e-mail the recruiting team of each company applied to: a short note, the résumé attached")
    po.add_argument("action", choices=["lookup", "plan", "send", "log"],
                    help="lookup = fill the address cache for every company, plan = show what would be sent, send = send it, log = what was sent")
    po.add_argument("--out", default="output")
    po.add_argument("--max", type=int, default=15, help="at most this many e-mails this run (and 15 a day)")
    po.add_argument("--only", default=None, help="only companies whose name contains this")
    po.add_argument("--force", action="store_true", help="send outside weekday working hours / past the daily cap")
    po.add_argument("--no-web", action="store_true", help="do not web-search for addresses; inbox and pages only")
    po.add_argument("--refresh", action="store_true", help="lookup: redo companies whose cache already holds an address")
    po.add_argument("--workers", type=int, default=3, help="lookup: companies searched at once (default 3)")
    po.set_defaults(func=lambda a: __import__("resume_tailor.outreach", fromlist=["run_cli"]).run_cli(a))

    plg = sub.add_parser("login", help="open a site's sign-in wall in Chrome; after you log in, the tool fills and submits that posting")
    plg.add_argument("posting", help="an id from the dashboard, or a company name")
    plg.add_argument("--out", default="output")
    plg.add_argument("--profile", default=None)
    plg.set_defaults(func=lambda a: asyncio.run(_login(a)))
    psb = sub.add_parser("submit", help="fill and submit one posting you approved on the dashboard (no approval gate)")
    psb.add_argument("posting", help="an id from the dashboard, or a company name")
    psb.add_argument("--out", default="output")
    psb.add_argument("--profile", default=None)
    psb.add_argument("--show", action="store_true", help="a visible Chrome window: some boards' bot checks stall in a headless one")
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
