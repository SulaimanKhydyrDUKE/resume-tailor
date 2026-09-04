"""The list of postings to run unattended, and what the runner remembers
between invocations of that list.

Two files, both plain and inspectable — this is meant to be read, not just
executed against:

  queue.yaml   - what to apply to. You write this once per batch.
  state.json   - what happened. The runner writes this; you read it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class QueueEntry:
    id: str
    url: str = ""
    text: str = ""
    apply_url: str = ""
    company_hint: str = ""
    title: str = ""
    location: str = ""  # the listing's location(s); decides which address applies

    @property
    def has_source(self) -> bool:
        return bool(self.url or self.text)


def load_queue(path: str | Path) -> list[QueueEntry]:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a YAML list of entries")

    entries: list[QueueEntry] = []
    for item in raw:
        if isinstance(item, str):
            item = {"url": item}
        url, text = (item.get("url") or "").strip(), (item.get("text") or "").strip()
        entry_id = url or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        entries.append(QueueEntry(
            id=entry_id, url=url, text=text,
            apply_url=(item.get("apply_url") or url).strip(),
            company_hint=(item.get("company") or "").strip(),
            title=(item.get("title") or "").strip(),
        ))
    return entries


_CORP_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|holdings|group|technologies|labs)\b\.?")


def company_key(name: str) -> str:
    """One company, one key, however a posting or a résumé header spells it:
    "Chicago Trading Company (CTC)", "Chicago Trading Company" and "CTC
    Chicago Trading Co." all come out as "chicago trading". Parentheticals,
    punctuation and corporate suffixes are not the name."""
    n = (name or "").lower()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = _CORP_SUFFIX.sub(" ", n)
    n = re.sub(r"^\s*the\s+", "", n)
    return " ".join(n.split())


@dataclass
class RunState:
    """Persisted across runs so a killed or resumed batch does not reapply,
    and so `apply_once_at_company` holds across separate invocations, not just
    within one process."""

    path: Path
    done: dict[str, dict] = field(default_factory=dict)
    applied_companies: set[str] = field(default_factory=set)
    _touched: set[str] = field(default_factory=set, repr=False)
    _loaded: set[str] = field(default_factory=set, repr=False)  # applied companies as they stood when this process loaded

    @classmethod
    def load(cls, path: str | Path) -> "RunState":
        path = Path(path)
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        companies = {company_key(c) for c in data.get("applied_companies", []) if company_key(c)}
        return cls(path=path, done=data.get("done", {}), applied_companies=companies, _loaded=set(companies))

    def has_applied(self, company: str) -> bool:
        key = company_key(company)
        return bool(key) and key in self.applied_companies

    def mark_applied(self, company: str) -> None:
        key = company_key(company)
        if key:
            self.applied_companies.add(key)

    def save(self) -> None:
        """Write the state — merged with whatever another process wrote since
        this one loaded it (the watch and a hand-run retry may both be
        recording), under a lock, and atomically, so a reader such as the
        dashboard never sees half a file. Entries this process recorded win;
        everything else is taken as it stands on disk."""
        import fcntl
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    on_disk = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
                except json.JSONDecodeError:
                    on_disk = {}
                merged = dict(on_disk.get("done") or {})
                for entry_id in self._touched:
                    if entry_id in self.done:
                        merged[entry_id] = self.done[entry_id]
                for entry_id, rec in self.done.items():
                    merged.setdefault(entry_id, rec)
                # The file is the record of applied companies; this process
                # adds only what it applied to since loading, so a correction
                # made by hand (a false "applied" struck out) stays struck out.
                companies = ({company_key(c) for c in (on_disk.get("applied_companies") or []) if company_key(c)}
                             | (self.applied_companies - self._loaded))
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"done": merged, "applied_companies": sorted(companies)}, indent=2),
                               encoding="utf-8")
                os.replace(tmp, self.path)
                self.done, self.applied_companies = merged, companies
                # Written: from here on, a later edit by someone else (a hold
                # placed by hand) must win over this process's older record.
                self._touched.clear()
                self._loaded = set(companies)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def record(self, entry_id: str, outcome: dict[str, Any]) -> None:
        self.done[entry_id] = outcome
        self._touched.add(entry_id)
        self.save()

    def already_attempted(self, entry_id: str, retry_statuses: set[str]) -> bool:
        prior = self.done.get(entry_id)
        return prior is not None and prior.get("status") not in retry_statuses
