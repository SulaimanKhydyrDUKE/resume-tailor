"""MCP server: tailoring plus the browser that can actually attach the PDF.

One process owns both halves on purpose. Splitting them means the tailorer
writes a file the browser server cannot see and the browser cannot attach.
"""
from __future__ import annotations

import json
import sys

from resume_tailor import __version__
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .apply import ApplySession, _is_empty
from .profile import Profile, ProfileError
from .render import available_styles, render_pdf_async

mcp = MCPServer(name="resume-tailor")

OUT_DIR = Path(os.environ.get("RESUME_TAILOR_OUTPUT", Path.cwd() / "output")).expanduser()
_session = ApplySession(headless=os.environ.get("RESUME_TAILOR_HEADLESS", "0") == "1")
_profile: Profile | None = None


def _get_profile() -> Profile:
    global _profile
    if _profile is None:
        _profile = Profile.load()
    return _profile


# --- tailoring ------------------------------------------------------------

@mcp.tool()
async def tailor_resume(job_description: str, style: str = "clean", label: str = "role") -> str:
    """Generate a resume tailored to one job description and render it to PDF.

    Returns JSON with the PDF path, an ATS keyword-coverage report, and any
    requirements the career record does not support. Pass the full text of the
    posting, not a URL.
    """
    from .tailor import tailor

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})

    result = await tailor(
        profile=profile,
        job_description=job_description,
        style=style,
        out_dir=OUT_DIR,
        label=label,
    )
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool()
async def render_resume_pdf(body_html: str, out_name: str, style: str = "clean") -> str:
    """Render resume HTML to a styled, ATS-parseable PDF. Returns the path."""
    path = await render_pdf_async(body_html, OUT_DIR / f"{out_name}.pdf", style=style)
    return str(path)


@mcp.tool()
def list_styles() -> str:
    """List available resume CSS themes."""
    return json.dumps(sorted(available_styles().keys()))


# --- the answer bank ------------------------------------------------------

@mcp.tool()
def answer_form_question(question: str) -> str:
    """Look up one application-form question in the answer bank.

    Returns {"answer": ..., "source": ...} on a hit, or {"answer": null} on a
    miss. A miss means ask the user — never invent the value.
    """
    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    answer, key = profile.lookup(question)
    return json.dumps({"answer": answer, "source": key, "question": question})


@mcp.tool()
def list_known_answers() -> str:
    """Every question the answer bank can answer, as key -> value."""
    try:
        return json.dumps(_get_profile().flat_answers(), indent=2)
    except ProfileError as e:
        return json.dumps({"error": str(e)})


# --- the browser ----------------------------------------------------------

@mcp.tool()
async def browser_open(url: str) -> str:
    """Open a URL in the persistent browser. Logins survive across runs."""
    return await _session.goto(url)


@mcp.tool()
async def browser_read() -> str:
    """Visible text of the current page — use this to read a job posting."""
    return (await _session.read_text())[:60000]


@mcp.tool()
async def describe_form() -> str:
    """Every visible form field with its label, type, options and required flag."""
    return json.dumps(await _session.describe_form(), indent=2)


@mcp.tool()
async def fill_field(selector: str, value: str) -> str:
    """Fill one field. Use a selector from describe_form."""
    await _session.fill(selector, value)
    return f"filled {selector}"


@mcp.tool()
async def attach_file(selector: str, file_path: str) -> str:
    """Attach a file to a file input — the resume PDF, typically.

    This is the step a browser-tool server cannot do for you: browsers reject
    synthetic events on file inputs, so it must run in-process via Playwright.
    """
    await _session.upload(selector, file_path)
    return f"attached {Path(file_path).name} to {selector}"


@mcp.tool()
async def click(selector: str) -> str:
    """Click an element. Use list_buttons to find one."""
    await _session.click(selector)
    return f"clicked {selector}"


@mcp.tool()
async def list_buttons() -> str:
    """Every visible button on the page, with its text."""
    return json.dumps(await _session.buttons(), indent=2)


@mcp.tool()
async def screenshot(name: str = "page") -> str:
    """Full-page screenshot. Returns the path."""
    return str(await _session.screenshot(OUT_DIR / f"{name}.png"))


@mcp.tool()
async def review_before_submit() -> str:
    """Pre-submit check: current values of every field, plus required ones still empty.

    Read this before calling submit. It is the last point at which a wrong
    answer costs nothing to fix.
    """
    fields = await _session.describe_form()
    missing = await _session.unfilled_required()
    shot = await _session.screenshot(OUT_DIR / "pre-submit.png")
    return json.dumps(
        {"fields": fields, "unfilled_required": missing, "screenshot": str(shot),
         "buttons": await _session.buttons()},
        indent=2,
    )


