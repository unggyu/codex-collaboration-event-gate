#!/usr/bin/env bash
# Deterministic regression tests for one-shot native wait_agent authorization.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
HOOK="$ROOT/hooks/codex-collaboration-lifecycle.py"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-event-gate-wait.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

python3 - "$HOOK" "$TMP_ROOT" <<'PY'
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

hook = Path(sys.argv[1])
tmp_root = Path(sys.argv[2])


def environment(state_name):
    value = os.environ.copy()
    value["PLUGIN_DATA"] = str(tmp_root / state_name)
    return value


def lifecycle(event, session, agent="", active=False, turn_id=None):
    value = {
        "cwd": "/safe/non-git/project",
        "hook_event_name": event,
        "session_id": session,
        "stop_hook_active": active,
        "turn_id": turn_id or f"turn-{session}",
    }
    if agent:
        value.update({"agent_id": agent, "agent_type": "worker"})
    return value


def wait_payload(
    session,
    tool_input=None,
    tool_name="collaborationwait_agent",
    child=False,
    call_id=None,
):
    value = {
        "cwd": "/safe/non-git/project",
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_input": {} if tool_input is None else tool_input,
        "tool_name": tool_name,
        "tool_use_id": call_id or f"wait-{session}",
        "turn_id": f"turn-{session}",
    }
    if child:
        value.update({"agent_id": "child-a", "agent_type": "worker"})
    return value


def tool_post(session, call_id, tool_name, tool_input, response):
    return {
        "cwd": "/safe/non-git/project",
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_input": tool_input,
        "tool_name": tool_name,
        "tool_response": response,
        "tool_use_id": call_id,
        "turn_id": f"turn-{session}",
    }


def interrupt_payload(session, call_id, target, event="PreToolUse", response=None):
    value = {
        "cwd": "/safe/non-git/project",
        "hook_event_name": event,
        "session_id": session,
        "tool_input": {"target": target},
        "tool_name": "collaborationinterrupt_agent",
        "tool_use_id": call_id,
        "turn_id": f"turn-{session}",
    }
    if event == "PostToolUse":
        value["tool_response"] = response
    return value


def spawn_payload(
    session,
    call_id,
    *,
    child=False,
    tool_name="Agent",
    task_name="opaque-test-worker",
    ledger_capacity=1,
    ledger_active=1,
    batch_id="batchone",
    batch_position=1,
    batch_size=None,
):
    batch_size = batch_size or ledger_capacity
    message = "\n".join(
        (
            "CLASSIFICATION: non-UI",
            "PARALLELISM_CLASS: read-only",
            f"LEDGER_LANE: {call_id}",
            f"LEDGER_SLOT_CAPACITY: {ledger_capacity}",
            f"LEDGER_CURRENT_ACTIVE: {ledger_active}",
            f"LEDGER_BATCH_ID: {batch_id}",
            f"LEDGER_BATCH_POSITION: {batch_position}",
            f"LEDGER_BATCH_SIZE: {batch_size}",
        )
    )
    value = {
        "cwd": "/safe/non-git/project",
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_input": {
            "task_name": task_name,
            "message": message,
            "model": "gpt-5.6-terra",
            "fork_turns": "none",
        },
        "tool_name": tool_name,
        "tool_use_id": call_id,
        "turn_id": f"turn-{session}",
    }
    if child:
        value.update({"agent_id": "child-a", "agent_type": "worker"})
    return value


def spawn_post(session, call_id, response, task_name="opaque-test-worker"):
    return {
        "cwd": "/safe/non-git/project",
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_input": {"task_name": task_name},
        "tool_name": "Agent",
        "tool_response": response,
        "tool_use_id": call_id,
        "turn_id": f"turn-{session}",
    }


def steer(session):
    return {
        "cwd": "/safe/non-git/project",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "New user direction",
        "session_id": session,
        "turn_id": f"steer-{session}",
    }


def invoke_raw(raw, state_name, timeout=2):
    return subprocess.run(
        [sys.executable, str(hook)],
        input=raw,
        text=True,
        capture_output=True,
        env=environment(state_name),
        timeout=timeout,
        check=False,
    )


