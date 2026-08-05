#!/usr/bin/env python3
"""Interactive Codex collaboration event gate.

Native subagent lifecycle events atomically maintain opaque session state in
PLUGIN_DATA. One root wait_agent call is authorized per dispatch, completion,
or steering event and is rewritten to the maximum supported timeout. Stop never
waits: it immediately continues once to arm that interruptible subscription, or
fails closed on a repeated final attempt while workers remain.

No transcript, project root, FIFO, polling loop, agent API, or external service
is used.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Any, Iterator


STATE_VERSION = 6
RECOVERY_VERSION = 2
MAX_RECOVERY_WAIT_TIMEOUT_MS = 3_600_000
SESSION_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
WAIT_AGENT_HOOK_NAMES = frozenset(
    {"wait_agent", "collaborationwait_agent", "multi_agent_v1wait_agent"}
)
MAX_AUDIT_RECORDS = 128
MAX_AUDIT_FILES = 256
AUDIT_RETENTION_SECONDS = 14 * 24 * 60 * 60


class StateCorruption(RuntimeError):
    """The session state cannot safely enforce the completion gate."""


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")


def text_field(payload: dict[str, Any], key: str, limit: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value[:limit]


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise StateCorruption("runtime state path is not a directory")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


def opaque_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def state_paths(payload: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing session identity")
    plugin_data = os.environ.get("PLUGIN_DATA", "")
    if not plugin_data:
        raise StateCorruption("PLUGIN_DATA is unavailable")
    base = Path(plugin_data).expanduser()
    if not base.is_absolute():
        raise StateCorruption("PLUGIN_DATA must be an absolute path")
    sessions = private_directory(private_directory(base) / "sessions")
    session_key = opaque_id(session_id)
    prefix = sessions / session_key
    audit = private_directory(private_directory(base) / "audit")
    return (
        prefix.with_suffix(".json"),
        prefix.with_suffix(".lock"),
        prefix.with_suffix(".recovery.json"),
        audit / f"{session_key}.json",
    )


def new_state(session_id: str) -> dict[str, Any]:
    return {
        "dispatch_sequence": 0,
        "event_epoch": 0,
        "last_event": "session",
        "pending": {},
        "session_hash": opaque_id(session_id),
        "stop_continuation_epoch": -1,
        "version": STATE_VERSION,
        "wait_issued_epoch": -1,
        "workers": {},
    }


def new_recovery_control(session_id: str) -> dict[str, Any]:
    return {
        "authorized": False,
        "consumed": False,
        "reason": "",
        "session_hash": opaque_id(session_id),
        "version": RECOVERY_VERSION,
    }


def load_state(path: Path, session_id: str) -> tuple[dict[str, Any], bool]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return new_state(session_id), False
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise StateCorruption("session state is not a regular file")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        raise StateCorruption("session state is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != STATE_VERSION
        or value.get("session_hash") != opaque_id(session_id)
        or not isinstance(value.get("workers"), dict)
        or not isinstance(value.get("pending"), dict)
        or type(value.get("dispatch_sequence")) is not int
        or value["dispatch_sequence"] < 0
        or type(value.get("event_epoch")) is not int
        or value["event_epoch"] < 0
        or type(value.get("wait_issued_epoch")) is not int
        or value["wait_issued_epoch"] < -1
        or type(value.get("stop_continuation_epoch")) is not int
        or value["stop_continuation_epoch"] < -1
        or not isinstance(value.get("last_event"), str)
    ):
        raise StateCorruption("session state schema is invalid")
    for agent_id, worker in value["workers"].items():
        if not isinstance(agent_id, str) or not isinstance(worker, dict):
            raise StateCorruption("active-worker state is invalid")
        if (
            not isinstance(worker.get("agent_type"), str)
            or not isinstance(worker.get("turn_id"), str)
            or not isinstance(worker.get("started_at"), (int, float))
            or not isinstance(worker.get("updated_at"), (int, float))
        ):
            raise StateCorruption("active-worker record is invalid")
    for call_id, pending in value["pending"].items():
        if not isinstance(call_id, str) or not isinstance(pending, dict):
            raise StateCorruption("pending-dispatch state is invalid")
        if (
            not isinstance(pending.get("created_at"), (int, float))
            or not isinstance(pending.get("updated_at"), (int, float))
            or type(pending.get("sequence")) is not int
            or pending["sequence"] <= 0
            or not isinstance(pending.get("turn_hash"), str)
            or not isinstance(pending.get("outcome"), str)
        ):
            raise StateCorruption("pending-dispatch record is invalid")
    return value, True


def load_recovery_control(
    path: Path, session_id: str
) -> tuple[dict[str, Any], bool]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return new_recovery_control(session_id), False
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise StateCorruption("recovery control is not a regular file")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        raise StateCorruption("recovery control is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != RECOVERY_VERSION
        or value.get("session_hash") != opaque_id(session_id)
        or not isinstance(value.get("authorized"), bool)
        or not isinstance(value.get("consumed"), bool)
        or not isinstance(value.get("reason"), str)
    ):
        raise StateCorruption("recovery control schema is invalid")
    return value, True


def write_state(path: Path, state: Any) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def remove_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_audit(path: Path) -> list[dict[str, Any]]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise StateCorruption("lifecycle audit is not a regular file")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        raise StateCorruption("lifecycle audit is unreadable") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise StateCorruption("lifecycle audit schema is invalid")
    return value[-MAX_AUDIT_RECORDS:]


def audit_record(
    audit_path: Path,
    payload: dict[str, Any],
    event: str,
    outcome: str,
    *,
    epoch: int | None = None,
    agent_id: str = "",
    tool_use_id: str = "",
) -> None:
    """Append bounded opaque lifecycle metadata without prompts or tool data."""
    records = load_audit(audit_path)
    record: dict[str, Any] = {
        "at": int(time.time()),
        "event": event[:48],
        "outcome": outcome[:64],
        "session_hash": opaque_id(text_field(payload, "session_id")),
        "v": 1,
    }
    if epoch is not None:
        record["epoch"] = epoch
    turn_id = text_field(payload, "turn_id")
    if turn_id:
        record["turn_hash"] = opaque_id(turn_id)
    if tool_use_id:
        record["tool_hash"] = opaque_id(tool_use_id)
    if agent_id:
        record["agent_hash"] = opaque_id(agent_id)
    records.append(record)
    write_state(audit_path, records[-MAX_AUDIT_RECORDS:])


def prune_audit_directory(audit_path: Path) -> None:
    """Keep retained audit metadata finite on session lifecycle boundaries."""
    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for candidate in audit_path.parent.glob("*.json"):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            continue
        if now - info.st_mtime > AUDIT_RETENTION_SECONDS:
            remove_path(candidate)
        else:
            candidates.append((info.st_mtime, candidate))
    excess = max(0, len(candidates) - MAX_AUDIT_FILES)
    for _, candidate in sorted(candidates):
        if excess == 0:
            break
        if candidate != audit_path:
            remove_path(candidate)
            excess -= 1


def has_active_work(state: dict[str, Any]) -> bool:
    return bool(state["workers"] or state["pending"])


@contextmanager
def session_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StateCorruption("session lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def advance_event(state: dict[str, Any], event: str) -> None:
    state["event_epoch"] += 1
    state["last_event"] = event[:64]
    state["stop_continuation_epoch"] = -1


def authorize_recovery(payload: dict[str, Any], problem: str) -> bool:
    """Atomically authorize one failure-recovery wait without loading state."""
    try:
        session_id = text_field(payload, "session_id")
        if not session_id:
            return False
        _, lock_path, recovery_path, audit_path = state_paths(payload)
        with session_lock(lock_path):
            control, _ = load_recovery_control(recovery_path, session_id)
            if control["consumed"]:
                return False
            control["authorized"] = True
            control["reason"] = problem[:512]
            write_state(recovery_path, control)
            audit_record(audit_path, payload, "recovery", "authorized")
        return True
    except Exception:
        return False


def recovery_message(problem: str, recovery_authorized: bool) -> str:
    message = (
        f"Codex collaboration event gate {problem}; active-worker state may "
        "remain. Do not send a final response or start a polling loop. "
    )
    if recovery_authorized:
        message += (
            "Exactly one recovery wait_agent call is authorized; PreToolUse "
            "will rewrite it to 3600000ms. "
        )
    else:
        message += (
            "No mechanical recovery wait could be authorized; do not call "
            "wait_agent until the session state is explicitly repaired. "
        )
    return message + (
        "Use one-shot list_agents only for diagnosis, reconcile queued native "
        "FINAL/error results, and never claim a passive post-final wake."
    )


def fail_closed(payload: dict[str, Any], problem: str) -> dict[str, Any]:
    message = recovery_message(problem, authorize_recovery(payload, problem))
    if payload.get("stop_hook_active") is True:
        return {
            "continue": False,
            "stopReason": message,
            "systemMessage": message,
        }
    return {"decision": "block", "reason": message}


def wait_denial(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def wait_allow(tool_input: dict[str, Any], context: str) -> dict[str, Any]:
    updated_input = dict(tool_input)
    updated_input["timeout_ms"] = MAX_RECOVERY_WAIT_TIMEOUT_MS
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
            "additionalContext": context,
        }
    }


def is_wait_agent_hook_name(tool_name: str) -> bool:
    return tool_name.endswith("wait_agent") and tool_name in WAIT_AGENT_HOOK_NAMES


def is_spawn_agent_hook_name(tool_name: str) -> bool:
    """Accept both the documented Agent alias and native collaboration names."""
    return tool_name == "Agent" or tool_name.lower().endswith("spawn_agent")


def pending_key(payload: dict[str, Any]) -> str:
    tool_use_id = text_field(payload, "tool_use_id", 256)
    if not tool_use_id:
        raise StateCorruption("spawn_agent is missing a tool-call identity")
    return opaque_id(tool_use_id)


def oldest_pending(state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Pair child lifecycle events to parent dispatches in native FIFO order.

    Codex v0.146.0 supplies child-session/child-turn identities for
    SubagentStart/Stop, not the parent spawn tool_use_id or parent turn. The
    only safe correlation currently available is one session-local pending
    capability per native start/stop in dispatch order.
    """
    candidates = list(state["pending"].items())
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[1]["sequence"])


