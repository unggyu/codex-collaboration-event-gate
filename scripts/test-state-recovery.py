#!/usr/bin/env python3
"""Deterministic upgrade/resume and explicit-repair regressions."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "codex-collaboration-lifecycle.py"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def environment(root: Path, name: str, *, thread_id: str = "unrelated-thread") -> dict[str, str]:
    return {
        **os.environ,
        "CODEX_THREAD_ID": thread_id,
        "PLUGIN_DATA": str(root / name),
    }


def invoke(root: Path, name: str, payload: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment(root, name),
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result
    assert not result.stderr, result.stderr
    return json.loads(result.stdout) if result.stdout else {}


def event(kind: str, session: str, **fields: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": kind,
        "session_id": session,
        "turn_id": f"turn-{session}",
    }
    value.update(fields)
    return value


def start(root: Path, name: str, session: str, source: str = "resume") -> dict[str, object]:
    return invoke(root, name, event("SessionStart", session, source=source))


def state_path(root: Path, name: str, session: str) -> Path:
    return root / name / "sessions" / f"{digest(session)}.json"


def lock_path(root: Path, name: str, session: str) -> Path:
    return root / name / "sessions" / f"{digest(session)}.lock"


def recovery_path(root: Path, name: str, session: str) -> Path:
    return root / name / "sessions" / f"{digest(session)}.recovery.json"


def write_private(path: Path, value: object) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def v7_worker(agent: str, *, valid: bool = True) -> dict[str, object]:
    return {
        "agent_type": "worker",
        "started_at": 100.0,
        "target_hashes": [digest(f"/root/{agent}")],
        "turn_id": f"child-turn-{agent}" if valid else "",
        "updated_at": 101.0,
    }


def v7_state(session: str, *, workers: int, valid: bool = True) -> dict[str, object]:
    return {
        "dispatch_sequence": 0,
        "event_epoch": 1011,
        "interrupts": {},
        "last_event": "dispatch-pending",
        "pending": {},
        "session_hash": digest(session),
        "stop_continuation_epoch": 1011,
        "version": 7,
        "wait_call_hash": digest("old-wait"),
        "wait_issued_epoch": 1011,
        "workers": {
            f"legacy-agent-{index}": v7_worker(
                f"legacy-agent-{index}", valid=valid or index != 0
            )
            for index in range(workers)
        },
    }


def consumed_recovery(session: str) -> dict[str, object]:
    return {
        "authorized": True,
        "consumed": True,
        "reason": "old failure episode",
        "session_hash": digest(session),
        "version": 2,
    }


def prepare_legacy(
    root: Path,
    name: str,
    session: str,
    *,
    workers: int,
    valid: bool = True,
    consumed: bool = True,
) -> bytes:
    start(root, name, session, "startup")
    raw = write_private(
        state_path(root, name, session),
        v7_state(session, workers=workers, valid=valid),
    )
    if consumed:
        write_private(recovery_path(root, name, session), consumed_recovery(session))
    return raw


def permission(value: dict[str, object]) -> str:
    return value["hookSpecificOutput"]["permissionDecision"]  # type: ignore[index]


def wait(root: Path, name: str, session: str, call: str) -> dict[str, object]:
    return invoke(
        root,
        name,
        event(
            "PreToolUse",
            session,
            tool_input={},
            tool_name="collaborationwait_agent",
            tool_use_id=call,
        ),
    )


def spawn(root: Path, name: str, session: str, call: str) -> dict[str, object]:
    message = "\n".join(
        (
            "CLASSIFICATION: non-UI",
            "PARALLELISM_CLASS: read-only",
            f"LEDGER_LANE: {call}",
            "LEDGER_SLOT_CAPACITY: 1",
            "LEDGER_CURRENT_ACTIVE: 1",
        )
    )
    return invoke(
        root,
        name,
        event(
            "PreToolUse",
            session,
            tool_input={
                "task_name": call,
                "message": message,
                "model": "gpt-5.6-terra",
                "fork_turns": "none",
            },
            tool_name="Agent",
            tool_use_id=call,
        ),
    )


def repair_command(session: str) -> str:
    return f"/collaboration-recover-empty {digest(session)} confirm-native-root-only"


with tempfile.TemporaryDirectory(prefix="codex-event-gate-recovery.") as temporary:
    root = Path(temporary)

    # Exact incident: v7 active workers plus an already-consumed v2 recovery
    # token resume under the authoritative payload session_id.
    name = "migrate-v7"
    session = "authoritative-migrated-session"
    prepare_legacy(root, name, session, workers=6)
    resumed = start(root, name, session)
    context = resumed["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert "legacy v7 lifecycle state was migrated" in context
    migrated = json.loads(state_path(root, name, session).read_text())
    assert migrated["version"] == 12
    assert migrated["event_epoch"] == 1012
    assert set(migrated["workers"]) == {
        f"legacy-agent-{index}" for index in range(6)
    }
    assert all(
        not worker["dispatch_metadata"]["observed"]
        and worker["dispatch_key"] == ""
        for worker in migrated["workers"].values()
    )
    assert migrated["workers"]["legacy-agent-0"]["turn_id"] == digest(
        "child-turn-legacy-agent-0"
    )
    assert migrated["ledger"] is None and migrated["ledger_verified"] is False
    assert migrated["legacy_recovery"]["kind"] == "migrated"
    assert not recovery_path(root, name, session).exists()
    assert permission(wait(root, name, session, "migration-wait")) == "allow"
    assert permission(wait(root, name, session, "migration-wait-repeat")) == "deny"
    assert permission(spawn(root, name, session, "conflicting-spawn")) == "deny"
    before_stop = json.loads(state_path(root, name, session).read_text())["event_epoch"]
    first_stop = invoke(root, name, event("Stop", session))
    second_stop = invoke(root, name, event("Stop", session, stop_hook_active=True))
    assert first_stop.get("decision") == "block"
    assert second_stop.get("continue") is False
    assert json.loads(state_path(root, name, session).read_text())["event_epoch"] == before_stop
    invoke(
        root,
        name,
        event(
            "SubagentStop",
            session,
            agent_id="legacy-agent-0",
            agent_type="worker",
        ),
    )
    assert permission(wait(root, name, session, "after-real-completion")) == "allow"
    print("PASS: v7 active state and a consumed recovery token migrate without invented ledger data or an infinite Stop loop.")

    name = "migrate-empty-v7"
    session = "authoritative-empty-legacy-session"
    prepare_legacy(root, name, session, workers=0)
    empty_started = start(root, name, session)
    assert "event gate is active" in empty_started["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    empty_current = json.loads(state_path(root, name, session).read_text())
    assert empty_current["version"] == 12
    assert empty_current["legacy_recovery"] is None
    assert empty_current["workers"] == {} and empty_current["pending"] == {}
    assert spawn(root, name, session, "post-empty-migration") == {}
    print("PASS: a strict empty v7 state migrates directly to usable current state without an operator reset.")

    # An unsafe legacy worker is quarantined, never silently dropped. Duplicate
    # SessionStart/steering/Stop preserve one barrier and one wait epoch.
    name = "quarantine-v7"
    session = "authoritative-quarantine-session"
    legacy_raw = prepare_legacy(root, name, session, workers=1, valid=False)
    quarantined = start(root, name, session)
    context = quarantined["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert "could not be migrated safely" in context
    current = json.loads(state_path(root, name, session).read_text())
    assert current["version"] == 12
    assert current["legacy_recovery"]["kind"] == "quarantined-legacy"
    quarantine_files = list((root / name / "quarantine").glob("*.json"))
    assert len(quarantine_files) == 1
    assert quarantine_files[0].read_bytes() == legacy_raw
    assert stat.S_IMODE(quarantine_files[0].stat().st_mode) == 0o600
    duplicate = start(root, name, session)
    assert "could not be migrated safely" in duplicate["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert len(list((root / name / "quarantine").glob("*.json"))) == 1
    assert permission(wait(root, name, session, "quarantine-wait")) == "allow"
    generic_prompt = invoke(
        root,
        name,
        event("UserPromptSubmit", session, prompt="continue diagnosis"),
    )
    assert repair_command(session) in generic_prompt["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert permission(wait(root, name, session, "quarantine-repeat")) == "deny"
    epoch = json.loads(state_path(root, name, session).read_text())["event_epoch"]
    assert invoke(root, name, event("Stop", session)).get("decision") == "block"
    assert invoke(root, name, event("Stop", session)).get("decision") == "block"
    assert json.loads(state_path(root, name, session).read_text())["event_epoch"] == epoch
    wrong = invoke(
        root,
        name,
        event(
            "UserPromptSubmit",
            session,
            prompt=f"/collaboration-recover-empty {digest('other')} confirm-native-root-only",
        ),
    )
    assert "rejected" in wrong["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert json.loads(state_path(root, name, session).read_text())["legacy_recovery"]
    repaired = invoke(
        root,
        name,
        event("UserPromptSubmit", session, prompt=repair_command(session)),
    )
    assert "valid empty current state" in repaired["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    duplicate_repair = invoke(
        root,
        name,
        event("UserPromptSubmit", session, prompt=repair_command(session)),
    )
    assert "already applied" in duplicate_repair["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert spawn(root, name, session, "bounded-worker") == {}
    invoke(
        root,
        name,
        event(
            "PostToolUse",
            session,
            tool_input={"task_name": "bounded-worker"},
            tool_name="Agent",
            tool_response={"task_name": "/root/bounded-worker"},
            tool_use_id="bounded-worker",
        ),
    )
    invoke(
        root,
        name,
        event(
            "SubagentStart",
            session,
            agent_id="native-worker",
            agent_type="worker",
        ),
    )
    allowed = wait(root, name, session, "normal-ledger-wait")
    assert permission(allowed) == "allow"
    assert allowed["hookSpecificOutput"]["updatedInput"]["timeout_ms"] == 3_600_000  # type: ignore[index]
    after_repair = json.loads(state_path(root, name, session).read_text())
    assert after_repair["ledger_verified"] is True
    assert after_repair["ledger"]["active_count"] == 1
    invoke(
        root,
        name,
        event(
            "SubagentStop",
            session,
            agent_id="native-worker",
            agent_type="worker",
        ),
    )
    assert invoke(root, name, event("Stop", session, stop_hook_active=True)) == {}
    print("PASS: unsafe legacy state is quarantined idempotently and explicit empty-native repair restores bounded spawn→ledger→wait→final.")

    # CODEX_THREAD_ID is deliberately contradictory. Every hook type resolves
    # the payload session_id, and a resumed lifecycle identity gets a distinct
    # lock/state without touching the older identity's v7 bytes.
    name = "identity"
    old_session = "old-hook-session"
    new_session = "new-hook-session"
    old_raw = prepare_legacy(root, name, old_session, workers=1)
    started = start(root, name, new_session)
    assert "event gate is active" in started["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert lock_path(root, name, new_session).exists()
    assert state_path(root, name, old_session).read_bytes() == old_raw
    assert not lock_path(root, name, "unrelated-thread").exists()
    assert spawn(root, name, new_session, "identity-worker") == {}
    assert state_path(root, name, new_session).exists()
    invoke(root, name, event("UserPromptSubmit", new_session, prompt="steer"))
    assert invoke(root, name, event("Stop", new_session)).get("decision") == "block"
    assert state_path(root, name, old_session).read_bytes() == old_raw
    print("PASS: authoritative hook payload session_id, not CODEX_THREAD_ID, scopes start/tool/steering/Stop and resumed identities.")

    # Malformed current-version state is not migrated as legacy or treated as
    # empty. It is quarantined behind the same fail-closed explicit barrier so
    # a consumed generic recovery token cannot strand the session forever.
    name = "malformed-current"
    session = "malformed-current-session"
    invoke(
        root,
        name,
        event(
            "SubagentStart",
            session,
            agent_id="worker",
            agent_type="worker",
        ),
    )
    malformed = json.loads(state_path(root, name, session).read_text())
    malformed["unexpected"] = True
    write_private(state_path(root, name, session), malformed)
    failed = start(root, name, session)
    assert "malformed current-version" in failed["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    barrier = json.loads(state_path(root, name, session).read_text())
    assert barrier["legacy_recovery"]["kind"] == "quarantined-current"
    quarantined_current = list((root / name / "quarantine").glob("*.json"))
    assert len(quarantined_current) == 1
    assert json.loads(quarantined_current[0].read_text())["unexpected"] is True
    assert permission(spawn(root, name, session, "malformed-spawn")) == "deny"
    recovery = wait(root, name, session, "malformed-recovery")
    assert permission(recovery) == "allow"
    assert permission(wait(root, name, session, "malformed-recovery-repeat")) == "deny"
    stop = invoke(root, name, event("Stop", session, stop_hook_active=True))
    assert stop.get("continue") is False
    operator = invoke(
        root,
        name,
        event("UserPromptSubmit", session, prompt=repair_command(session)),
    )
    assert "valid empty current state" in operator["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert json.loads(state_path(root, name, session).read_text())["legacy_recovery"] is None
    assert json.loads(quarantined_current[0].read_text())["unexpected"] is True
    print("PASS: malformed current state stays fail-closed until explicit native-empty repair, with original bytes quarantined.")

    # Unsafe filesystem objects never become migration candidates.
    name = "unsafe-files"
    for suffix, install in (
        (
            "symlink",
            lambda path, external: path.symlink_to(external),
        ),
        (
            "nonregular",
            lambda path, external: path.mkdir(),
        ),
    ):
        session = f"{suffix}-session"
        start(root, name + suffix, session, "startup")
        path = state_path(root, name + suffix, session)
        external = root / f"{suffix}-external"
        write_private(external, {"sentinel": suffix})
        install(path, external)
        failed = start(root, name + suffix, session)
        assert "tracking failed" in failed["systemMessage"]
        assert json.loads(external.read_text())["sentinel"] == suffix

    session = "wrong-mode-session"
    mode_name = "unsafe-mode"
    start(root, mode_name, session, "startup")
    write_private(state_path(root, mode_name, session), v7_state(session, workers=1))
    state_path(root, mode_name, session).chmod(0o644)
    failed = start(root, mode_name, session)
    assert "tracking failed" in failed["systemMessage"]
    assert stat.S_IMODE(state_path(root, mode_name, session).stat().st_mode) == 0o644
    print("PASS: symlink, non-regular, and wrong-mode state fail closed without quarantine or target mutation.")

    # Low-level ownership, lock, interrupted-write, and quarantine-collision
    # behavior is deterministic without changing live plugin data.
    spec = importlib.util.spec_from_file_location("event_gate_hook", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    owned = root / "owned.json"
    write_private(owned, {"owned": True})
    with mock.patch.object(module.os, "geteuid", return_value=os.geteuid() + 1):
        try:
            module.regular_file_bytes(owned, "test state")
        except module.StateCorruption as error:
            assert "wrong owner" in str(error)
        else:
            raise AssertionError("wrong ownership was accepted")

    lock_name = "lock-contention"
    lock_session = "lock-contention-session"
    start(root, lock_name, lock_session, "startup")
    descriptor = os.open(lock_path(root, lock_name, lock_session), os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    process = subprocess.Popen(
        [sys.executable, str(HOOK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment(root, lock_name),
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(event("SessionStart", lock_session, source="resume")))
    process.stdin.close()
    ready, _, _ = select.select([process.stdout], [], [], 0.2)
    assert not ready, "hook bypassed the held session lock"
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
    stdout = process.stdout.read()
    stderr = process.stderr.read() if process.stderr else ""
    assert process.wait(timeout=2) == 0
    assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert not stderr

    atomic = root / "atomic.json"
    original = write_private(atomic, {"sentinel": True})
    with mock.patch.object(module.os, "replace", side_effect=OSError("interrupted")):
        try:
            module.write_state(atomic, {"sentinel": False})
        except OSError:
            pass
        else:
            raise AssertionError("interrupted replace unexpectedly succeeded")
    assert atomic.read_bytes() == original
    assert not list(atomic.parent.glob(f".{atomic.name}.*.tmp"))

    crash_sessions = root / "crash-scope" / "sessions"
    crash_sessions.mkdir(parents=True, mode=0o700)
    crash_state = crash_sessions / "crash-window.json"
    crash_raw = write_private(crash_state, {"version": 7, "sentinel": True})
    module.quarantine_state_bytes(crash_state, crash_raw, 7)
    assert crash_state.read_bytes() == crash_raw
    assert len(list((root / "crash-scope" / "quarantine").glob("*.json"))) == 1

    collision_name = "quarantine-collision"
    collision_session = "quarantine-collision-session"
    collision_raw = prepare_legacy(
        root,
        collision_name,
        collision_session,
        workers=1,
        valid=False,
        consumed=False,
    )
    quarantine = root / collision_name / "quarantine"
    quarantine.mkdir(mode=0o700)
    collision = quarantine / (
        f"{digest(collision_session)}.v7.{hashlib.sha256(collision_raw).hexdigest()}.json"
    )
    collision_bytes = write_private(collision, {"different": True})
    failed = start(root, collision_name, collision_session)
    assert "tracking failed" in failed["systemMessage"]
    assert state_path(root, collision_name, collision_session).read_bytes() == collision_raw
    assert collision.read_bytes() == collision_bytes
    print("PASS: wrong ownership, lock contention, interrupted atomic replacement, and quarantine collision preserve fail-closed state.")

print("PASS: upgrade/resume state recovery suite completed.")