def run_hook(value, state_name, timeout=2):
    result = invoke_raw(json.dumps(value), state_name, timeout)
    assert result.returncode == 0, result
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


def output(value):
    item = value.get("hookSpecificOutput", {})
    assert item.get("hookEventName") == "PreToolUse", value
    return item


def assert_allowed(value, original=None):
    item = output(value)
    assert item["permissionDecision"] == "allow", value
    assert item["updatedInput"]["timeout_ms"] == 3_600_000
    for key, expected in (original or {}).items():
        if key != "timeout_ms":
            assert item["updatedInput"].get(key) == expected
    return item


def assert_denied(value, fragment=""):
    item = output(value)
    assert item["permissionDecision"] == "deny", value
    reason = item["permissionDecisionReason"]
    assert "Do not retry" in reason or "Do not retry or poll" in reason
    if fragment:
        assert fragment in reason, value
    return item


def state_files(state_name):
    sessions = tmp_root / state_name / "sessions"
    return [
        path
        for path in sessions.glob("*.json")
        if not path.name.endswith(".recovery.json")
    ]


def recovery_files(state_name):
    return list((tmp_root / state_name).rglob("*.recovery.json"))


# A dispatch authorizes one wait. Short/default inputs are rewritten, and every
# native spelling follows the same guard.
for index, tool_name in enumerate(
    ("wait_agent", "collaborationwait_agent", "multi_agent_v1wait_agent")
):
    state_name = f"variant-{index}"
    session = state_name
    run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
    original = {"targets": ["agent-a"], "timeout_ms": 10_000}
    assert_allowed(
        run_hook(
            wait_payload(session, original, tool_name=tool_name), state_name
        ),
        original,
    )
    assert_denied(
        run_hook(wait_payload(session, {}, tool_name=tool_name), state_name),
        "already consumed",
    )
print("PASS: every native wait spelling is rewritten once and repeated waits are denied.")

# A native wait may return for an intermediate MESSAGE without completing the
# worker. Its matching PostToolUse event re-arms exactly one subscription. A
# timeout does not, so the hook never turns timeouts into a polling loop.
state_name = "wait-intermediate-message"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
run_hook(lifecycle("SubagentStart", session, "agent-b"), state_name)
first_wait = "wait-message-first"
assert_allowed(
    run_hook(wait_payload(session, call_id=first_wait), state_name)
)
returned = run_hook(
    tool_post(
        session,
        first_wait,
        "collaborationwait_agent",
        {"timeout_ms": 3_600_000},
        '{"message":"Wait completed.","timed_out":false}',
    ),
    state_name,
)
assert "MESSAGE/FINAL/error" in returned["hookSpecificOutput"]["additionalContext"]
assert_allowed(
    run_hook(wait_payload(session, call_id="wait-message-second"), state_name)
)
assert_denied(
    run_hook(wait_payload(session, call_id="wait-message-third"), state_name),
    "already consumed",
)
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert set(snapshot["workers"]) == {"agent-a", "agent-b"}

state_name = "wait-timeout"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
timeout_wait = "wait-timeout-first"
assert_allowed(run_hook(wait_payload(session, call_id=timeout_wait), state_name))
assert run_hook(
    tool_post(
        session,
        timeout_wait,
        "collaborationwait_agent",
        {"timeout_ms": 3_600_000},
        {"message": "Wait timed out.", "timed_out": True},
    ),
    state_name,
) == {}
assert_denied(
    run_hook(wait_payload(session, call_id="wait-timeout-second"), state_name),
    "already consumed",
)
print("PASS: non-timeout wait returns re-arm once while timeouts never create polling authority.")

# A successful interrupt removes only the capability's exact target. A later
# duplicate SubagentStop for that target cannot consume or remove another
# worker, and an unconfirmed interrupt response removes nothing.
state_name = "interrupt-one-of-two"
session = state_name
for suffix in ("a", "b"):
    task_name = f"worker-{suffix}"
    spawn_call = f"spawn-{suffix}"
    run_hook(
        spawn_payload(
            session,
            spawn_call,
            task_name=task_name,
            ledger_capacity=2,
            ledger_active=2,
            batch_id="interruptbatch",
            batch_position=1 if suffix == "a" else 2,
            batch_size=2,
        ),
        state_name,
    )
    run_hook(
        spawn_post(
            session,
            spawn_call,
            {"task_name": f"/root/{task_name}"},
            task_name=task_name,
        ),
        state_name,
    )
    run_hook(
        lifecycle("SubagentStart", session, f"opaque-agent-{suffix}"),
        state_name,
    )
