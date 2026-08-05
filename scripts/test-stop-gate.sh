#!/usr/bin/env bash
# Deterministic regression tests for the immediate, interactive Stop gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOOK="$ROOT/hooks/codex-collaboration-lifecycle.py"
CONFIG="$ROOT/hooks/hooks.json"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-event-gate-stop.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

python3 - "$HOOK" "$CONFIG" "$TMP_ROOT" <<'PY'
import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

hook = Path(sys.argv[1])
config_path = Path(sys.argv[2])
tmp_root = Path(sys.argv[3])
source = hook.read_text(encoding="utf-8")
tree = ast.parse(source)

calls = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            calls.append(f"{getattr(node.func.value, 'id', '')}.{node.func.attr}")
        elif isinstance(node.func, ast.Name):
            calls.append(node.func.id)
assert "select.select" not in calls
assert "os.mkfifo" not in calls
assert "time.sleep" not in calls and "sleep" not in calls
assert not any(
    item in {"wait_agent", "list_agents"}
    or item.endswith(".wait_agent")
    or item.endswith(".list_agents")
    for item in calls
)

config = json.loads(config_path.read_text(encoding="utf-8"))["hooks"]
assert set(config) == {
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
}
for groups in config.values():
    for group in groups:
        for handler in group["hooks"]:
            assert "${PLUGIN_ROOT}/hooks/codex-collaboration-lifecycle.py" in handler["command"]
assert config["Stop"][0]["hooks"][0]["timeout"] == 3
assert {group.get("matcher") for group in config["PreToolUse"]} == {
    "Agent|spawn_agent$",
    "wait_agent$",
}
assert config["PostToolUse"][0]["matcher"] == "Agent|spawn_agent$"


def environment(state_name):
    value = os.environ.copy()
    value["PLUGIN_DATA"] = str(tmp_root / state_name)
    return value


def payload(event, session, agent="", active=False, source="startup"):
    value = {
        "cwd": "/not/a/git/repository/deep/path",
        "hook_event_name": event,
        "session_id": session,
        "stop_hook_active": active,
        "turn_id": f"turn-{session}",
    }
    if event == "SessionStart":
        value["source"] = source
    if agent:
        value.update({"agent_id": agent, "agent_type": "worker"})
    return value


def wait_payload(session, tool_input=None):
    return {
        "cwd": "/another/non-git/cwd",
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_input": {} if tool_input is None else tool_input,
        "tool_name": "collaborationwait_agent",
        "turn_id": f"turn-{session}",
    }


def run_hook(value, state_name, timeout=2):
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        env=environment(state_name),
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result
    assert not result.stderr, result.stderr
    return json.loads(result.stdout), elapsed


def permission(value):
    return value["hookSpecificOutput"]["permissionDecision"]


session_result, _ = run_hook(payload("SessionStart", "session"), "session")
context = session_result["hookSpecificOutput"]["additionalContext"]
assert "interruptible by steered user input" in context
print("PASS: SessionStart works from a non-git cwd and publishes interactive guidance.")

zero, elapsed = run_hook(payload("Stop", "zero"), "zero")
assert zero == {}
assert elapsed < 0.5
print("PASS: zero-worker Stop returns immediately.")

state_name = "active"
session = "active"
assert run_hook(payload("SubagentStart", session, "agent-a"), state_name)[0] == {}
blocked, elapsed = run_hook(payload("Stop", session), state_name)
assert elapsed < 0.5
assert blocked.get("decision") == "block", blocked
assert "returned immediately" in blocked["reason"]
assert "3600000ms" in blocked["reason"]
print("PASS: active-worker Stop returns immediately with one wait-arming continuation.")

allowed, _ = run_hook(wait_payload(session, {"timeout_ms": 10_000}), state_name)
output = allowed["hookSpecificOutput"]
assert output["permissionDecision"] == "allow"
assert output["updatedInput"]["timeout_ms"] == 3_600_000

repeat, _ = run_hook(wait_payload(session), state_name)
assert permission(repeat) == "deny"
assert "already consumed" in repeat["hookSpecificOutput"]["permissionDecisionReason"]

fail_closed, elapsed = run_hook(
    payload("Stop", session, active=True), state_name
)
assert elapsed < 0.5
assert fail_closed.get("continue") is False
assert "decision" not in fail_closed
print("PASS: repeated final and repeated waits fail closed without continuation loops.")

assert run_hook(payload("SubagentStop", session, "agent-a"), state_name)[0] == {}
finished, _ = run_hook(payload("Stop", session, active=True), state_name)
assert finished == {}
assert not list((tmp_root / state_name / "sessions").glob("*.json"))
assert list((tmp_root / state_name / "audit").glob("*.json"))
print("PASS: final worker completion releases state while bounded audit remains.")

# Concurrent lifecycle hooks serialize through the session lock.
state_name = "parallel"
session = "parallel"
agents = [f"agent-{index}" for index in range(16)]
with ThreadPoolExecutor(max_workers=16) as pool:
    list(
        pool.map(
            lambda agent: run_hook(
                payload("SubagentStart", session, agent), state_name
            ),
            agents,
        )
    )
state_file = next(
    path
    for path in (tmp_root / state_name).rglob("*.json")
    if not path.name.endswith(".recovery.json")
)
assert len(json.loads(state_file.read_text())["workers"]) == len(agents)
with ThreadPoolExecutor(max_workers=15) as pool:
    list(
        pool.map(
            lambda agent: run_hook(
                payload("SubagentStop", session, agent), state_name
            ),
            agents[:-1],
        )
    )
assert len(json.loads(state_file.read_text())["workers"]) == 1
blocked, elapsed = run_hook(payload("Stop", session), state_name)
assert elapsed < 0.5 and blocked.get("decision") == "block"
run_hook(payload("SubagentStop", session, agents[-1]), state_name)
assert run_hook(payload("Stop", session, active=True), state_name)[0] == {}
print("PASS: concurrent lifecycle updates preserve every active worker.")

state_name = "corrupt"
session = "corrupt"
run_hook(payload("SubagentStart", session, "agent-a"), state_name)
state_file = next(
    path
    for path in (tmp_root / state_name).rglob("*.json")
    if not path.name.endswith(".recovery.json")
)
state_file.write_text("{", encoding="utf-8")
corrupt, elapsed = run_hook(payload("Stop", session), state_name)
assert elapsed < 0.5
assert corrupt.get("decision") == "block"
assert "corrupt or unavailable" in corrupt["reason"]
corrupt_again, _ = run_hook(payload("Stop", session, active=True), state_name)
assert corrupt_again.get("continue") is False
print("PASS: corrupt state fails closed immediately and authorizes no Stop loop.")

print("PASS: immediate interactive Stop-gate suite completed.")
PY
