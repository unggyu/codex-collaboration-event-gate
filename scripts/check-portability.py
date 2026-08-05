#!/usr/bin/env python3
"""Reject machine-specific and generated artifacts from the source tree."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".yaml", ".yml", ""}
PATH_PATTERNS = (
    re.compile("/" + "home" + r"/[^/\\\s]+/"),
    re.compile("/" + "Users" + r"/[^/\\\s]+/"),
    re.compile(r"(?:[A-Za-z]:)?" + "\\\\" * 2 + "Users" + "\\\\"),
)
FORBIDDEN_PARTS = {
    ".codex-runtime",
    ".plugin-data",
    "audit",
    "sessions",
    "cache",
}


def source_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


problems: list[str] = []
for path in source_files():
    relative = path.relative_to(ROOT)
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        problems.append(f"generated/runtime path committed: {relative}")
    if path.suffix not in TEXT_SUFFIXES:
        continue
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for pattern in PATH_PATTERNS:
        if pattern.search(content):
            problems.append(f"machine-specific path in {relative}: {pattern.pattern}")
            break

if problems:
    print("FAIL: portability scan found forbidden source artifacts:", file=sys.stderr)
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    raise SystemExit(1)

print("PASS: source tree has no machine-specific paths or generated runtime artifacts.")
