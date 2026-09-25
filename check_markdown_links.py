#!/usr/bin/env python3
"""Lightweight Markdown consistency check over the publishable tree (stdlib only).

Two failure classes that keep biting fresh clones:

1. relative link targets that do not exist in the repository
2. absolute filesystem link targets or absolute `cd /...` command examples,
   which only work on one contributor's machine

Checked files: every *.md the repository would publish — the tracked files plus
untracked, nonignored ones — so a new document is checked while it is being
written, not only once it has been staged. Ignored paths (local working files,
virtualenvs, caches) stay out, and in a clean clone the set is exactly the
tracked files. External links (http/https/mailto), pure anchors (#...), and
fenced-code content are left alone except for the absolute-path scan, which
deliberately includes command examples.

Usage: python3 check_markdown_links.py  (exit 1 on findings)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_RE = re.compile(r"\!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ABS_PATH_RE = re.compile(r"(?:\]\(|\bcd\s+)(/(?:home|Users)/)")

# Documented-by-example placeholders that are not real targets.
PLACEHOLDER_MARKERS = ("<", ">", "$", "{", "}", "...")


def candidate_files() -> set[str]:
    """The publishable tree: tracked paths plus untracked, nonignored ones."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {line.strip() for line in out if line.strip()}


def candidate_markdown(candidates: set[str]) -> list[Path]:
    return [REPO_ROOT / line for line in sorted(candidates) if line.endswith(".md")]


def strip_fenced_code(text: str) -> str:
    """Remove fenced code blocks so shell snippets don't parse as links."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def check_file(path: Path, candidates: set[str]) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")

    for match in ABS_PATH_RE.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        problems.append(
            f"{path.relative_to(REPO_ROOT)}:{line_no}: absolute machine path "
            f"'{match.group(1)}...' — use a repo-relative path"
        )

    prose = strip_fenced_code(text)
    for regex in (LINK_RE, IMAGE_RE):
        for match in regex.finditer(prose):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if any(marker in target for marker in PLACEHOLDER_MARKERS):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            if target_path.startswith("/"):
                # already reported by the absolute-path scan
                continue
            # Validate against the PUBLISHABLE file set, not the local disk:
            # ignored local files (DB/.env, venvs, backups) exist on a
            # configured machine but not in the published repository, in CI,
            # or in a fresh clone — a link to one is broken for every reader.
            resolved = (path.parent / target_path).resolve()
            try:
                rel = resolved.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}: link escapes the "
                    f"repository -> {target_path}"
                )
                continue
            is_candidate_file = rel in candidates
            is_candidate_dir = any(c.startswith(rel + "/") for c in candidates)
            if not (is_candidate_file or is_candidate_dir):
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}: broken relative link "
                    f"-> {target_path} (not a publishable file/directory)"
                )
    return problems


def main() -> int:
    problems: list[str] = []
    candidates = candidate_files()
    files = candidate_markdown(candidates)
    for path in files:
        if not path.exists():
            continue
        problems.extend(check_file(path, candidates))
    if problems:
        print(f"{len(problems)} markdown consistency problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"markdown link check ok ({len(files)} publishable files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