def spawn_failure(response: Any) -> bool:
    """Only clear a capability on an explicit native tool failure."""
    if not isinstance(response, dict):
        return False
    if response.get("isError") is True or response.get("success") is False:
        return True
    status = response.get("status")
    if isinstance(status, str) and status.lower() in {
        "error",
        "failed",
        "denied",
        "cancelled",
    }:
        return True
    if isinstance(response.get("type"), str) and response["type"].lower() == "error":
        return True
    return bool(response.get("error"))


def record_spawn_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_spawn_agent_hook_name(tool_name):
        return {}
    if text_field(payload, "agent_id") or text_field(payload, "agent_type"):
        return wait_denial(
            "Child spawn_agent calls are blocked by the non-recursive worker "
            "contract. Do not retry; complete the assigned work and report "
            "to the parent."
        )
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing spawn_agent session identity")
    state_path, lock_path, _, audit_path = state_paths(payload)
    call_key = pending_key(payload)
    with session_lock(lock_path):
        state, _ = load_state(state_path, session_id)
        if call_key not in state["pending"]:
            now = time.time()
            state["dispatch_sequence"] += 1
            state["pending"][call_key] = {
                "created_at": now,
                "outcome": "prepared",
                "sequence": state["dispatch_sequence"],
                "turn_hash": opaque_id(text_field(payload, "turn_id")),
                "updated_at": now,
            }
            advance_event(state, "dispatch-pending")
            write_state(state_path, state)
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "spawn-recorded",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
        else:
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "spawn-duplicate",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
    return {}