@mcp.tool()
async def submit_application(selector: str, confirm: str = "") -> str:
    """Submit the application by clicking `selector`.

    Requires confirm="yes-submit" so a submit is always a deliberate call and
    never something the model drifts into. Call review_before_submit first.
    """
    if confirm != "yes-submit":
        return json.dumps({
            "submitted": False,
            "reason": "Pass confirm='yes-submit' to actually submit. Call review_before_submit first.",
        })
    missing = await _session.unfilled_required()
    if missing:
        return json.dumps({
            "submitted": False,
            "reason": "Required fields are still empty.",
            "unfilled_required": missing,
        })
    await _session.click(selector)
    shot = await _session.screenshot(OUT_DIR / "post-submit.png")
    return json.dumps({"submitted": True, "screenshot": str(shot),
                       "page_text": (await _session.read_text())[:2000]})


# --- unattended batch ------------------------------------------------------

@mcp.tool()
async def run_batch_apply(queue_path: str, dry_run: bool = False, headless: bool = True,
                          pause_seconds: float = 15.0, retry_all: bool = False) -> str:
    """Apply to every posting in a queue file, unattended — no per-application approval.

    Uses its own browser session, separate from browser_open/describe_form above.
    A required field the answer bank cannot resolve skips that one posting and
    logs why, rather than guessing; a login wall or a bot/CAPTCHA check does the
    same. Set dry_run=True first to fill and screenshot every form without
    clicking submit. Safe to re-run — entries already recorded are skipped
    unless retry_all=True.

    Returns a JSON summary. The full per-posting report is a CSV at
    <out_dir>/batch-report.csv; screenshots are under <out_dir>/screenshots/.
    """
    from .batch import run_batch

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})

    summary = await run_batch(
        profile=profile, queue_path=queue_path, out_dir=OUT_DIR,
        dry_run=dry_run, headless=headless, pause_seconds=pause_seconds, retry_all=retry_all,
    )
    return json.dumps(summary, indent=2)


@mcp.tool()
def discover_internships(limit: int = 40) -> str:
    """New Summer 2027 software postings from the SimplifyJobs list that match
    the search settings in answers.yaml and haven't been attempted yet.

    Returns JSON: the postings in the order they would be attempted (direct-form
    ATS hosts first, account-gated hosts last) plus a tally of why the rest were
    left out. Read-only — nothing is applied to.
    """
    from .discover import Prefs, refresh, select
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    listings, changed = refresh(OUT_DIR)
    entries, excluded = select(listings, Prefs.from_profile(profile), RunState.load(OUT_DIR / "batch-state.json"), limit=limit)
    return json.dumps({
        "listings_total": len(listings), "source_changed": changed,
        "to_apply": [{"company": e.company_hint, "title": e.title, "url": e.url, "apply_url": e.apply_url} for e in entries],
        "left_out": excluded,
    }, indent=2)


@mcp.tool()
async def apply_new_internships(dry_run: bool = False, max_per_run: int = 25, headless: bool = True) -> str:
    """One pass of the unattended loop: fetch the SimplifyJobs list, pick the new
    matching postings, tailor a resume to each and apply. No per-application
    approval. Unanswerable required fields, login walls and CAPTCHAs each skip
    that one posting and are logged. dry_run=True fills and screenshots without
    submitting. Returns the pass summary; batch_report has the per-posting detail.
    """
    from .batch import run_batch
    from .discover import Prefs, refresh, select
    from .queue import RunState

    try:
        profile = _get_profile()
    except ProfileError as e:
        return json.dumps({"error": str(e)})
    listings, changed = refresh(OUT_DIR)
    entries, excluded = select(listings, Prefs.from_profile(profile), RunState.load(OUT_DIR / "batch-state.json"), limit=max_per_run)
    if not entries:
        return json.dumps({"listings_total": len(listings), "new": 0, "left_out": excluded})
    summary = await run_batch(profile=profile, entries=entries, out_dir=OUT_DIR, dry_run=dry_run, headless=headless)
    summary.update({"listings_total": len(listings), "new": len(entries), "left_out": excluded})
    return json.dumps(summary, indent=2)


@mcp.tool()
def batch_report() -> str:
    """The results of the most recent batch run — status and detail per posting."""
    path = OUT_DIR / "batch-report.csv"
    if not path.is_file():
        return json.dumps({"error": f"no report at {path} yet — run run_batch_apply first"})
    return path.read_text(encoding="utf-8")


def main() -> None:
    if "--version" in sys.argv[1:]:
        print(f"resume-tailor-mcp {__version__}")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
