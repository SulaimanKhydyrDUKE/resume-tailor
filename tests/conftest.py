"""Collect the standalone check scripts (tests/check_*.py) as pytest items.

Each script is the original harness: it runs its checks with every model call
stubbed, prints one ``  PASS  name`` or ``  FAIL  name  detail`` line per check,
and exits non-zero if any failed. This plugin runs each script once in a
subprocess and turns every printed line into its own pytest test, so
``pytest tests -q`` reports the same checks individually with no rewrite and
no loss of coverage. The scripts still run standalone: ``python tests/check_batch.py``.

The chain script renders a PDF through Playwright. When Chromium is not
installed, its checks are skipped with the install command in the reason.
"""

import os
import re
import subprocess
import sys

import pytest

LINE = re.compile(r"^\s+(PASS|FAIL)\s{2,}(.*)$")
BROWSER_MISSING = "playwright install"


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".py" and file_path.name.startswith("check_"):
        return CheckScript.from_parent(parent, path=file_path)


class CheckScript(pytest.File):
    def collect(self):
        result = subprocess.run(
            [sys.executable, str(self.path)],
            capture_output=True,
            text=True,
            cwd=str(self.path.parent.parent),
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        output = result.stdout + result.stderr
        skip_reason = None
        if BROWSER_MISSING in output:
            skip_reason = "Chromium is not installed for Playwright; run: python -m playwright install chromium"

        seen = {}
        items = []
        for line in result.stdout.splitlines():
            match = LINE.match(line)
            if not match:
                continue
            status, rest = match.groups()
            parts = re.split(r"\s{2,}", rest.strip(), maxsplit=1)
            name = parts[0]
            detail = parts[1] if len(parts) > 1 else ""
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"
            items.append(CheckItem.from_parent(self, name=name, passed=status == "PASS", detail=detail, skip_reason=skip_reason))

        if not items:
            detail = "" if result.returncode == 0 else output[-3000:]
            items.append(CheckItem.from_parent(self, name="script ran", passed=result.returncode == 0, detail=detail, skip_reason=skip_reason))
        return items


class CheckItem(pytest.Item):
    def __init__(self, *, passed, detail, skip_reason, **kwargs):
        super().__init__(**kwargs)
        self.passed = passed
        self.detail = detail
        self.skip_reason = skip_reason

    def runtest(self):
        if self.skip_reason and not self.passed:
            pytest.skip(self.skip_reason)
        assert self.passed, self.detail or "the script reported FAIL"

    def reportinfo(self):
        return self.path, None, self.name

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, AssertionError):
            return f"{self.path.name}: {self.name}\n{self.detail or 'FAIL'}"
        return super().repr_failure(excinfo)
