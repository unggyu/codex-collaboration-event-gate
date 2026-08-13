#!/usr/bin/env python3
"""Small self-contained manifest check used by clean-clone CI."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
PLUGIN_NAME = "codex-collaboration-event-gate"
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


try:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "manifest must be a JSON object")
    require(value.get("name") == PLUGIN_NAME, "manifest name is invalid")
    require(isinstance(value.get("version"), str) and SEMVER.fullmatch(value["version"]), "version must be strict semver")
    require(isinstance(value.get("description"), str) and value["description"].strip(), "description is required")
    author = value.get("author")
    require(isinstance(author, dict) and isinstance(author.get("name"), str) and author["name"].strip(), "author.name is required")
    require(value.get("license") == "MIT", "license metadata must match LICENSE")
    require(value.get("skills") == "./skills/", "skills must use the portable default path")
    require("hooks" not in value, "default hooks/hooks.json discovery must not be overridden")
    interface = value.get("interface")
    require(isinstance(interface, dict), "interface metadata is required")
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        require(isinstance(interface.get(key), str) and interface[key].strip(), f"interface.{key} is required")
    require(not any("[TODO:" in str(item) for item in value.values()), "manifest contains a TODO placeholder")
except (OSError, json.JSONDecodeError, ValueError) as error:
    print(f"FAIL: invalid plugin manifest: {error}", file=sys.stderr)
    raise SystemExit(1)

print("PASS: self-contained plugin manifest contract is valid.")
