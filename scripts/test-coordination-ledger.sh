#!/usr/bin/env bash
# Deterministic regressions for hook-owned, session-bound ledger registration.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOOK="$ROOT/hooks/codex-collaboration-lifecycle.py"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-event-gate-ledger.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

python3 - "$HOOK" "$TMP_ROOT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

hook = Path(sys.argv[1])
tmp_root = Path(sys.argv[2])


def env(name):
    return {**os.environ, "PLUGIN_DATA": str(tmp_root / name)}


def invoke(value, name):
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        env=env(name),
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    return json.loads(result.stdout) if result.stdout else {}


def event(kind, session, *, agent="", tool_name="", tool_input=None, call="", response=None):
    value = {
        "hook_event_name": kind,
        "session_id": session,
        "turn_id": f"turn-{session}",
    }
    if agent:
        value.update({"agent_id": agent, "agent_type": "worker"})
    if tool_name:
        value.update(
            {
                "tool_name": tool_name,
                "tool_input": tool_input or {},
                "tool_use_id": call,
            }
        )
    if response is not None:
        value["tool_response"] = response
    return value


def root_spawn(
    name,
    lane,
    *,
    capacity,
    active,
    task=None,
    model="gpt-5.6-terra",
    classification="non-UI",
    pclass="read-only",
    gate="",
    fork="none",
    novel=False,
    override="",
    encrypted_v2=False,
):
    lines = [
        f"CLASSIFICATION: {classification}",
        f"PARALLELISM_CLASS: {pclass}",
        f"LEDGER_LANE: {lane}",
        f"LEDGER_SLOT_CAPACITY: {capacity}",
        f"LEDGER_CURRENT_ACTIVE: {active}",
    ]
    if gate:
        lines.append(f"EXCLUSIVE_GATE: {gate}")
    if novel:
        lines.append("NOVEL_UI_COMPLEXITY: high")
    if override:
        lines.append(f"SOL_OVERRIDE_REASON: {override}")
    explicit_task = task
    task = task or f"task-{lane}"
    message = "\n".join(lines)
    if encrypted_v2:
        classification_code = {"non-UI": "n", "UI": "u"}[classification]
        parallelism_code = {"read-only": "r", "isolated-write": "w"}[pclass]
        sol_policy = "h" if novel else "u" if override == "user-requested" else "d"
        task = explicit_task or (
            f"cceg1_{classification_code}_{parallelism_code}_{lane}_"
            f"{capacity}_{active}_{sol_policy}"
        )
        message = "gAAAA" + "A" * 96
    tool_input = {
        "task_name": task,
        "message": message,
        "fork_turns": fork,
    }
    if model is not None:
        tool_input["model"] = model
    call = f"spawn-{lane}"
    output = invoke(
        event("PreToolUse", name, tool_name="Agent", tool_input=tool_input, call=call),
        name,
    )
    return output, tool_input, call, task


def accept_and_start(name, spawn, agent):
    _, tool_input, call, task = spawn
    assert invoke(
        event(
            "PostToolUse",
            name,
            tool_name="Agent",
            tool_input=tool_input,
            call=call,
            response={"task_name": f"/root/{task}"},
        ),
        name,
    ) == {}
    assert invoke(event("SubagentStart", name, agent=agent), name) == {}


def wait(name, call="wait"):
    return invoke(
        event(
            "PreToolUse",
            name,
            tool_name="collaborationwait_agent",
            tool_input={},
            call=call,
        ),
        name,
    )


def permission(output):
    return output["hookSpecificOutput"]["permissionDecision"]


def state_file(name):
    files = list((tmp_root / name / "sessions").glob("*.json"))
    assert len(files) == 1, files
    return files[0]


# Activation integration: this is exactly the new-session root workflow. The
# root has only ordinary spawn_agent and wait_agent capability; it never sees
# PLUGIN_DATA or supplies a session id to a helper. The wait PreToolUse hook
# creates the ledger from the two bound spawn capabilities and permits the wait.
name = "fresh-root-activation"
started = invoke(
    {"hook_event_name": "SessionStart", "session_id": name, "source": "startup"},
    name,
)
assert "cceg1" in started["hookSpecificOutput"]["additionalContext"]
first = root_spawn(name, "lane-one", capacity=2, active=2)
second = root_spawn(name, "lane-two", capacity=2, active=2)
assert first[0] == second[0] == {}
accept_and_start(name, first, "worker-one")
accept_and_start(name, second, "worker-two")
allowed = wait(name)
assert permission(allowed) == "allow", allowed
assert allowed["hookSpecificOutput"]["updatedInput"]["timeout_ms"] == 3_600_000
snapshot = json.loads(state_file(name).read_text(encoding="utf-8"))
assert snapshot["ledger"]["active_count"] == 2
assert len(snapshot["ledger"]["lanes"]) == 2
assert name not in state_file(name).read_text(encoding="utf-8")
print("PASS: a fresh session registers through root spawn PreToolUse then allows wait_agent without a callable helper.")


