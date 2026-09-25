#!/usr/bin/env python3
"""Enforce the rule that ``people_pubs.__version__`` must change whenever
transform logic changes.

Review alone does not catch this reliably, so the check is mechanical: if a
commit range (or the staged index) touches any designated transform path
without changing ``__version__`` in ``DB/people_pubs/__init__.py``, it fails.

Designated transform paths are the code that shapes what importers write to
the database — deliberately broad; a version bump on a borderline change is
cheap, a silent unbumped transform is not.

Usage:
  python DB/check_transform_version.py --staged            # pre-commit
  python DB/check_transform_version.py --base A --head B   # CI range

Escape hatch for changes inside those paths that provably do not alter any
written row (comment/docstring-only edits): set
``PEOPLE_PUBS_SKIP_TRANSFORM_VERSION_CHECK=1`` (or ``SKIP=db-transform-version``
with pre-commit) — and say why in the commit message.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

VERSION_FILE = "DB/people_pubs/__init__.py"
TRANSFORM_PATHS = (
    "DB/people_pubs/sync/",
    "DB/people_pubs/services/",
    "DB/people_pubs/db/",
    "DB/people_pubs/utils/",
    "DB/people_pubs/dedupe_policy.py",
    "DB/people_pubs/orcidkit.py",
    "DB/people_pubs/config.py",
)
_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _git(*args: str) -> str:
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    ).stdout.strip()
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True, check=True
    ).stdout


def _version_at(ref_path: str) -> str | None:
    try:
        content = _git("show", ref_path)
    except subprocess.CalledProcessError:
        return None
    match = _VERSION_RE.search(content)
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check the git index against HEAD")
    mode.add_argument("--base", help="base ref of the range to check")
    parser.add_argument("--head", default="HEAD", help="head ref of the range (default HEAD)")
    args = parser.parse_args()

    if os.environ.get("PEOPLE_PUBS_SKIP_TRANSFORM_VERSION_CHECK"):
        print("transform-version check skipped via PEOPLE_PUBS_SKIP_TRANSFORM_VERSION_CHECK")
        return 0

    if args.staged:
        changed = _git("diff", "--cached", "--name-only").splitlines()
        old_version = _version_at(f"HEAD:{VERSION_FILE}")
        new_version = _version_at(f":{VERSION_FILE}")
    else:
        try:
            _git("rev-parse", "--verify", f"{args.base}^{{commit}}")
        except subprocess.CalledProcessError:
            print(f"transform-version check: base ref {args.base!r} unavailable "
                  "(new branch or shallow clone) — skipping")
            return 0
        changed = _git("diff", "--name-only", f"{args.base}...{args.head}").splitlines()
        old_version = _version_at(f"{args.base}:{VERSION_FILE}")
        new_version = _version_at(f"{args.head}:{VERSION_FILE}")

    touched = sorted(
        path for path in changed
        if any(path == p or path.startswith(p) for p in TRANSFORM_PATHS)
    )
    if not touched:
        return 0
    if old_version is None:
        # Range predates D7's version stamp; nothing to compare against.
        return 0
    if new_version is not None and new_version != old_version:
        print(f"transform-version check: {old_version} -> {new_version} "
              f"({len(touched)} transform file(s) changed) — OK")
        return 0

    print(
        "Transform paths changed without a people_pubs.__version__ bump "
        f"(still {old_version!r}) — a version bump is required.\n"
        "Changed transform files:\n  " + "\n  ".join(touched) + "\n"
        f"Bump __version__ in {VERSION_FILE} (patch for bugfix-level changes, "
        "minor for behavior changes, major for output-shape changes).\n"
        "If no written row can differ (comments/docstrings only), rerun with "
        "PEOPLE_PUBS_SKIP_TRANSFORM_VERSION_CHECK=1 and justify it in the "
        "commit message.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