assert_allowed(
    run_hook(wait_payload(session, call_id="wait-before-interrupt"), state_name)
)
interrupt_call = "interrupt-agent-a"
assert run_hook(
    interrupt_payload(session, interrupt_call, "/root/worker-a"), state_name
) == {}
assert_denied(
    run_hook(
        interrupt_payload(session, interrupt_call, "/root/worker-b"),
        state_name,
    ),
    "already bound",
)
interrupted = run_hook(
    interrupt_payload(
        session,
        interrupt_call,
        "/root/worker-a",
        event="PostToolUse",
        response='{"previous_status":"running"}',
    ),
    state_name,
)
assert "exact interrupted target" in interrupted["hookSpecificOutput"]["additionalContext"]
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert set(snapshot["workers"]) == {"opaque-agent-b"}
assert_allowed(
    run_hook(wait_payload(session, call_id="wait-after-interrupt"), state_name)
)
run_hook(lifecycle("SubagentStop", session, "opaque-agent-a"), state_name)
assert_denied(
    run_hook(wait_payload(session, call_id="wait-after-duplicate-stop"), state_name),
    "already consumed",
)
assert run_hook(lifecycle("Stop", session), state_name).get("decision") == "block"

failed_call = "interrupt-agent-b-failed"
assert run_hook(
    interrupt_payload(session, failed_call, "/root/worker-b"), state_name
) == {}
assert run_hook(
    interrupt_payload(
        session,
        failed_call,
        "/root/worker-b",
        event="PostToolUse",
        response={"error": "native interruption failed"},
    ),
    state_name,
) == {}
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert set(snapshot["workers"]) == {"opaque-agent-b"}

ambiguous_call = "interrupt-agent-b-ambiguous"
assert run_hook(
    interrupt_payload(session, ambiguous_call, "/root/worker-b"), state_name
) == {}
assert run_hook(
    interrupt_payload(
        session,
        ambiguous_call,
        "/root/worker-b",
        event="PostToolUse",
        response={"status": "cancelled"},
    ),
    state_name,
) == {}
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert set(snapshot["workers"]) == {"opaque-agent-b"}

terminal_call = "interrupt-agent-b-terminal"
assert run_hook(
    interrupt_payload(session, terminal_call, "/root/worker-b"), state_name
) == {}
run_hook(
    interrupt_payload(
        session,
        terminal_call,
        "/root/worker-b",
        event="PostToolUse",
        response={"status": "cancelled", "success": True},
    ),
    state_name,
)
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
print("PASS: interrupt reconciliation removes exactly one confirmed target and preserves every other worker.")

# Native completed statuses carry their worker result as a tagged object,
# unlike running statuses, which are returned as a string. A completed pending
# dispatch must still be cleared by its exact interrupt capability.
state_name = "interrupt-completed-pending"
session = state_name
spawn_call = "spawn-completed-pending"
task_name = "completed-pending-worker"
run_hook(
    spawn_payload(session, spawn_call, task_name=task_name), state_name
)
run_hook(
    spawn_post(
        session,
        spawn_call,
        {"task_name": f"/root/{task_name}"},
        task_name=task_name,
    ),
    state_name,
)
ambiguous_call = "interrupt-completed-pending-ambiguous"
assert run_hook(
    interrupt_payload(session, ambiguous_call, f"/root/{task_name}"),
    state_name,
) == {}
assert run_hook(
    interrupt_payload(
        session,
        ambiguous_call,
        f"/root/{task_name}",
        event="PostToolUse",
        response={
            "previous_status": {
                "completed": "bounded worker result",
                "running": None,
            }
        },
    ),
    state_name,
) == {}
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert len(snapshot["pending"]) == 1