def reconcile_spawn_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_spawn_agent_hook_name(tool_name):
        return {}
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing PostToolUse session identity")
    state_path, lock_path, _, audit_path = state_paths(payload)
    call_key = pending_key(payload)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        if not existed or call_key not in state["pending"]:
            audit_record(
                audit_path,
                payload,
                "PostToolUse",
                "spawn-untracked",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
            return {}
        if spawn_failure(payload.get("tool_response")):
            state["pending"].pop(call_key, None)
            advance_event(state, "dispatch-failed")
            write_state(state_path, state)
            audit_record(
                audit_path,
                payload,
                "PostToolUse",
                "spawn-failed-cleared",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
            return {}
        state["pending"][call_key]["outcome"] = "accepted"
        state["pending"][call_key]["updated_at"] = time.time()
        write_state(state_path, state)
        audit_record(
            audit_path,
            payload,
            "PostToolUse",
            "spawn-accepted",
            epoch=state["event_epoch"],
            tool_use_id=text_field(payload, "tool_use_id", 256),
        )
    return {}


def session_start(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = text_field(payload, "session_id")
    source = text_field(payload, "source", 32)
    if not session_id or source not in SESSION_SOURCES:
        raise StateCorruption("invalid SessionStart identity or source")
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    with session_lock(lock_path):
        load_state(state_path, session_id)
        load_recovery_control(recovery_path, session_id)
        prune_audit_directory(audit_path)
        audit_record(audit_path, payload, "SessionStart", "ready")
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "The interactive collaboration event gate is active. After "
                "dispatching bounded native workers, call wait_agent once; it "
                "will be rewritten to 3600000ms and remains interruptible by "
                "steered user input. Never poll or repeat a wait without a new "
                "dispatch, worker completion, or steering event. Stop returns "
                "immediately and denies final while workers remain. Reconcile "
                "native FINAL/error results before final and never promise a "
                "passive post-final wake. Workers must not spawn workers."
            ),
        }
    }


