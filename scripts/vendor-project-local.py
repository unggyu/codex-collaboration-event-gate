#!/usr/bin/env python3
"""Install, update, or remove one project-local copy of this hook bundle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


PLUGIN_NAME = "codex-collaboration-event-gate"
OWNED_DESCRIPTION = (
    "Managed project-local vendoring for codex-collaboration-event-gate; "
    "do not enable the personal plugin at the same time."
)
HOOK_FILENAME = "codex-collaboration-lifecycle.py"
RUNNER_FILENAME = "run-hook.sh"


def fail(message: str) -> None:
    raise RuntimeError(message)


def secure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            fail(f"refusing non-directory or symlink path: {path}")
        return
    path.mkdir(mode=0o755)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            fail(f"refusing non-regular or symlink target: {path}")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def hook_config(runner: Path) -> dict[str, Any]:
    command = f"sh {shlex.quote(str(runner))}"

    def handler(status: str, timeout: int = 3, context_limit: int | None = None):
        value: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": timeout,
            "statusMessage": status,
        }
        if context_limit is not None:
            value["additionalContextLimit"] = context_limit
        return value

    return {
        "description": OWNED_DESCRIPTION,
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        handler(
                            "Initializing collaboration event gate",
                            context_limit=600,
                        )
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Agent|spawn_agent$",
                    "hooks": [handler("Recording Codex worker dispatch")],
                },
                {
                    "matcher": "wait_agent$",
                    "hooks": [handler("Checking Codex wait authorization")],
                },
                {
                    "matcher": "interrupt_agent$",
                    "hooks": [
                        handler("Recording exact Codex worker interruption")
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "Agent|spawn_agent$",
                    "hooks": [handler("Reconciling Codex worker dispatch")],
                },
                {
                    "matcher": "wait_agent$",
                    "hooks": [handler("Re-arming after a Codex wait event")],
                },
                {
                    "matcher": "interrupt_agent$",
                    "hooks": [
                        handler("Reconciling exact Codex worker interruption")
                    ],
                },
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        handler(
                            "Re-arming collaboration after steering",
                            context_limit=400,
                        )
                    ]
                }
            ],
            "SubagentStart": [
                {"hooks": [handler("Tracking Codex subagent")]}
            ],
            "SubagentStop": [
                {"hooks": [handler("Reconciling Codex subagent")]}
            ],
            "Stop": [
                {"hooks": [handler("Checking active Codex subagents")]}
            ],
            "SessionEnd": [
                {
                    "matcher": "other",
                    "hooks": [handler("Cleaning collaboration event state")],
                }
            ],
        },
    }


def runner_source() -> bytes:
    return b"""#!/bin/sh
set -eu
PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
export PLUGIN_ROOT
if [ -z "${PLUGIN_DATA:-}" ]; then
  runtime_root="${XDG_RUNTIME_DIR:-/tmp}"
  PLUGIN_DATA="$runtime_root/codex-collaboration-event-gate-$(id -u)"
  export PLUGIN_DATA
fi
exec python3 "$PLUGIN_ROOT/codex-collaboration-lifecycle.py"
"""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot verify existing JSON file {path}: {error}")
    if not isinstance(value, dict):
        fail(f"existing JSON file is not an object: {path}")
    return value


def is_owned_config(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and (
        read_json(path).get("description") == OWNED_DESCRIPTION
    )


def installed_in_codex() -> bool | None:
    executable = shutil.which("codex")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "plugin", "list", "--json"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    installed = value.get("installed", []) if isinstance(value, dict) else []
    for item in installed:
        if isinstance(item, str) and item.split("@", 1)[0] == PLUGIN_NAME:
            return True
        if isinstance(item, dict):
            name = item.get("name")
            if name == PLUGIN_NAME:
                return True
            plugin = item.get("plugin")
            if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
                return True
    return False


def locations(project: Path) -> tuple[Path, Path, Path, Path, Path]:
    codex_dir = project / ".codex"
    hooks_dir = codex_dir / "hooks"
    target = hooks_dir / PLUGIN_NAME
    return (
        codex_dir,
        hooks_dir,
        target,
        target / HOOK_FILENAME,
        codex_dir / "hooks.json",
    )


def install_or_update(
    action: str, project: Path, confirm_plugin_disabled: bool
) -> None:
    if not confirm_plugin_disabled:
        fail(
            "pass --confirm-plugin-disabled only after disabling or removing "
            "the installed plugin; two active copies would race"
        )
    installed = installed_in_codex()
    if installed is True:
        fail(
            f"{PLUGIN_NAME} is installed in Codex; run "
            f"'codex plugin remove {PLUGIN_NAME}@personal' and start a new "
            "session before vendoring"
        )

    codex_dir, hooks_dir, target, target_hook, config_path = locations(project)
    secure_directory(codex_dir)

    config_present = config_path.exists() or config_path.is_symlink()
    owned = config_present and is_owned_config(config_path)
    if config_present and not owned:
        fail(
            f"refusing to merge with unrelated active hook source: {config_path}"
        )
    if action == "install" and (owned or target.exists() or target.is_symlink()):
        fail("vendored copy already exists; use the update action")
    if action == "update" and not owned:
        fail("no owned vendored hooks.json exists to update")

    secure_directory(hooks_dir)
    secure_directory(target)

    plugin_root = Path(__file__).resolve().parent.parent
    source_hook = plugin_root / "hooks" / HOOK_FILENAME
    runner = target / RUNNER_FILENAME
    atomic_write(target_hook, source_hook.read_bytes(), 0o755)
    atomic_write(runner, runner_source(), 0o755)
    config = hook_config(runner.resolve())
    atomic_write(
        config_path,
        (json.dumps(config, indent=2) + "\n").encode("utf-8"),
        0o644,
    )
    print(f"{action}ed project-local hooks at {target}")
    if installed is None:
        print(
            "warning: Codex installed-plugin state could not be verified; "
            "the explicit disabled-plugin acknowledgement was trusted",
            file=sys.stderr,
        )


def uninstall(project: Path) -> None:
    _, hooks_dir, target, target_hook, config_path = locations(project)
    if not is_owned_config(config_path):
        fail("refusing to remove an unowned or missing project hooks.json")
    secure_directory(target)
    allowed = {HOOK_FILENAME, RUNNER_FILENAME}
    entries = {item.name for item in target.iterdir()}
    if entries != allowed:
        fail(f"refusing removal because vendored directory differs: {entries}")
    for path in (target_hook, target / RUNNER_FILENAME):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            fail(f"refusing unsafe vendored file: {path}")
    config_path.unlink()
    target_hook.unlink()
    (target / RUNNER_FILENAME).unlink()
    target.rmdir()
    if not any(hooks_dir.iterdir()):
        hooks_dir.rmdir()
    print(f"uninstalled project-local hooks from {project}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Vendor one project-local event gate without combining it with "
            "the installed personal plugin."
        )
    )
    parser.add_argument("action", choices=("install", "update", "uninstall"))
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument(
        "--confirm-plugin-disabled",
        action="store_true",
        help="acknowledge that the installed plugin is disabled or removed",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        fail(f"project path is not a directory: {project}")
    if args.action == "uninstall":
        uninstall(project)
    else:
        install_or_update(
            args.action, project, args.confirm_plugin_disabled
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