# Codex Multi-Agent V2 encrypts spawn_agent.message before PreToolUse. The
# visible task_name capability therefore carries only the fixed, non-secret
# coordination declaration. It must drive the same spawn -> wait -> completion
# -> final-release lifecycle without decrypting or retaining task content.
name = "encrypted-v2-activation"
started = invoke(
    {"hook_event_name": "SessionStart", "session_id": name, "source": "startup"},
    name,
)
assert "cceg1" in started["hookSpecificOutput"]["additionalContext"]
encrypted = root_spawn(
    name,
    "isolatedsmoke",
    capacity=1,
    active=1,
    encrypted_v2=True,
)
assert encrypted[0] == {}, encrypted[0]
accept_and_start(name, encrypted, "encrypted-worker")
allowed = wait(name, "encrypted-wait")
assert permission(allowed) == "allow", allowed
assert allowed["hookSpecificOutput"]["updatedInput"]["timeout_ms"] == 3_600_000
assert invoke(event("SubagentStop", name, agent="encrypted-worker"), name) == {}
assert invoke(event("Stop", name), name) == {}
assert not list((tmp_root / name / "sessions").glob("*.json"))
print("PASS: encrypted V2 dispatch uses a visible task capability and releases final after completion.")


name = "encrypted-v2-missing-capability"
denied, _, _, _ = root_spawn(
    name,
    "isolatedsmoke",
    capacity=1,
    active=1,
    task="ordinary_task",
    encrypted_v2=True,
)
assert permission(denied) == "deny", denied
assert "encrypted V2 message requires a visible cceg1" in denied["hookSpecificOutput"]["permissionDecisionReason"]
assert not list((tmp_root / name / "sessions").glob("*.json"))

name = "encrypted-v2-malformed-capability"
denied = invoke(
    event(
        "PreToolUse",
        name,
        tool_name="Agent",
        tool_input={
            "task_name": "cceg1_n_x_unsupported_1_1_d",
            "message": "gAAAA" + "A" * 96,
            "model": "gpt-5.6-terra",
            "fork_turns": "none",
        },
        call="spawn-malformed",
    ),
    name,
)
assert permission(denied) == "deny", denied
assert "V2 task_name capability must match" in denied["hookSpecificOutput"]["permissionDecisionReason"]
assert not list((tmp_root / name / "sessions").glob("*.json"))

name = "encrypted-v2-mixed-metadata"
denied = invoke(
    event(
        "PreToolUse",
        name,
        tool_name="Agent",
        tool_input={
            "task_name": "cceg1_n_r_mixedmetadata_1_1_d",
            "message": "\n".join(
                (
                    "CLASSIFICATION: non-UI",
                    "PARALLELISM_CLASS: read-only",
                    "LEDGER_LANE: mixedmetadata",
                    "LEDGER_SLOT_CAPACITY: 1",
                    "LEDGER_CURRENT_ACTIVE: 1",
                )
            ),
            "model": "gpt-5.6-terra",
            "fork_turns": "none",
        },
        call="spawn-mixed",
    ),
    name,
)
assert permission(denied) == "deny", denied
assert "must not be combined" in denied["hookSpecificOutput"]["permissionDecisionReason"]

name = "encrypted-v2-novel-ui-sol"
allowed, _, _, _ = root_spawn(
    name,
    "noveluisol",
    capacity=1,
    active=1,
    model="gpt-5.6-sol",
    classification="UI",
    novel=True,
    encrypted_v2=True,
)
assert allowed == {}, allowed
print("PASS: encrypted V2 capabilities reject ambiguity and retain narrow Sol policy.")

name = "fresh-root-activation"


# Exact audit repro: a count of two with only worker one mapped used to permit
# wait. A syntactically valid but incomplete persisted ledger must now fail.
snapshot["ledger"]["lanes"].pop(next(iter(snapshot["ledger"]["lanes"])))
snapshot["wait_issued_epoch"] = -1
snapshot["wait_call_hash"] = ""
state_file(name).write_text(json.dumps(snapshot), encoding="utf-8")
denied = wait(name, "wait-omitted")
assert permission(denied) == "deny", denied
assert "map every tracked active or pending native dispatch exactly once" in denied["hookSpecificOutput"]["permissionDecisionReason"]
print("PASS: count-only ledger coverage cannot omit one of two tracked workers.")


