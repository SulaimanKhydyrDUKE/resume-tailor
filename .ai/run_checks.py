#!/usr/bin/env python3
"""Run the setup and check commands declared in .ai/project.yaml.

Used by .github/workflows/ci.yml so every repository built from the
template verifies itself the same way. Requires PyYAML.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    policy = yaml.safe_load((root / ".ai" / "project.yaml").read_text())
    commands = policy.get("commands") or {}
    workdir = root / (policy.get("working_directory") or ".")
    setup = (commands.get("setup") or "").strip()
    checks = [c for c in (commands.get("checks") or []) if str(c).strip()]

    if not checks:
        print("::error::.ai/project.yaml declares no checks; add at least one.")
        return 1

    steps = ([("setup", setup)] if setup else []) + [("check", c) for c in checks]
    for kind, command in steps:
        print(f"\n::group::{kind}: {command}", flush=True)
        result = subprocess.run(command, shell=True, cwd=workdir, env=os.environ)
        print("::endgroup::", flush=True)
        if result.returncode != 0:
            print(f"::error::{kind} failed ({result.returncode}): {command}")
            return result.returncode
    print(f"\nAll {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
