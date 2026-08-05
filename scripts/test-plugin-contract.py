#!/usr/bin/env python3
"""Static and portable-vendoring contract tests."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import re


ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "codex-collaboration-lifecycle.py"
CONFIG = ROOT / "hooks" / "hooks.json"
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
VENDOR = ROOT / "scripts" / "vendor-project-local.py"


def run(
    arguments: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=10,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result)
    return result


manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
assert manifest["name"] == ROOT.name
assert re.fullmatch(r"0\.1\.0(?:\+codex\.[A-Za-z0-9.-]+)?", manifest["version"])
assert manifest["license"] == "MIT"
assert manifest["author"]["name"] != "Local developer"
assert "hooks" not in manifest
assert not any("TODO" in str(value) for value in manifest.values())

config = json.loads(CONFIG.read_text(encoding="utf-8"))
assert CONFIG == ROOT / "hooks" / "hooks.json"
for groups in config["hooks"].values():
    for group in groups:
        for handler in group["hooks"]:
            command = handler["command"]
            assert "${PLUGIN_ROOT}" in command
            assert "git " not in command
            assert "$(git" not in command
assert config["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 3

source = HOOK.read_text(encoding="utf-8")
assert "PLUGIN_DATA" in source
assert "git rev-parse" not in source
assert "os.mkfifo" not in source
assert "select.select" not in source
tree = ast.parse(source)
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    name = ""
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = f"{getattr(node.func.value, 'id', '')}.{node.func.attr}"
    assert name not in {"sleep", "time.sleep", "wait_agent", "list_agents"}
print("PASS: manifest and default-discovery hook contract are portable.")

with tempfile.TemporaryDirectory(prefix="codex-event-gate-vendor.") as name:
    temporary = Path(name)
    fake_bin = temporary / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"installed\":[],\"available\":[]}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["PLUGIN_DATA"] = str(temporary / "plugin-data")

    project = temporary / "plain project"
    project.mkdir()
    rejected = run(
        [sys.executable, str(VENDOR), "install", "--project", str(project)],
        env=env,
        check=False,
    )
    assert rejected.returncode == 2
    assert "confirm-plugin-disabled" in rejected.stderr
    assert not (project / ".codex").exists()

    run(
        [
            sys.executable,
            str(VENDOR),
            "install",
            "--project",
            str(project),
            "--confirm-plugin-disabled",
        ],
        env=env,
    )
    hooks_json = project / ".codex" / "hooks.json"
    vendored = project / ".codex" / "hooks" / ROOT.name
    assert hooks_json.is_file()
    assert (vendored / HOOK.name).is_file()
    assert (vendored / "run-hook.sh").is_file()
    assert stat.S_IMODE((vendored / HOOK.name).stat().st_mode) == 0o755

    vendored_config = json.loads(hooks_json.read_text(encoding="utf-8"))
    command = vendored_config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    nested = project / "a" / "deep" / "cwd"
    nested.mkdir(parents=True)
    payload = {
        "cwd": str(nested),
        "hook_event_name": "SessionStart",
        "session_id": "non-git-vendor",
        "source": "startup",
    }
    result = subprocess.run(
        command,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        shell=True,
        cwd=nested,
        env=env,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "interruptible" in context

    run(
        [
            sys.executable,
            str(VENDOR),
            "update",
            "--project",
            str(project),
            "--confirm-plugin-disabled",
        ],
        env=env,
    )
    run(
        [
            sys.executable,
            str(VENDOR),
            "uninstall",
            "--project",
            str(project),
        ],
        env=env,
    )
    assert not hooks_json.exists()
    assert not vendored.exists()

    unrelated = temporary / "unrelated"
    (unrelated / ".codex").mkdir(parents=True)
    unrelated_config = unrelated / ".codex" / "hooks.json"
    sentinel = '{"description":"user-owned","hooks":{}}\n'
    unrelated_config.write_text(sentinel, encoding="utf-8")
    refused = run(
        [
            sys.executable,
            str(VENDOR),
            "install",
            "--project",
            str(unrelated),
            "--confirm-plugin-disabled",
        ],
        env=env,
        check=False,
    )
    assert refused.returncode == 2
    assert unrelated_config.read_text(encoding="utf-8") == sentinel

    symlinked = temporary / "symlinked"
    (symlinked / ".codex").mkdir(parents=True)
    external_config = temporary / "external-hooks.json"
    external_config.write_text(sentinel, encoding="utf-8")
    (symlinked / ".codex" / "hooks.json").symlink_to(external_config)
    refused = run(
        [
            sys.executable,
            str(VENDOR),
            "install",
            "--project",
            str(symlinked),
            "--confirm-plugin-disabled",
        ],
        env=env,
        check=False,
    )
    assert refused.returncode == 2
    assert external_config.read_text(encoding="utf-8") == sentinel
    assert not (symlinked / ".codex" / "hooks").exists()

    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        "'{\"installed\":[{\"name\":"
        f"\"{ROOT.name}\""
        "}],\"available\":[]}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    duplicate = temporary / "duplicate"
    duplicate.mkdir()
    refused = run(
        [
            sys.executable,
            str(VENDOR),
            "install",
            "--project",
            str(duplicate),
            "--confirm-plugin-disabled",
        ],
        env=env,
        check=False,
    )
    assert refused.returncode == 2
    assert "is installed in Codex" in refused.stderr
    assert not (duplicate / ".codex").exists()

print("PASS: project-local vendoring is non-git, cwd-independent, and collision-safe.")