def guard_wait_agent(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_wait_agent_hook_name(tool_name):
        return {}
    if text_field(payload, "agent_id") or text_field(payload, "agent_type"):
        return wait_denial(
            "Child wait_agent calls are blocked by the non-recursive worker "
            "contract. Do not retry; report the assigned result to the parent."
        )
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise StateCorruption("wait_agent tool_input is not an object")
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing PreToolUse session identity")
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    with session_lock(lock_path):
        control, _ = load_recovery_control(recovery_path, session_id)
        if control["authorized"] and not control["consumed"]:
            control["consumed"] = True
            write_state(recovery_path, control)
            # If lifecycle state is still valid, consume its current normal
            # epoch too. This prevents a transient failure from granting a
            # recovery wait followed by a second normal wait for the same
            # event. Corrupt state must not prevent the explicit recovery.
            try:
                state, _ = load_state(state_path, session_id)
                if has_active_work(state):
                    state["wait_issued_epoch"] = state["event_epoch"]
                    write_state(state_path, state)
            except Exception:
                pass
            audit_record(
                audit_path, payload, "PreToolUse", "wait-recovery-allowed"
            )
            return wait_allow(
                tool_input,
                "One failure-recovery wait was consumed and rewritten to "
                "3600000ms. Do not call wait_agent again without a lifecycle "
                "or steering event.",
            )
        state, _ = load_state(state_path, session_id)
        if not has_active_work(state):
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "wait-denied-no-active-work",
                epoch=state["event_epoch"],
            )
            return wait_denial(
                "wait_agent is blocked because no tracked active workers or "
                "pending native dispatches "
                "remain. Do not retry; reconcile results and finish normally."
            )
        if state["wait_issued_epoch"] == state["event_epoch"]:
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "wait-denied-consumed",
                epoch=state["event_epoch"],
            )
            return wait_denial(
                "The one wait_agent authorization for the current lifecycle "
                "event was already consumed. Do not retry or poll. Re-arm only "
                "after a new dispatch, worker completion, or steered user input."
            )
        state["wait_issued_epoch"] = state["event_epoch"]
        write_state(state_path, state)
        audit_record(
            audit_path,
            payload,
            "PreToolUse",
            "wait-allowed",
            epoch=state["event_epoch"],
        )
        return wait_allow(
            tool_input,
            "The event subscription was rewritten to 3600000ms. It consumes "
            "no model polling turns and remains interruptible by steered user "
            "input. Reconcile the returned completion or steering event before "
            "arming another authorized wait.",
        )


