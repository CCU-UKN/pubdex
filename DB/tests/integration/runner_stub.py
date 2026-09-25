"""Trivial child module for refresh_state_runner integration tests.

Behavior is driven by env vars so the runner's job config controls it:
  RUNNER_STUB_BEHAVIOR = ok (default) | fail | sleep
  RUNNER_STUB_SLEEP    = seconds to sleep when behavior == sleep
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-person-id", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    behavior = os.environ.get("RUNNER_STUB_BEHAVIOR", "ok")
    if behavior == "sleep":
        time.sleep(float(os.environ.get("RUNNER_STUB_SLEEP", "10")))
    print(f"runner_stub person={args.only_person_id} behavior={behavior} dry_run={args.dry_run}")
    if behavior == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