def malformed_ledger_case(kind):
    name = f"coverage-{kind}"
    one = root_spawn(name, "lane-one", capacity=2, active=2)
    two = root_spawn(name, "lane-two", capacity=2, active=2)
    accept_and_start(name, one, "worker-one")
    accept_and_start(name, two, "worker-two")
    assert permission(wait(name, "wait-good")) == "allow"
    path = state_file(name)
    state = json.loads(path.read_text(encoding="utf-8"))
    lanes = list(state["ledger"]["lanes"].values())
    if kind == "duplicate":
        lanes[1]["dispatch_map"] = dict(lanes[0]["dispatch_map"])
    else:
        lanes[0]["dispatch_map"] = {
            "task_name": "",
            "tool_use_id": "",
            "tool_use_hash": "",
            "agent_id": hashlib.sha256(b"phantom-worker").hexdigest(),
            "target_hash": "",
        }
    state["wait_issued_epoch"] = -1
    state["wait_call_hash"] = ""
    path.write_text(json.dumps(state), encoding="utf-8")
    denied = wait(name, f"wait-{kind}")
    assert permission(denied) == "deny", denied


malformed_ledger_case("duplicate")
malformed_ledger_case("phantom")
print("PASS: duplicate and phantom lane mappings fail closed.")


# The caller's declared active count is only an assertion. A partially
# dispatched two-lane batch cannot turn that count into wait authority.
name = "declared-count-mismatch"
spawn = root_spawn(name, "lane-one", capacity=2, active=2)
accept_and_start(name, spawn, "worker-one")
denied = wait(name)
assert permission(denied) == "deny", denied
assert "could not verify every root dispatch declaration" in denied["hookSpecificOutput"]["permissionDecisionReason"]
print("PASS: declared active count is checked against observed tracked dispatches.")


# Exact exclusive gates remain serialized, while root spawn policy rejects
# missing model, non-UI Sol, and fork_turns=all before native dispatch.
name = "duplicate-gate"
assert root_spawn(name, "one", capacity=2, active=2, pclass="exclusive-gate", gate="git-ref:origin/qa")[0] == {}
assert root_spawn(name, "two", capacity=2, active=2, pclass="exclusive-gate", gate="git-ref:origin/qa")[0] == {}
denied = wait(name)
assert permission(denied) == "deny"
assert "hook-owned session ledger" in denied["hookSpecificOutput"]["permissionDecisionReason"]
for name, kwargs in (
    ("missing-model", {"model": None}),
    ("non-ui-sol", {"model": "gpt-5.6-sol"}),
    ("fork-all", {"fork": "all"}),
):
    denied, _, _, _ = root_spawn(name, "lane", capacity=1, active=1, **kwargs)
    assert permission(denied) == "deny", denied
name = "novel-ui-sol"
assert root_spawn(name, "lane", capacity=1, active=1, model="gpt-5.6-sol", classification="UI", novel=True)[0] == {}
print("PASS: exact gate and model/fork policy checks remain fail-closed.")


# A verified batch can shrink after a completion. The hook, not a caller,
# refreshes the surviving bound dispatch's active count for the next epoch.
name = "completion-refresh"
one = root_spawn(name, "lane-one", capacity=2, active=2)
two = root_spawn(name, "lane-two", capacity=2, active=2)
accept_and_start(name, one, "worker-one")
accept_and_start(name, two, "worker-two")
assert permission(wait(name, "wait-before-completion")) == "allow"
assert invoke(event("SubagentStop", name, agent="worker-one"), name) == {}
assert permission(wait(name, "wait-after-completion")) == "allow"
print("PASS: verified lifecycle completion refreshes only hook-bound active count for one re-arm.")


# A failed follow-up spawn after a verified wait must restore the surviving
# batch count inside the hook, rather than stranding the prior worker.
name = "failed-follow-up-refresh"
first = root_spawn(name, "lane-one", capacity=2, active=1)
accept_and_start(name, first, "worker-one")
assert permission(wait(name, "wait-before-failure")) == "allow"
failed = root_spawn(name, "lane-two", capacity=2, active=2)
assert failed[0] == {}
_, failed_input, failed_call, _ = failed
assert invoke(
    event(
        "PostToolUse",
        name,
        tool_name="Agent",
        tool_input=failed_input,
        call=failed_call,
        response={"isError": True},
    ),
    name,
) == {}
assert permission(wait(name, "wait-after-failure")) == "allow"
print("PASS: a failed follow-up dispatch restores hook-bound active count for one re-arm.")


# Steering invalidates the stored epoch. The next normal root wait calls the
# same hook-owned registrar, rebuilding only from still-tracked capabilities.
name = "steering"
spawn = root_spawn(name, "lane", capacity=1, active=1)
accept_and_start(name, spawn, "worker")
assert permission(wait(name, "wait-first")) == "allow"
steered = invoke(event("UserPromptSubmit", name), name)
assert "newly authorized wait_agent" in steered["hookSpecificOutput"]["additionalContext"]
assert permission(wait(name, "wait-after-steering")) == "allow"
print("PASS: steering invalidates and hook-owned registration rebuilds the ledger for one new wait.")
PY