def update_subagent(payload: dict[str, Any]) -> dict[str, Any]:
    event = text_field(payload, "hook_event_name")
    session_id = text_field(payload, "session_id")
    agent_id = text_field(payload, "agent_id")
    if event not in {"SubagentStart", "SubagentStop"}:
        return {}
    if not session_id or not agent_id:
        raise StateCorruption(f"{event} is missing session or agent identity")
    state_path, lock_path, _, audit_path = state_paths(payload)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        if event == "SubagentStart":
            now = time.time()
            is_new = agent_id not in state["workers"]
            state["workers"][agent_id] = {
                "agent_type": text_field(payload, "agent_type"),
                "started_at": (
                    now
                    if is_new
                    else state["workers"][agent_id]["started_at"]
                ),
                "turn_id": text_field(payload, "turn_id"),
                "updated_at": now,
            }
            if is_new:
                matched = oldest_pending(state)
                if matched is not None:
                    state["pending"].pop(matched[0], None)
                    outcome = "start-promoted-pending"
                else:
                    advance_event(state, "dispatch-unpaired")
                    outcome = "start-unpaired"
                write_state(state_path, state)
                audit_record(
                    audit_path,
                    payload,
                    "SubagentStart",
                    outcome,
                    epoch=state["event_epoch"],
                    agent_id=agent_id,
                )
            else:
                audit_record(
                    audit_path,
                    payload,
                    "SubagentStart",
                    "start-duplicate",
                    epoch=state["event_epoch"],
                    agent_id=agent_id,
                )
        elif event == "SubagentStop" and existed:
            if state["workers"].pop(agent_id, None) is not None:
                advance_event(state, "completion")
                write_state(state_path, state)
                audit_record(
                    audit_path,
                    payload,
                    "SubagentStop",
                    "stop-worker",
                    epoch=state["event_epoch"],
                    agent_id=agent_id,
                )
            else:
                # A dropped Start must not strand a pending dispatch forever.
                matched = oldest_pending(state)
                if matched is not None:
                    state["pending"].pop(matched[0], None)
                    advance_event(state, "completion-unpaired")
                    write_state(state_path, state)
                    outcome = "stop-cleared-unpaired-pending"
                else:
                    outcome = "stop-unknown"
                audit_record(
                    audit_path,
                    payload,
                    "SubagentStop",
                    outcome,
                    epoch=state["event_epoch"],
                    agent_id=agent_id,
                )
    return {}


def user_prompt_submit(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing UserPromptSubmit session identity")
    state_path, lock_path, _, audit_path = state_paths(payload)
    active = False
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        if existed and has_active_work(state):
            advance_event(state, "steering")
            write_state(state_path, state)
            audit_record(
                audit_path,
                payload,
                "UserPromptSubmit",
                "steering-active",
                epoch=state["event_epoch"],
            )
            active = True
        elif existed:
            audit_record(
                audit_path,
                payload,
                "UserPromptSubmit",
                "steering-idle",
                epoch=state["event_epoch"],
            )
    if not active:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "Steered input interrupted the prior event subscription while "
                "workers remain. Address this input, reconcile available worker "
                "results, then arm exactly one newly authorized wait_agent."
            ),
        }
    }


