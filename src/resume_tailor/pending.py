"""Questions the agent could not answer, written down instead of waited on.

The chrome-apply rule: when a form asks something the facts, the bank and
the record cannot answer (an essay in the user's own words, a video, a
preference nobody recorded), the agent does not stop the run to ask. It
leaves the form open in its tab, appends one line here, records the posting
`needs_review`, and moves to the next posting. The user works through the
list when they sit down (`resume-tailor pending`), answers once
(`resume-tailor pending --answer N "..."`, which also teaches the bank), and
the open tab is finished by hand or by the next session.

One JSON object per line in output/pending-questions.jsonl (git-ignored with
the rest of output/): when, entry_id, company, role, url, tab, question,
status (open | answered | dropped), answer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FILE = "pending-questions.jsonl"


def _path(out_dir: str | Path) -> Path:
    return Path(out_dir) / FILE


def _read(out_dir: str | Path) -> list[dict[str, Any]]:
    p = _path(out_dir)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write(out_dir: str | Path, rows: list[dict[str, Any]]) -> None:
    p = _path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(p)


def add(out_dir: str | Path, question: str, company: str = "", role: str = "", url: str = "",
        entry_id: str = "", tab: str = "") -> dict[str, Any]:
    """Note one unanswered question; the same question for the same posting is not repeated."""
    rows = _read(out_dir)
    for r in rows:
        if r.get("status") == "open" and r.get("question") == question and r.get("url") == url:
            return r
    row = {"n": max([int(r.get("n", 0)) for r in rows] + [0]) + 1, "when": time.strftime("%Y-%m-%d %H:%M"),
           "entry_id": entry_id, "company": company, "role": role, "url": url, "tab": tab,
           "question": question, "status": "open", "answer": ""}
    rows.append(row)
    _write(out_dir, rows)
    return row


def listing(out_dir: str | Path, include_closed: bool = False) -> list[dict[str, Any]]:
    return [r for r in _read(out_dir) if include_closed or r.get("status") == "open"]


def close(out_dir: str | Path, n: int, answer: str = "", status: str = "answered") -> dict[str, Any] | None:
    rows = _read(out_dir)
    hit = None
    for r in rows:
        if int(r.get("n", 0)) == int(n):
            r["status"] = status
            r["answer"] = answer
            r["closed"] = time.strftime("%Y-%m-%d %H:%M")
            hit = r
    if hit:
        _write(out_dir, rows)
    return hit