interrupt_call = "interrupt-completed-pending"
assert run_hook(
    interrupt_payload(session, interrupt_call, f"/root/{task_name}"),
    state_name,
) == {}
interrupted = run_hook(
    interrupt_payload(
        session,
        interrupt_call,
        f"/root/{task_name}",
        event="PostToolUse",
        response={"previous_status": {"completed": "bounded worker result"}},
    ),
    state_name,
)
assert "exact interrupted target" in interrupted["hookSpecificOutput"]["additionalContext"]
snapshot = json.loads(state_files(state_name)[0].read_text(encoding="utf-8"))
assert snapshot["workers"] == {} and snapshot["pending"] == {}
assert run_hook(lifecycle("Stop", session), state_name) == {}
print("PASS: tagged completed interrupt responses clear the exact pending dispatch.")

# PreToolUse(spawn_agent) is the durable authority. It must allow the first
# wait and deny Stop while a later or dropped SubagentStart has not arrived.
state_name = "pending-start-gap"
session = state_name
run_hook(spawn_payload(session, "call-gap", tool_name="collaborationspawn_agent"), state_name)
run_hook(spawn_post(session, "call-gap", {"task_name": "opaque-test-worker"}), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
blocked_stop = run_hook(lifecycle("Stop", session), state_name)
assert blocked_stop.get("decision") == "block", blocked_stop
assert "pending dispatches" in blocked_stop["reason"]
run_hook(
    lifecycle(
        "SubagentStart", session, "agent-gap", turn_id="child-turn-gap"
    ),
    state_name,
)
run_hook(
    lifecycle("SubagentStop", session, "agent-gap", turn_id="child-turn-gap"),
    state_name,
)
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
audit = list((tmp_root / state_name / "audit").glob("*.json"))
assert len(audit) == 1
records = json.loads(audit[0].read_text(encoding="utf-8"))
assert all("session_id" not in item and "task_name" not in item for item in records)
assert {item["outcome"] for item in records} >= {
    "spawn-recorded", "start-promoted-pending", "stop-worker"
}
print("PASS: a pending spawn closes the Start delivery gap across parent/child turns and keeps only opaque audit metadata.")

# Native lifecycle events identify the child turn, not the parent spawn turn.
# Multiple accepted dispatches therefore pair to Start events in session-local
# FIFO order and each corresponding Stop leaves no stale pending authority.
state_name = "fifo-parent-child-turns"
session = state_name
for position, call in enumerate(("call-first", "call-second", "call-third"), start=1):
    run_hook(
        spawn_payload(
            session,
            call,
            ledger_capacity=3,
            ledger_active=3,
            batch_id="fifobatch",
            batch_position=position,
            batch_size=3,
        ),
        state_name,
    )
    run_hook(spawn_post(session, call, {"accepted": True}), state_name)
state_file = state_files(state_name)[0]
initial = json.loads(state_file.read_text(encoding="utf-8"))
assert len(initial["pending"]) == 3
assert initial["dispatch_sequence"] == 3
assert_allowed(run_hook(wait_payload(session), state_name))
for index, agent in enumerate(("agent-one", "agent-two", "agent-three"), start=1):
    child_turn = f"child-turn-{index}"
    run_hook(
        lifecycle("SubagentStart", session, agent, turn_id=child_turn),
        state_name,
    )
    snapshot = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(snapshot["pending"]) == 3 - index
    expected_pending = {
        hashlib.sha256(call.encode()).hexdigest()
        for call in ("call-first", "call-second", "call-third")[index:]
    }
    assert set(snapshot["pending"]) == expected_pending
    assert snapshot["dispatch_sequence"] == 3
    assert agent in snapshot["workers"]
for index, agent in enumerate(("agent-one", "agent-two", "agent-three"), start=1):
    run_hook(
        lifecycle("SubagentStop", session, agent, turn_id=f"child-turn-{index}"),
        state_name,
    )
snapshot = json.loads(state_file.read_text(encoding="utf-8"))
assert snapshot["workers"] == {} and snapshot["pending"] == {}
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
print("PASS: FIFO pending pairing works across distinct parent/child turns and final cleanup passes.")

# Explicit spawn failure removes the pending authority and cannot strand Stop.
state_name = "pending-failure"
session = state_name
run_hook(spawn_payload(session, "call-failure"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
run_hook(spawn_post(session, "call-failure", {"isError": True}), state_name)
assert_denied(run_hook(wait_payload(session), state_name), "no tracked active")
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
print("PASS: explicit spawn failure clears its pending capability.")

# If Start was dropped, a later native Stop reconciles the pending dispatch and
# releases it. This is separate from the normal known-worker completion path.
state_name = "dropped-start"
session = state_name
run_hook(spawn_payload(session, "call-dropped"), state_name)
run_hook(spawn_post(session, "call-dropped", {"result": "accepted"}), state_name)
assert run_hook(lifecycle("Stop", session), state_name).get("decision") == "block"
run_hook(
    lifecycle(
        "SubagentStop",
        session,
        "agent-never-started",
        turn_id="child-turn-without-start",
    ),
    state_name,
)
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
print("PASS: a dropped Start is reconciled by its later completion without stale state.")

# An unmatched duplicate Stop cannot consume a second capability once FIFO
# fallback has already reconciled the one missing Start.
state_name = "unmatched-stop"
session = state_name
run_hook(spawn_payload(session, "call-only"), state_name)
run_hook(spawn_post(session, "call-only", {"accepted": True}), state_name)
run_hook(lifecycle("SubagentStop", session, "missing-one", turn_id="child-a"), state_name)
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
run_hook(lifecycle("SubagentStop", session, "missing-two", turn_id="child-b"), state_name)
assert run_hook(lifecycle("Stop", session, active=True), state_name) == {}
print("PASS: duplicate or unmatched Stops cannot recreate or consume stale pending work.")

# Workers cannot create descendants even through the documented Agent alias.
state_name = "child-spawn"
session = state_name
denied_spawn = run_hook(spawn_payload(session, "call-child", child=True), state_name)
assert_denied(denied_spawn, "Child spawn_agent")
print("PASS: child spawn dispatches are denied before native execution.")

# Authorization consumption is atomic across concurrent callers.
state_name = "concurrent"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(
        pool.map(
            lambda _: run_hook(wait_payload(session), state_name),
            range(4),
        )
    )
permissions = [output(item)["permissionDecision"] for item in results]
assert permissions.count("allow") == 1, permissions
assert permissions.count("deny") == 3, permissions
print("PASS: concurrent callers consume exactly one wait authorization.")

# A child cannot consume the root authorization.
state_name = "child"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
assert_denied(
    run_hook(wait_payload(session, child=True), state_name),
    "non-recursive worker",
)
assert_allowed(run_hook(wait_payload(session), state_name))
print("PASS: child waits are denied without consuming the root capability.")

# Duplicate lifecycle notifications do not manufacture authorization epochs.
state_name = "duplicate-events"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
assert_denied(run_hook(wait_payload(session), state_name), "already consumed")
run_hook(lifecycle("SubagentStop", session, "unknown-agent"), state_name)
assert_denied(run_hook(wait_payload(session), state_name), "already consumed")
print("PASS: duplicate and unknown lifecycle events cannot mint waits.")

# Completion, steering, and a new dispatch each create exactly one new event
# epoch. The model must still issue the next wait call.
state_name = "rearm"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
run_hook(lifecycle("SubagentStart", session, "agent-b"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
run_hook(lifecycle("SubagentStop", session, "agent-a"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
assert_denied(run_hook(wait_payload(session), state_name), "already consumed")
steering = run_hook(steer(session), state_name)
assert "newly authorized wait_agent" in steering["hookSpecificOutput"]["additionalContext"]
assert_allowed(run_hook(wait_payload(session), state_name))
run_hook(lifecycle("SubagentStart", session, "agent-c"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
print("PASS: completion, steering, and dispatch re-arm one wait each.")

# Final attempts with active workers create one immediate Stop continuation and
# a fresh one-shot wait authorization even if the previous subscription timed
# out without a represented lifecycle event.
state_name = "stop-rearm"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
assert_allowed(run_hook(wait_payload(session), state_name))
stop = run_hook(lifecycle("Stop", session), state_name)
assert stop.get("decision") == "block"
assert_allowed(run_hook(wait_payload(session), state_name))
assert_denied(run_hook(wait_payload(session), state_name), "already consumed")
print("PASS: the single Stop continuation can arm one fresh maximum wait.")

# A failed/corrupt Stop grants one separate root recovery call. It is rewritten
# before lifecycle state is loaded and cannot be consumed twice.
state_name = "recovery"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
only_state = state_files(state_name)[0]
only_state.write_text("{", encoding="utf-8")
failure = run_hook(lifecycle("Stop", session), state_name)
assert failure.get("decision") == "block", failure
assert "Exactly one recovery wait_agent" in failure["reason"]
assert_allowed(run_hook(wait_payload(session), state_name))
assert_denied(run_hook(wait_payload(session), state_name), "failed closed")
print("PASS: corrupt state grants exactly one rewritten recovery wait.")

# A transient lifecycle validation failure with otherwise healthy state cannot
# stack a recovery wait on top of the normal authorization for the same epoch.
state_name = "transient-recovery"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
invalid_start = lifecycle("SessionStart", session)
invalid_start["source"] = "invalid"
failure = run_hook(invalid_start, state_name)
assert "Exactly one guarded recovery wait" in failure["systemMessage"]
assert_allowed(run_hook(wait_payload(session), state_name))
assert_denied(run_hook(wait_payload(session), state_name), "already consumed")
print("PASS: recovery consumption also closes a healthy normal wait epoch.")

# Malformed and symlinked objects fail closed without touching their target.
assert_denied(
    run_hook(
        wait_payload("bad-tool-input", tool_input="not-an-object"),
        "bad-tool-input",
    ),
    "failed closed",
)
missing = wait_payload("missing")
missing.pop("session_id")
assert_denied(run_hook(missing, "missing"), "failed closed")

state_name = "malformed-lifecycle"
session = state_name
malformed_start = lifecycle("SubagentStart", session)
lifecycle_failure = run_hook(malformed_start, state_name)
assert "Exactly one guarded recovery wait" in lifecycle_failure["systemMessage"]
unresolved_stop = run_hook(lifecycle("Stop", session), state_name)
assert unresolved_stop.get("decision") == "block"
assert "unresolved lifecycle failure" in unresolved_stop["reason"]
assert_allowed(run_hook(wait_payload(session), state_name))
assert_denied(run_hook(wait_payload(session), state_name))
assert run_hook(lifecycle("Stop", session), state_name) == {}

state_name = "symlink-state"
session = state_name
run_hook(lifecycle("SubagentStart", session, "agent-a"), state_name)
state_path = state_files(state_name)[0]
external = tmp_root / "external.json"
external.write_text('{"sentinel":true}', encoding="utf-8")
state_path.unlink()
state_path.symlink_to(external)
failure = run_hook(lifecycle("Stop", session), state_name)
assert failure.get("decision") == "block"
assert external.read_text(encoding="utf-8") == '{"sentinel":true}'

for raw in ("{", "[]", '"scalar"'):
    result = invoke_raw(raw, "raw-malformed")
    assert result.returncode == 2
    assert not result.stdout
    assert "rejected" in result.stderr
print("PASS: malformed and symlinked state fails closed.")

# A symlinked PLUGIN_DATA root is rejected without chmod or writes through it.
target = tmp_root / "plugin-data-target"
target.mkdir()
sentinel = target / "sentinel"
sentinel.write_text("unchanged", encoding="utf-8")
link = tmp_root / "plugin-data-link"
link.symlink_to(target, target_is_directory=True)
value = lifecycle("SessionStart", "symlink-root")
value["source"] = "startup"
result = subprocess.run(
    [sys.executable, str(hook)],
    input=json.dumps(value),
    text=True,
    capture_output=True,
    env={**os.environ, "PLUGIN_DATA": str(link)},
    timeout=2,
    check=False,
)
assert result.returncode == 0
message = json.loads(result.stdout)
assert "tracking failed" in message["systemMessage"]
assert sentinel.read_text(encoding="utf-8") == "unchanged"
print("PASS: symlinked plugin data roots are rejected safely.")

print("PASS: one-shot wait_agent guard suite completed.")
PY