def stop_gate(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing Stop session identity")
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        control, recovery_existed = load_recovery_control(
            recovery_path, session_id
        )
        if not has_active_work(state):
            if (
                recovery_existed
                and control["authorized"]
                and not control["consumed"]
            ):
                message = recovery_message(
                    "has an unresolved lifecycle failure", True
                )
                if payload.get("stop_hook_active") is True:
                    return {
                        "continue": False,
                        "stopReason": message,
                        "systemMessage": message,
                    }
                return {"decision": "block", "reason": message}
            if existed:
                remove_path(state_path)
            remove_path(recovery_path)
            audit_record(audit_path, payload, "Stop", "released")
            return {}
        if (
            payload.get("stop_hook_active") is True
            or state["stop_continuation_epoch"] == state["event_epoch"]
        ):
            message = (
                "Active native Codex workers or pending dispatches still remain "
                "after the single Stop "
                "continuation. Final is denied without another continuation to "
                "avoid a loop. Reconcile the current event and arm the one "
                "authorized 3600000ms wait_agent instead."
            )
            audit_record(
                audit_path,
                payload,
                "Stop",
                "denied-repeat",
                epoch=state["event_epoch"],
            )
            return {
                "continue": False,
                "stopReason": message,
                "systemMessage": message,
            }
        advance_event(state, "stop-continuation")
        state["stop_continuation_epoch"] = state["event_epoch"]
        write_state(state_path, state)
        audit_record(
            audit_path,
            payload,
            "Stop",
            "denied-active",
            epoch=state["event_epoch"],
        )
        return {
            "decision": "block",
            "reason": (
                "Active native Codex workers or pending dispatches remain, so "
                "final is denied. Stop "
                "returned immediately to keep the composer responsive. Arm "
                "exactly one wait_agent now; PreToolUse will rewrite it to "
                "3600000ms. The native subscription uses no model polling turns "
                "and steered user input can interrupt it. Reconcile every "
                "returned FINAL/error or steering event before another wait, "
                "and never promise a passive post-final wake."
            ),
        }


def session_end(payload: dict[str, Any]) -> dict[str, Any]:
    """Release only owned transient state after a parent session ends."""
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing SessionEnd session identity")
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        audit_record(
            audit_path,
            payload,
            "SessionEnd",
            "cleanup-active" if existed and has_active_work(state) else "cleanup-idle",
            epoch=state["event_epoch"],
        )
        remove_path(state_path)
        remove_path(recovery_path)
        prune_audit_directory(audit_path)
    return {}


def update_for_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = text_field(payload, "hook_event_name")
    if event == "SessionStart":
        return session_start(payload)
    if event == "PreToolUse":
        if is_spawn_agent_hook_name(text_field(payload, "tool_name", 128)):
            return record_spawn_dispatch(payload)
        return guard_wait_agent(payload)
    if event == "PostToolUse":
        return reconcile_spawn_dispatch(payload)
    if event in {"SubagentStart", "SubagentStop"}:
        return update_subagent(payload)
    if event == "UserPromptSubmit":
        return user_prompt_submit(payload)
    if event == "Stop":
        return stop_gate(payload)
    if event == "SessionEnd":
        return session_end(payload)
    return {}


def main() -> int:
    payload: dict[str, Any] = {}
    try:
        loaded = json.load(sys.stdin)
        if not isinstance(loaded, dict):
            sys.stderr.write(
                "Codex collaboration hook rejected a non-object payload; "
                "blocking the current event where supported.\n"
            )
            return 2
        payload = loaded
        emit(update_for_event(payload))
    except Exception:
        event = text_field(payload, "hook_event_name")
        if event == "PreToolUse":
            emit(
                wait_denial(
                    "wait_agent guard failed closed because session state could "
                    "not be verified. Do not retry; attempt final so Stop can "
                    "authorize exactly one explicit recovery path."
                )
            )
        elif event == "Stop":
            emit(fail_closed(payload, "detected corrupt or unavailable state"))
        elif event in {
            "SessionStart",
            "PostToolUse",
            "SubagentStart",
            "SubagentStop",
            "UserPromptSubmit",
        }:
            recovery_authorized = authorize_recovery(
                payload, f"{event} lifecycle tracking failed"
            )
            message = (
                f"Codex {event} lifecycle tracking failed; final remains "
                "fail-closed while worker state is uncertain. "
                + (
                    "Exactly one guarded recovery wait is authorized."
                    if recovery_authorized
                    else "No guarded recovery wait could be authorized."
                )
            )
            value: dict[str, Any] = {"systemMessage": message}
            if event == "SessionStart":
                value["hookSpecificOutput"] = {
                    "hookEventName": "SessionStart",
                    "additionalContext": message,
                }
            elif event == "UserPromptSubmit":
                value["hookSpecificOutput"] = {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": message,
                }
            emit(value)
        elif event == "SessionEnd":
            # SessionEnd is advisory; never strand a closed session on cleanup
            # telemetry failure.
            return 0
        else:
            sys.stderr.write(
                "Codex collaboration hook rejected malformed input; blocking "
                "the current event where supported.\n"
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
