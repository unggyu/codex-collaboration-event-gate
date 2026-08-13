#!/usr/bin/env python3
"""Exercise the loaded-command runtime pin across installed-cache removal."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "codex-collaboration-lifecycle.py"
CONFIG = ROOT / "hooks" / "hooks.json"


def hook_commands() -> list[str]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    return [
        handler["command"]
        for groups in config["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]


def payload(event: str, session: str, agent: str = "") -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": event,
        "session_id": session,
        "turn_id": f"turn-{session}",
    }
    if event == "SessionStart":
        value["source"] = "startup"
    if agent:
        value.update({"agent_id": agent, "agent_type": "worker"})
    return value


def invoke(
    command: str,
    value: dict[str, object],
    plugin_data: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=json.dumps(value),
        text=True,
        capture_output=True,
        shell=True,
        env={**os.environ, "PLUGIN_DATA": str(plugin_data)},
        timeout=3,
        check=False,
    )
    if check:
        assert result.returncode == 0, result
        assert not result.stderr, result.stderr
    return result


commands = hook_commands()
assert len(commands) == 12
assert len(set(commands)) == 1
configured = commands[0]
digest = hashlib.sha256(HOOK.read_bytes()).hexdigest()
assert digest in configured
assert "${PLUGIN_ROOT}/hooks/codex-collaboration-lifecycle.py" in configured

with tempfile.TemporaryDirectory(prefix="codex-event-gate-runtime.") as name:
    temporary = Path(name)
    installed_root = temporary / "cache root" / "0.2.0+codex.test"
    installed_hook = installed_root / "hooks" / HOOK.name
    installed_hook.parent.mkdir(parents=True)
    shutil.copy2(HOOK, installed_hook)
    installed_hook.chmod(0o755)
    command = configured.replace("${PLUGIN_ROOT}", str(installed_root))
    plugin_data = temporary / "plugin-data"

    started = invoke(command, payload("SessionStart", "cache-removal"), plugin_data)
    assert "hookSpecificOutput" in json.loads(started.stdout)
    pin = plugin_data / "runtime" / f"{digest}.py"
    assert pin.read_bytes() == HOOK.read_bytes()
    assert stat.S_IMODE(pin.stat().st_mode) == 0o600

    invoke(
        command,
        payload("SubagentStart", "cache-removal", "worker-one"),
        plugin_data,
    )
    assert installed_root.is_dir()
    shutil.rmtree(installed_root)
    assert not installed_root.exists()

    stopped = invoke(
        command,
        payload("SubagentStop", "cache-removal", "worker-one"),
        plugin_data,
    )
    assert json.loads(stopped.stdout) == {}
    released = invoke(command, payload("Stop", "cache-removal"), plugin_data)
    assert json.loads(released.stdout) == {}
    print(
        "PASS: a session-pinned runtime completes SubagentStop and Stop after "
        "its installed cache snapshot is removed."
    )

    pin.write_bytes(b"print('tampered')\n")
    pin.chmod(0o600)
    rejected = invoke(
        command,
        payload("Stop", "cache-removal"),
        plugin_data,
        check=False,
    )
    assert rejected.returncode != 0
    assert "digest mismatch" in rejected.stderr
    assert "tampered" not in rejected.stdout
    print("PASS: a modified pinned runtime fails closed before execution.")

with tempfile.TemporaryDirectory(prefix="codex-event-gate-symlink.") as name:
    temporary = Path(name)
    installed_root = temporary / "cache root" / "0.2.0+codex.test"
    installed_hook = installed_root / "hooks" / HOOK.name
    installed_hook.parent.mkdir(parents=True)
    shutil.copy2(HOOK, installed_hook)
    installed_hook.chmod(0o755)
    command = configured.replace("${PLUGIN_ROOT}", str(installed_root))
    plugin_data = temporary / "plugin-data"
    runtime = plugin_data / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    pin = runtime / f"{digest}.py"
    pin.symlink_to(installed_hook)
    rejected = invoke(
        command,
        payload("SessionStart", "symlink-pin"),
        plugin_data,
        check=False,
    )
    assert rejected.returncode != 0
    assert not rejected.stdout
    print("PASS: a symlinked runtime pin is rejected without source fallback.")
