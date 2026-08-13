#!/usr/bin/env python3
"""Interactive Codex collaboration event gate.

Native subagent lifecycle events atomically maintain opaque session state in
PLUGIN_DATA. One root wait_agent call is authorized per dispatch, completion,
non-timeout wait return, or steering event and is rewritten to the maximum
supported timeout. Successful interrupt_agent calls reconcile only their exact
tracked target. Stop never waits: it immediately continues once to arm that
interruptible subscription, or fails closed on a repeated final attempt while
workers remain.

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
import re


STATE_VERSION = 12
LEGACY_STATE_VERSIONS = frozenset(range(1, STATE_VERSION))
RECOVERY_VERSION = 2
MAX_RECOVERY_WAIT_TIMEOUT_MS = 3_600_000
SESSION_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
WAIT_AGENT_HOOK_NAMES = frozenset(
    {"wait_agent", "collaborationwait_agent", "multi_agent_v1wait_agent"}
)
INTERRUPT_AGENT_HOOK_NAMES = frozenset(
    {
        "interrupt_agent",
        "collaborationinterrupt_agent",
        "multi_agent_v1interrupt_agent",
    }
)
INTERRUPT_SUCCESS_PREVIOUS_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "completed",
        "errored",
        "failed",
        "interrupted",
        "pending",
        "running",
        "shutdown",
        "stopped",
        "waiting",
    }
)
INTERRUPT_TERMINAL_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "completed",
        "errored",
        "failed",
        "interrupted",
        "shutdown",
        "stopped",
    }
)
MAX_INTERRUPT_CAPABILITIES = 64
MAX_TARGET_HASHES = 4
MAX_LEDGER_LANES = 64
MAX_LEDGER_GATES = 8
MAX_LEDGER_DEPENDENCIES = 16
MAX_SLOT_CAPACITY = 64
MAX_RUNTIME_FILE_BYTES = 2 * 1024 * 1024
MAX_AUDIT_RECORDS = 128
MAX_AUDIT_FILES = 256
AUDIT_RETENTION_SECONDS = 14 * 24 * 60 * 60
LANE_STATUSES = frozenset({"active", "eligible", "deferred", "blocked", "done"})
PARALLELISM_CLASSES = frozenset({"read-only", "isolated-write", "exclusive-gate"})
MODEL_CLASSES = frozenset({"terra", "sol"})
FORK_MODES = frozenset({"none", "all"})
DISPATCH_MAP_FIELDS = frozenset(
    {"task_name", "tool_use_id", "tool_use_hash", "agent_id", "target_hash"}
)
GATE_VALUE = r"[A-Za-z0-9][A-Za-z0-9._/@-]{0,159}"
GATE_PATTERNS = (
    re.compile(r"^git-ref:origin/[A-Za-z0-9][A-Za-z0-9._/-]{0,159}$"),
    re.compile(r"^github-pr:" + GATE_VALUE + r":" + GATE_VALUE + r":" + GATE_VALUE + r"$"),
    re.compile(r"^qa-deploy:" + GATE_VALUE + r"$"),
    re.compile(r"^production-deploy:" + GATE_VALUE + r"$"),
    re.compile(r"^plugin-source:" + GATE_VALUE + r"$"),
    re.compile(r"^local-worktree-cleanup:" + GATE_VALUE + r"$"),
    re.compile(r"^shared-resource:" + GATE_VALUE + r"$"),
)
PROMPT_METADATA = re.compile(
    r"(?m)^\s*(CLASSIFICATION|PARALLELISM_CLASS|EXCLUSIVE_GATE|"
    r"SOL_OVERRIDE_REASON|NOVEL_UI_COMPLEXITY|LEDGER_LANE|"
    r"LEDGER_SLOT_CAPACITY|LEDGER_CURRENT_ACTIVE)\s*:\s*([^\r\n]{1,512})\s*$"
)
OPERATOR_RECOVERY_PREFIX = "/collaboration-recover-empty"
OPERATOR_RECOVERY_CONFIRMATION = "confirm-native-root-only"


class StateCorruption(RuntimeError):
    """The session state cannot safely enforce the completion gate."""


class LegacyState(StateCorruption):
    """A structurally separate legacy schema needs SessionStart recovery."""

    def __init__(self, version: int, value: dict[str, Any], raw: bytes) -> None:
        super().__init__(f"legacy session state version {version} is incompatible")
        self.version = version
        self.value = value
        self.raw = raw


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
        if info.st_uid != os.geteuid():
            raise StateCorruption("runtime state directory has the wrong owner")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise StateCorruption("runtime state directory permissions are unsafe")
    finally:
        os.close(descriptor)
    return path


def opaque_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def valid_target_hashes(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_TARGET_HASHES
        and all(valid_digest(item) for item in value)
    )


def valid_hash_list(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and value == sorted(set(value))
        and all(valid_digest(item) for item in value)
    )


def valid_dispatch_metadata(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {
            "classification",
            "fork_mode",
            "gate_hashes",
            "lane_hash",
            "ledger_active_count",
            "ledger_capacity",
            "model",
            "model_class",
            "novel_ui",
            "observed",
            "parallelism_class",
            "sol_override_evidence_hash",
            "sol_override_kind",
        }
        and isinstance(value["observed"], bool)
        and value["classification"] in {"", "ui", "non-ui"}
        and value["fork_mode"] in {"", *FORK_MODES}
        and value["parallelism_class"] in {"", *PARALLELISM_CLASSES}
        and (not value["lane_hash"] or valid_digest(value["lane_hash"]))
        and type(value["ledger_capacity"]) is int
        and 0 <= value["ledger_capacity"] <= MAX_SLOT_CAPACITY
        and type(value["ledger_active_count"]) is int
        and 0 <= value["ledger_active_count"] <= MAX_SLOT_CAPACITY
        and valid_hash_list(value["gate_hashes"], MAX_LEDGER_GATES)
        and isinstance(value["model"], str)
        and len(value["model"]) <= 96
        and value["model_class"] in {"", *MODEL_CLASSES}
        and isinstance(value["novel_ui"], bool)
        and value["sol_override_kind"] in {"", "user-requested", "terra-blocked"}
        and (
            not value["sol_override_evidence_hash"]
            or valid_digest(value["sol_override_evidence_hash"])
        )
        and (
            not value["observed"]
            or (
                valid_digest(value["lane_hash"])
                and 1 <= value["ledger_capacity"] <= MAX_SLOT_CAPACITY
                and 1 <= value["ledger_active_count"] <= value["ledger_capacity"]
            )
        )
        and (
            value["observed"]
            or (
                value["lane_hash"] == ""
                and value["ledger_active_count"] == 0
                and value["ledger_capacity"] == 0
            )
        )
    )


def valid_legacy_recovery(value: Any) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value)
        == {
            "kind",
            "pending_count",
            "quarantine_hash",
            "source_version",
            "worker_count",
        }
        and value["kind"]
        in {"migrated", "quarantined-current", "quarantined-legacy"}
        and type(value["source_version"]) is int
        and -1 <= value["source_version"] <= 1_000_000
        and type(value["worker_count"]) is int
        and -1 <= value["worker_count"] <= MAX_LEDGER_LANES
        and type(value["pending_count"]) is int
        and -1 <= value["pending_count"] <= MAX_LEDGER_LANES
        and (
            value["quarantine_hash"] == ""
            or valid_digest(value["quarantine_hash"])
        )
        and (
            value["kind"].startswith("quarantined-")
            or value["quarantine_hash"] == ""
        )
    )


def valid_ledger(value: Any) -> bool:
    if value is None:
        return True
    if (
        not isinstance(value, dict)
        or set(value) != {"active_count", "capacity", "epoch", "lanes"}
        or type(value["epoch"]) is not int
        or value["epoch"] < 0
        or type(value["capacity"]) is not int
        or not 1 <= value["capacity"] <= MAX_SLOT_CAPACITY
        or type(value["active_count"]) is not int
        or not 0 <= value["active_count"] <= value["capacity"]
        or not isinstance(value["lanes"], dict)
        or len(value["lanes"]) > MAX_LEDGER_LANES
    ):
        return False
    expected = {
        "classification",
        "dependency_hashes",
        "dispatch_map",
        "external_blocker_hash",
        "fork_mode",
        "gate_hashes",
        "model",
        "model_class",
        "novel_ui",
        "parallelism_class",
        "sol_override_evidence_hash",
        "sol_override_kind",
        "status",
    }
    for lane_hash, lane in value["lanes"].items():
        if not valid_digest(lane_hash) or not isinstance(lane, dict) or set(lane) != expected:
            return False
        dispatch_map = lane["dispatch_map"]
        if (
            lane["status"] not in LANE_STATUSES
            or lane["parallelism_class"] not in PARALLELISM_CLASSES
            or lane["classification"] not in {"ui", "non-ui"}
            or lane["model_class"] not in MODEL_CLASSES
            or not isinstance(lane["model"], str)
            or len(lane["model"]) > 96
            or lane["fork_mode"] not in FORK_MODES
            or not isinstance(lane["novel_ui"], bool)
            or lane["sol_override_kind"] not in {"", "user-requested", "terra-blocked"}
            or (
                lane["sol_override_evidence_hash"]
                and not valid_digest(lane["sol_override_evidence_hash"])
            )
            or (
                lane["external_blocker_hash"]
                and not valid_digest(lane["external_blocker_hash"])
            )
            or not valid_hash_list(lane["gate_hashes"], MAX_LEDGER_GATES)
            or not valid_hash_list(
                lane["dependency_hashes"], MAX_LEDGER_DEPENDENCIES
            )
            or not isinstance(dispatch_map, dict)
            or set(dispatch_map) != DISPATCH_MAP_FIELDS
            or any(value and not valid_digest(value) for value in dispatch_map.values())
        ):
            return False
    return True


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
        "interrupts": {},
        "last_event": "session",
        "legacy_recovery": None,
        "ledger": None,
        "ledger_verified": False,
        "pending": {},
        "session_hash": opaque_id(session_id),
        "stop_continuation_epoch": -1,
        "version": STATE_VERSION,
        "wait_call_hash": "",
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


def regular_file_bytes(path: Path, purpose: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise StateCorruption(f"{purpose} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StateCorruption(f"{purpose} is not a regular file")
        if info.st_uid != os.geteuid():
            raise StateCorruption(f"{purpose} has the wrong owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise StateCorruption(f"{purpose} permissions are unsafe")
        if info.st_size > MAX_RUNTIME_FILE_BYTES:
            raise StateCorruption(f"{purpose} exceeds the bounded size")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as error:
        raise StateCorruption(f"{purpose} is unreadable") from error
    finally:
        os.close(descriptor)


def decoded_json_object(raw: bytes, purpose: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateCorruption(f"{purpose} is unreadable") from error
    if not isinstance(value, dict):
        raise StateCorruption(f"{purpose} schema is invalid")
    return value


def load_state(path: Path, session_id: str) -> tuple[dict[str, Any], bool]:
    try:
        raw = regular_file_bytes(path, "session state")
    except FileNotFoundError:
        return new_state(session_id), False
    value = decoded_json_object(raw, "session state")
    version = value.get("version")
    if type(version) is int and version in LEGACY_STATE_VERSIONS:
        raise LegacyState(version, value, raw)
    expected_state_fields = set(new_state(session_id))
    if frozenset(value) not in {
        frozenset(expected_state_fields),
        frozenset(expected_state_fields - {"legacy_recovery"}),
    }:
        raise StateCorruption("session state schema is invalid")
    value.setdefault("legacy_recovery", None)
    if (
        value.get("version") != STATE_VERSION
        or value.get("session_hash") != opaque_id(session_id)
        or not isinstance(value.get("workers"), dict)
        or not isinstance(value.get("pending"), dict)
        or not isinstance(value.get("interrupts"), dict)
        or not valid_legacy_recovery(value.get("legacy_recovery"))
        or not valid_ledger(value.get("ledger"))
        or not isinstance(value.get("ledger_verified"), bool)
        or len(value["interrupts"]) > MAX_INTERRUPT_CAPABILITIES
        or type(value.get("dispatch_sequence")) is not int
        or value["dispatch_sequence"] < 0
        or type(value.get("event_epoch")) is not int
        or value["event_epoch"] < 0
        or type(value.get("wait_issued_epoch")) is not int
        or value["wait_issued_epoch"] < -1
        or type(value.get("stop_continuation_epoch")) is not int
        or value["stop_continuation_epoch"] < -1
        or not isinstance(value.get("wait_call_hash"), str)
        or (value["wait_call_hash"] and not valid_digest(value["wait_call_hash"]))
        or not isinstance(value.get("last_event"), str)
    ):
        raise StateCorruption("session state schema is invalid")
    for agent_id, worker in value["workers"].items():
        if not isinstance(agent_id, str) or not isinstance(worker, dict):
            raise StateCorruption("active-worker state is invalid")
        if (
            set(worker)
            != {
                "agent_type",
                "dispatch_key",
                "dispatch_metadata",
                "started_at",
                "target_hashes",
                "turn_id",
                "updated_at",
            }
            or not isinstance(worker.get("agent_type"), str)
            or not valid_digest(worker.get("turn_id"))
            or not isinstance(worker.get("started_at"), (int, float))
            or not isinstance(worker.get("updated_at"), (int, float))
            or not valid_target_hashes(worker.get("target_hashes"))
            or not isinstance(worker.get("dispatch_key"), str)
            or (worker["dispatch_key"] and not valid_digest(worker["dispatch_key"]))
            or not valid_dispatch_metadata(worker.get("dispatch_metadata"))
        ):
            raise StateCorruption("active-worker record is invalid")
    for call_id, pending in value["pending"].items():
        if not valid_digest(call_id) or not isinstance(pending, dict):
            raise StateCorruption("pending-dispatch state is invalid")
        if (
            set(pending)
            != {
                "created_at",
                "dispatch_metadata",
                "outcome",
                "sequence",
                "target_hashes",
                "turn_hash",
                "updated_at",
            }
            or not isinstance(pending.get("created_at"), (int, float))
            or not isinstance(pending.get("updated_at"), (int, float))
            or type(pending.get("sequence")) is not int
            or pending["sequence"] <= 0
            or not isinstance(pending.get("turn_hash"), str)
            or not isinstance(pending.get("outcome"), str)
            or not valid_target_hashes(pending.get("target_hashes"))
            or not valid_dispatch_metadata(pending.get("dispatch_metadata"))
        ):
            raise StateCorruption("pending-dispatch record is invalid")
    for call_id, interrupt in value["interrupts"].items():
        if not valid_digest(call_id) or not isinstance(interrupt, dict):
            raise StateCorruption("interrupt capability state is invalid")
        if (
            set(interrupt)
            != {"created_at", "target_hash", "work_key", "work_kind"}
            or not isinstance(interrupt.get("created_at"), (int, float))
            or not valid_digest(interrupt.get("target_hash"))
            or interrupt.get("work_kind") not in {"pending", "worker"}
            or not isinstance(interrupt.get("work_key"), str)
        ):
            raise StateCorruption("interrupt capability record is invalid")
    return value, True


def load_recovery_control(
    path: Path, session_id: str
) -> tuple[dict[str, Any], bool]:
    try:
        raw = regular_file_bytes(path, "recovery control")
    except FileNotFoundError:
        return new_recovery_control(session_id), False
    value = decoded_json_object(raw, "recovery control")
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
        directory = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
        raw = regular_file_bytes(path, "lifecycle audit")
    except FileNotFoundError:
        return []
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
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
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
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


def has_recovery_barrier(state: dict[str, Any]) -> bool:
    return state.get("legacy_recovery") is not None


def has_unresolved_work(state: dict[str, Any]) -> bool:
    return has_active_work(state) or has_recovery_barrier(state)


@contextmanager
def session_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StateCorruption("session lock is not a regular file")
        if info.st_uid != os.geteuid():
            raise StateCorruption("session lock has the wrong owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise StateCorruption("session lock permissions are unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def synchronize_declared_active_count(state: dict[str, Any]) -> None:
    """Refresh bound batch counts only after a verified ledger epoch."""
    active_count = len(state["workers"]) + len(state["pending"])
    for pending in state["pending"].values():
        metadata = pending["dispatch_metadata"]
        if metadata["observed"]:
            metadata["ledger_active_count"] = active_count
    for worker in state["workers"].values():
        metadata = worker["dispatch_metadata"]
        if metadata["observed"]:
            metadata["ledger_active_count"] = active_count


def advance_event(state: dict[str, Any], event: str) -> None:
    if state["ledger_verified"]:
        synchronize_declared_active_count(state)
    state["event_epoch"] += 1
    state["ledger"] = None
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


def is_interrupt_agent_hook_name(tool_name: str) -> bool:
    return (
        tool_name.endswith("interrupt_agent")
        and tool_name in INTERRUPT_AGENT_HOOK_NAMES
    )


def is_spawn_agent_hook_name(tool_name: str) -> bool:
    """Accept both the documented Agent alias and native collaboration names."""
    return tool_name == "Agent" or tool_name.lower().endswith("spawn_agent")


def pending_key(payload: dict[str, Any]) -> str:
    tool_use_id = text_field(payload, "tool_use_id", 256)
    if not tool_use_id:
        raise StateCorruption("tracked tool is missing a tool-call identity")
    return opaque_id(tool_use_id)


def response_object(response: Any) -> dict[str, Any] | None:
    """Accept the documented JSON object or its local model-facing encoding."""
    if isinstance(response, dict):
        return response
    if not isinstance(response, str) or len(response) > 4096:
        return None
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def spawn_target_hashes(tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    task_name = tool_input.get("task_name")
    if not isinstance(task_name, str) or not task_name or len(task_name) > 512:
        return []
    targets = {task_name}
    if not task_name.startswith("/"):
        targets.add(f"/root/{task_name}")
    return sorted(opaque_id(target) for target in targets)


def response_target_hash(response: Any) -> str:
    value = response_object(response)
    if value is None:
        return ""
    task_name = value.get("task_name")
    if not isinstance(task_name, str) or not task_name or len(task_name) > 512:
        return ""
    return opaque_id(task_name)


def exact_work_target(
    state: dict[str, Any], target: str
) -> tuple[str, str] | None:
    """Resolve one exact worker/pending target without FIFO fallback."""
    target_hash = opaque_id(target)
    matches: set[tuple[str, str]] = set()
    if target in state["workers"]:
        matches.add(("worker", target))
    for agent_id, worker in state["workers"].items():
        if target_hash in worker["target_hashes"]:
            matches.add(("worker", agent_id))
    for call_key, pending in state["pending"].items():
        if target_hash in pending["target_hashes"]:
            matches.add(("pending", call_key))
    if len(matches) != 1:
        return None
    return next(iter(matches))


def interrupt_response_confirms_terminal(response: Any) -> bool:
    value = response_object(response)
    if value is None:
        return False
    if (
        value.get("isError") is True
        or value.get("success") is False
        or bool(value.get("error"))
        or (
            isinstance(value.get("type"), str)
            and value["type"].lower() == "error"
        )
    ):
        return False
    status = value.get("status")
    if (
        value.get("success") is True
        and isinstance(status, str)
        and status.lower() in INTERRUPT_TERMINAL_STATUSES
    ):
        return True
    previous_status = value.get("previous_status")
    return (
        isinstance(previous_status, str)
        and previous_status.lower() in INTERRUPT_SUCCESS_PREVIOUS_STATUSES
    )


def wait_response_is_event(response: Any) -> bool:
    value = response_object(response)
    return (
        value is not None
        and not spawn_failure(value)
        and value.get("timed_out") is False
    )


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
    value = response_object(response)
    if value is None:
        return False
    if value.get("isError") is True or value.get("success") is False:
        return True
    status = value.get("status")
    if isinstance(status, str) and status.lower() in {
        "error",
        "failed",
        "denied",
        "cancelled",
    }:
        return True
    if isinstance(value.get("type"), str) and value["type"].lower() == "error":
        return True
    return bool(value.get("error"))


def canonical_gate(value: Any) -> str:
    if not isinstance(value, str):
        raise StateCorruption("exclusive gate is not a string")
    gate = value.strip()
    if (
        not gate
        or len(gate) > 512
        or any(pattern.fullmatch(gate) for pattern in GATE_PATTERNS) is False
    ):
        raise StateCorruption("exclusive gate is outside the exact taxonomy")
    return gate


def normalized_gate_hashes(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_LEDGER_GATES:
        raise StateCorruption("exclusive gates must be a bounded list")
    canonical = [canonical_gate(value) for value in values]
    if len(set(canonical)) != len(canonical):
        raise StateCorruption("exclusive gates must not repeat")
    return sorted(opaque_id(value) for value in canonical)


def prompt_metadata(tool_input: Any) -> tuple[dict[str, str], list[str]]:
    if not isinstance(tool_input, dict):
        return {}, []
    message = tool_input.get("message")
    if not isinstance(message, str):
        return {}, []
    values: dict[str, str] = {}
    gates: list[str] = []
    for match in PROMPT_METADATA.finditer(message[:32_768]):
        key = match.group(1)
        value = match.group(2).strip()
        if key == "EXCLUSIVE_GATE":
            gates.append(value)
        elif key not in values:
            values[key] = value
        else:
            raise StateCorruption(f"duplicate {key} spawn metadata")
    return values, gates


def model_class(model: str) -> str:
    if model == "gpt-5.6-terra":
        return "terra"
    if model == "gpt-5.6-sol":
        return "sol"
    return ""


def sol_override(value: str) -> tuple[str, str]:
    if value == "user-requested":
        return "user-requested", ""
    if value.startswith("terra-blocked:"):
        evidence = value.split(":", 1)[1].strip()
        if len(evidence) >= 12 and evidence.lower() not in {
            "unknown",
            "generic",
            "not available",
        }:
            return "terra-blocked", opaque_id(evidence)
    if value:
        raise StateCorruption("SOL_OVERRIDE_REASON is not concrete")
    return "", ""


def ledger_integer(value: str, field: str, minimum: int, maximum: int) -> int:
    if not value.isdecimal():
        raise StateCorruption(f"{field} must be a decimal integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise StateCorruption(f"{field} is outside the bounded contract")
    return result


def spawn_metadata(tool_input: Any) -> dict[str, Any]:
    """Parse only fixed coordination metadata; never persist the prompt."""
    values, prompt_gates = prompt_metadata(tool_input)
    classification = values.get("CLASSIFICATION", "").lower()
    classification = {"ui": "ui", "non-ui": "non-ui"}.get(
        classification, "invalid"
    )
    if classification == "invalid":
        raise StateCorruption("CLASSIFICATION must be UI or non-UI")
    parallelism_class = values.get("PARALLELISM_CLASS", "")
    if parallelism_class not in PARALLELISM_CLASSES:
        raise StateCorruption("PARALLELISM_CLASS is invalid")
    gate_hashes = normalized_gate_hashes(prompt_gates) if prompt_gates else []
    if parallelism_class == "exclusive-gate" and not gate_hashes:
        raise StateCorruption("exclusive-gate spawn metadata lacks EXCLUSIVE_GATE")
    if parallelism_class and parallelism_class != "exclusive-gate" and gate_hashes:
        raise StateCorruption("non-exclusive spawn metadata declares a gate")
    tool = tool_input if isinstance(tool_input, dict) else {}
    model = tool.get("model")
    if not isinstance(model, str) or model not in {
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    }:
        raise StateCorruption("spawn model must be an explicit supported model")
    fork_mode = tool.get("fork_turns")
    if fork_mode not in FORK_MODES:
        raise StateCorruption("fork_turns must be an explicit none or all value")
    novel_ui = values.get("NOVEL_UI_COMPLEXITY", "").lower() == "high"
    if values.get("NOVEL_UI_COMPLEXITY", "") and not novel_ui:
        raise StateCorruption("NOVEL_UI_COMPLEXITY must be high when supplied")
    override_kind, override_evidence = sol_override(
        values.get("SOL_OVERRIDE_REASON", "")
    )
    lane_id = values.get("LEDGER_LANE", "").strip()
    if not lane_id or len(lane_id) > 128:
        raise StateCorruption("LEDGER_LANE is required and must be bounded")
    capacity = ledger_integer(
        values.get("LEDGER_SLOT_CAPACITY", ""),
        "LEDGER_SLOT_CAPACITY",
        1,
        MAX_SLOT_CAPACITY,
    )
    active_count = ledger_integer(
        values.get("LEDGER_CURRENT_ACTIVE", ""),
        "LEDGER_CURRENT_ACTIVE",
        1,
        capacity,
    )
    return {
        "classification": classification,
        "fork_mode": fork_mode,
        "gate_hashes": gate_hashes,
        "lane_hash": opaque_id(lane_id),
        "ledger_active_count": active_count,
        "ledger_capacity": capacity,
        "model": model,
        "model_class": model_class(model),
        "novel_ui": novel_ui,
        "observed": True,
        "parallelism_class": parallelism_class,
        "sol_override_evidence_hash": override_evidence,
        "sol_override_kind": override_kind,
    }


def policy_error(metadata: dict[str, Any]) -> str:
    if metadata["fork_mode"] == "all":
        return "fork_turns=all is prohibited for bounded coordination workers"
    if (
        metadata["classification"] == "non-ui"
        and metadata["model"] != "gpt-5.6-terra"
    ):
        return "non-UI workers require an explicit gpt-5.6-terra model"
    if metadata["model_class"] != "sol":
        return ""
    if metadata["classification"] == "ui" and metadata["novel_ui"]:
        return ""
    if metadata["sol_override_kind"] == "user-requested":
        return ""
    if (
        metadata["sol_override_kind"] == "terra-blocked"
        and metadata["sol_override_evidence_hash"]
    ):
        return ""
    return (
        "gpt-5.6-sol requires explicit novel high-complexity UI metadata or "
        "SOL_OVERRIDE_REASON: user-requested|terra-blocked:<specific evidence>"
    )


def empty_dispatch_metadata() -> dict[str, Any]:
    return {
        "classification": "",
        "fork_mode": "",
        "gate_hashes": [],
        "lane_hash": "",
        "ledger_active_count": 0,
        "ledger_capacity": 0,
        "model": "",
        "model_class": "",
        "novel_ui": False,
        "observed": False,
        "parallelism_class": "",
        "sol_override_evidence_hash": "",
        "sol_override_kind": "",
    }


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
    metadata = spawn_metadata(payload.get("tool_input"))
    error = policy_error(metadata)
    if error:
        return wait_denial(
            f"spawn_agent is blocked because {error}. Do not retry; correct "
            "the declared worker metadata before dispatching."
        )
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    call_key = pending_key(payload)
    with session_lock(lock_path):
        state, _ = load_state(state_path, session_id)
        if has_recovery_barrier(state):
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "spawn-denied-legacy-recovery",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
            return wait_denial(
                "spawn_agent is blocked because this authoritative lifecycle "
                "session is under explicit legacy-state recovery. Do not retry; "
                "reconcile surviving workers with the one guarded wait or use "
                "the SessionStart operator command only after list_agents "
                "confirms that no native children remain."
            )
        if call_key not in state["pending"]:
            now = time.time()
            state["dispatch_sequence"] += 1
            state["pending"][call_key] = {
                "created_at": now,
                "outcome": "prepared",
                "sequence": state["dispatch_sequence"],
                "dispatch_metadata": metadata,
                "target_hashes": spawn_target_hashes(payload.get("tool_input")),
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
        response_hash = response_target_hash(payload.get("tool_response"))
        if (
            response_hash
            and response_hash not in state["pending"][call_key]["target_hashes"]
            and len(state["pending"][call_key]["target_hashes"])
            < MAX_TARGET_HASHES
        ):
            state["pending"][call_key]["target_hashes"].append(response_hash)
            state["pending"][call_key]["target_hashes"].sort()
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


def interrupt_target(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise StateCorruption("interrupt_agent tool_input is not an object")
    target = tool_input.get("target")
    if not isinstance(target, str) or not target or len(target) > 512:
        raise StateCorruption("interrupt_agent target is invalid")
    return target


def record_interrupt_request(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_interrupt_agent_hook_name(tool_name):
        return {}
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing interrupt_agent session identity")
    target = interrupt_target(payload)
    call_key = pending_key(payload)
    state_path, lock_path, _, audit_path = state_paths(payload)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        matched = exact_work_target(state, target) if existed else None
        if matched is None:
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "interrupt-denied-unmatched",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
            return wait_denial(
                "interrupt_agent is blocked because its target does not match "
                "exactly one tracked worker or pending dispatch. Do not retry; "
                "use one-shot list_agents for diagnosis and reconcile the "
                "target identity before interrupting."
            )
        existing = state["interrupts"].get(call_key)
        if existing is not None and (
            existing["target_hash"] != opaque_id(target)
            or (existing["work_kind"], existing["work_key"]) != matched
        ):
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "interrupt-denied-call-reuse",
                epoch=state["event_epoch"],
                tool_use_id=text_field(payload, "tool_use_id", 256),
            )
            return wait_denial(
                "interrupt_agent is blocked because its tool-call identity was "
                "already bound to a different tracked target. Do not retry; "
                "reconcile the original interruption result."
            )
        if existing is None:
            if len(state["interrupts"]) >= MAX_INTERRUPT_CAPABILITIES:
                return wait_denial(
                    "interrupt_agent is blocked because the bounded interrupt "
                    "capability set is full. Do not retry; reconcile prior "
                    "interrupt results first."
                )
            state["interrupts"][call_key] = {
                "created_at": time.time(),
                "target_hash": opaque_id(target),
                "work_key": matched[1],
                "work_kind": matched[0],
            }
            write_state(state_path, state)
            outcome = "interrupt-recorded"
        else:
            outcome = "interrupt-duplicate"
        audit_record(
            audit_path,
            payload,
            "PreToolUse",
            outcome,
            epoch=state["event_epoch"],
            tool_use_id=text_field(payload, "tool_use_id", 256),
        )
    return {}


def reconcile_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_interrupt_agent_hook_name(tool_name):
        return {}
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing interrupt_agent PostToolUse identity")
    target = interrupt_target(payload)
    call_key = pending_key(payload)
    state_path, lock_path, _, audit_path = state_paths(payload)
    removed = False
    remaining = False
    removed_agent = ""
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        capability = state["interrupts"].pop(call_key, None) if existed else None
        if capability is None:
            outcome = "interrupt-untracked"
        elif capability["target_hash"] != opaque_id(target):
            outcome = "interrupt-target-mismatch"
            write_state(state_path, state)
        elif not interrupt_response_confirms_terminal(payload.get("tool_response")):
            outcome = "interrupt-unconfirmed"
            write_state(state_path, state)
        else:
            matched = exact_work_target(state, target)
            expected = (capability["work_kind"], capability["work_key"])
            if matched != expected:
                outcome = "interrupt-target-changed"
                write_state(state_path, state)
            else:
                if matched[0] == "worker":
                    state["workers"].pop(matched[1], None)
                    removed_agent = matched[1]
                    outcome = "interrupt-worker"
                else:
                    state["pending"].pop(matched[1], None)
                    outcome = "interrupt-pending"
                advance_event(state, "interrupted")
                if has_recovery_barrier(state) and not has_active_work(state):
                    state["legacy_recovery"] = None
                remaining = has_active_work(state)
                write_state(state_path, state)
                removed = True
        audit_record(
            audit_path,
            payload,
            "PostToolUse",
            outcome,
            epoch=state["event_epoch"],
            agent_id=removed_agent,
            tool_use_id=text_field(payload, "tool_use_id", 256),
        )
    if not removed:
        return {}
    context = "The exact interrupted target was removed from tracked work. "
    if remaining:
        context += (
            "Other workers remain; reconcile this result, then arm the one "
            "newly authorized wait_agent."
        )
    else:
        context += "No tracked work remains; reconcile results and finish normally."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def reconcile_wait_return(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = text_field(payload, "tool_name", 128)
    if not is_wait_agent_hook_name(tool_name):
        return {}
    session_id = text_field(payload, "session_id")
    if not session_id:
        raise StateCorruption("missing wait_agent PostToolUse identity")
    call_key = pending_key(payload)
    state_path, lock_path, _, audit_path = state_paths(payload)
    rearmed = False
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        if not existed or state["wait_call_hash"] != call_key:
            outcome = "wait-return-untracked"
        else:
            state["wait_call_hash"] = ""
            if has_recovery_barrier(state):
                outcome = "wait-return-legacy-recovery"
            elif (
                wait_response_is_event(payload.get("tool_response"))
                and has_active_work(state)
            ):
                advance_event(state, "wait-returned-event")
                rearmed = True
                outcome = "wait-return-rearmed"
            elif has_active_work(state):
                outcome = "wait-return-no-event"
            else:
                outcome = "wait-return-idle"
            write_state(state_path, state)
        audit_record(
            audit_path,
            payload,
            "PostToolUse",
            outcome,
            epoch=state["event_epoch"],
            tool_use_id=text_field(payload, "tool_use_id", 256),
        )
    if not rearmed:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "The native wait returned for a non-timeout event while tracked "
                "work remains. Reconcile every delivered MESSAGE/FINAL/error, "
                "then arm exactly one newly authorized wait_agent."
            ),
        }
    }


def tracked_dispatches(state: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for call_hash, pending in state["pending"].items():
        values.append(
            {
                "agent_hash": "",
                "identity": f"pending:{call_hash}",
                "metadata": pending["dispatch_metadata"],
                "target_hashes": pending["target_hashes"],
                "tool_hash": call_hash,
            }
        )
    for agent_id, worker in state["workers"].items():
        values.append(
            {
                "agent_hash": opaque_id(agent_id),
                "identity": f"worker:{opaque_id(agent_id)}",
                "metadata": worker["dispatch_metadata"],
                "target_hashes": worker["target_hashes"],
                "tool_hash": worker["dispatch_key"],
            }
        )
    return values


def matching_dispatch(state: dict[str, Any], dispatch_map: dict[str, str]) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for candidate in tracked_dispatches(state):
        if (
            dispatch_map["tool_use_id"]
            and dispatch_map["tool_use_id"] != candidate["tool_hash"]
        ) or (
            dispatch_map["tool_use_hash"]
            and dispatch_map["tool_use_hash"] != candidate["tool_hash"]
        ):
            continue
        if dispatch_map["agent_id"] and dispatch_map["agent_id"] != candidate["agent_hash"]:
            continue
        target_hashes = set(candidate["target_hashes"])
        if dispatch_map["task_name"] and dispatch_map["task_name"] not in target_hashes:
            continue
        if dispatch_map["target_hash"] and dispatch_map["target_hash"] not in target_hashes:
            continue
        matches.append(candidate)
    if len(matches) != 1:
        return None
    return matches[0]


def cross_check_dispatch(lane: dict[str, Any], candidate: dict[str, Any]) -> None:
    metadata = candidate["metadata"]
    if not metadata["observed"]:
        return
    if metadata["classification"] and metadata["classification"] != lane["classification"]:
        raise StateCorruption("spawn classification conflicts with lane declaration")
    if (
        metadata["parallelism_class"]
        and metadata["parallelism_class"] != lane["parallelism_class"]
    ):
        raise StateCorruption("spawn parallelism class conflicts with lane declaration")
    if (
        metadata["parallelism_class"]
        and metadata["gate_hashes"] != lane["gate_hashes"]
    ):
        raise StateCorruption("spawn exclusive gates conflict with lane declaration")
    if metadata["model"] and metadata["model"] != lane["model"]:
        raise StateCorruption("spawn model is not the explicitly declared lane model")
    if metadata["fork_mode"] and metadata["fork_mode"] != lane["fork_mode"]:
        raise StateCorruption("spawn fork mode conflicts with lane declaration")
    if lane["classification"] == "non-ui" and metadata["model"] != "gpt-5.6-terra":
        raise StateCorruption("non-UI spawn did not explicitly select Terra")
    if metadata["model_class"] == "sol" and policy_error(metadata):
        raise StateCorruption("spawn Sol policy could not be verified")


def dispatch_map_for_candidate(candidate: dict[str, Any]) -> dict[str, str]:
    result = {field: "" for field in DISPATCH_MAP_FIELDS}
    if candidate["tool_hash"]:
        result["tool_use_hash"] = candidate["tool_hash"]
    else:
        result["agent_id"] = candidate["agent_hash"]
    return result


def hook_owned_ledger(state: dict[str, Any]) -> dict[str, Any]:
    """Build the current-epoch ledger only from root hook capabilities.

    A normal root spawn is the only callable declaration surface available to
    a newly activated session. The hook binds each declared lane to that
    payload's pending capability, never to a caller-supplied session id or a
    process outside the hook host.
    """
    candidates = tracked_dispatches(state)
    if not candidates or len(candidates) > MAX_LEDGER_LANES:
        raise StateCorruption("tracked dispatches cannot form a bounded ledger")
    observed = [candidate for candidate in candidates if candidate["metadata"]["observed"]]
    if observed and len(observed) != len(candidates):
        raise StateCorruption("mixed observed and unpaired dispatches cannot form a ledger")
    if observed:
        capacities = {candidate["metadata"]["ledger_capacity"] for candidate in candidates}
        declared_counts = {
            candidate["metadata"]["ledger_active_count"] for candidate in candidates
        }
        if len(capacities) != 1 or len(declared_counts) != 1:
            raise StateCorruption("root dispatches disagree on ledger capacity or active count")
        capacity = capacities.pop()
        declared_active = declared_counts.pop()
        if declared_active != len(candidates):
            raise StateCorruption("declared active count does not match tracked dispatches")
    else:
        # Lifecycle-only events have no native parent dispatch payload. Keep
        # them mapped exactly once for recovery compatibility, but do not
        # claim that their spawn model or lane declaration was observed.
        capacity = len(candidates)

    lanes: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        metadata = candidate["metadata"]
        lane_hash = (
            metadata["lane_hash"]
            if metadata["observed"]
            else opaque_id(f"unpaired:{candidate['identity']}")
        )
        if lane_hash in lanes:
            raise StateCorruption("tracked dispatches map to a duplicate declared lane")
        lanes[lane_hash] = {
            "classification": metadata["classification"] or "non-ui",
            "dependency_hashes": [],
            "dispatch_map": dispatch_map_for_candidate(candidate),
            "external_blocker_hash": "",
            "fork_mode": metadata["fork_mode"] or "none",
            "gate_hashes": metadata["gate_hashes"],
            "model": metadata["model"] or "gpt-5.6-terra",
            "model_class": metadata["model_class"] or "terra",
            "novel_ui": metadata["novel_ui"],
            "parallelism_class": metadata["parallelism_class"] or "read-only",
            "sol_override_evidence_hash": metadata["sol_override_evidence_hash"],
            "sol_override_kind": metadata["sol_override_kind"],
            "status": "active",
        }
    ledger = {
        "active_count": len(candidates),
        "capacity": capacity,
        "epoch": state["event_epoch"],
        "lanes": lanes,
    }
    validate_ledger_coverage(state, ledger)
    return ledger


def validate_ledger_coverage(state: dict[str, Any], ledger: dict[str, Any]) -> None:
    """Require a bijection between tracked native work and active lanes."""
    candidates = tracked_dispatches(state)
    active_count = len(candidates)
    if ledger["active_count"] != active_count:
        raise StateCorruption("ledger active count does not match tracked dispatches")
    if active_count > ledger["capacity"]:
        raise StateCorruption("tracked dispatches exceed declared ledger capacity")
    covered: set[str] = set()
    planned_gates: set[str] = set()
    for lane in ledger["lanes"].values():
        mapped = any(lane["dispatch_map"].values())
        if lane["status"] in {"active", "eligible"}:
            if not mapped:
                raise StateCorruption("active or eligible lane lacks a dispatch mapping")
            candidate = matching_dispatch(state, lane["dispatch_map"])
            if candidate is None:
                raise StateCorruption("active lane maps to a missing or phantom dispatch")
            if candidate["identity"] in covered:
                raise StateCorruption("tracked dispatch maps to more than one active lane")
            covered.add(candidate["identity"])
            cross_check_dispatch(lane, candidate)
            if lane["parallelism_class"] == "exclusive-gate":
                duplicate_gates = planned_gates.intersection(lane["gate_hashes"])
                if duplicate_gates:
                    raise StateCorruption("duplicate active exclusive gate")
                planned_gates.update(lane["gate_hashes"])
        elif mapped:
            raise StateCorruption("non-active lane must not include a dispatch mapping")
    actual = {candidate["identity"] for candidate in candidates}
    if covered != actual:
        raise StateCorruption("ledger omits one or more tracked active dispatches")
    if len(covered) != ledger["active_count"]:
        raise StateCorruption("ledger count-only coverage is invalid")


def bounded_legacy_count(value: Any) -> int:
    if isinstance(value, dict) and len(value) <= MAX_LEDGER_LANES:
        return len(value)
    return -1


def valid_legacy_timestamp(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def migrate_v7_state(value: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Preserve v7 lifecycle identities without fabricating v12 ledger data."""
    expected = {
        "dispatch_sequence",
        "event_epoch",
        "interrupts",
        "last_event",
        "pending",
        "session_hash",
        "stop_continuation_epoch",
        "version",
        "wait_call_hash",
        "wait_issued_epoch",
        "workers",
    }
    if (
        set(value) != expected
        or value.get("version") != 7
        or value.get("session_hash") != opaque_id(session_id)
        or not isinstance(value.get("workers"), dict)
        or not isinstance(value.get("pending"), dict)
        or not isinstance(value.get("interrupts"), dict)
        or len(value["workers"]) + len(value["pending"]) > MAX_LEDGER_LANES
        or len(value["interrupts"]) > MAX_INTERRUPT_CAPABILITIES
        or type(value.get("dispatch_sequence")) is not int
        or value["dispatch_sequence"] < 0
        or type(value.get("event_epoch")) is not int
        or value["event_epoch"] < 0
        or type(value.get("wait_issued_epoch")) is not int
        or value["wait_issued_epoch"] < -1
        or type(value.get("stop_continuation_epoch")) is not int
        or value["stop_continuation_epoch"] < -1
        or not isinstance(value.get("wait_call_hash"), str)
        or (
            value["wait_call_hash"]
            and not valid_digest(value["wait_call_hash"])
        )
        or not isinstance(value.get("last_event"), str)
    ):
        raise StateCorruption("legacy v7 top-level schema is unsafe to migrate")

    workers: dict[str, dict[str, Any]] = {}
    worker_fields = {
        "agent_type",
        "started_at",
        "target_hashes",
        "turn_id",
        "updated_at",
    }
    for agent_id, worker in value["workers"].items():
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or len(agent_id) > 512
            or not isinstance(worker, dict)
            or set(worker) != worker_fields
            or not isinstance(worker.get("agent_type"), str)
            or len(worker["agent_type"]) > 128
            or not isinstance(worker.get("turn_id"), str)
            or not worker["turn_id"]
            or len(worker["turn_id"]) > 512
            or not valid_legacy_timestamp(worker.get("started_at"))
            or not valid_legacy_timestamp(worker.get("updated_at"))
            or not valid_target_hashes(worker.get("target_hashes"))
        ):
            raise StateCorruption("legacy v7 worker cannot be migrated safely")
        workers[agent_id] = {
            "agent_type": worker["agent_type"],
            "dispatch_key": "",
            "dispatch_metadata": empty_dispatch_metadata(),
            "started_at": worker["started_at"],
            "target_hashes": list(worker["target_hashes"]),
            "turn_id": opaque_id(worker["turn_id"]),
            "updated_at": worker["updated_at"],
        }

    pending: dict[str, dict[str, Any]] = {}
    pending_fields = {
        "created_at",
        "outcome",
        "sequence",
        "target_hashes",
        "turn_hash",
        "updated_at",
    }
    for call_id, record in value["pending"].items():
        if (
            not valid_digest(call_id)
            or not isinstance(record, dict)
            or set(record) != pending_fields
            or not valid_legacy_timestamp(record.get("created_at"))
            or not valid_legacy_timestamp(record.get("updated_at"))
            or type(record.get("sequence")) is not int
            or record["sequence"] <= 0
            or not valid_digest(record.get("turn_hash"))
            or not isinstance(record.get("outcome"), str)
            or len(record["outcome"]) > 64
            or not valid_target_hashes(record.get("target_hashes"))
        ):
            raise StateCorruption("legacy v7 pending dispatch cannot be migrated safely")
        pending[call_id] = {
            "created_at": record["created_at"],
            "dispatch_metadata": empty_dispatch_metadata(),
            "outcome": record["outcome"],
            "sequence": record["sequence"],
            "target_hashes": list(record["target_hashes"]),
            "turn_hash": record["turn_hash"],
            "updated_at": record["updated_at"],
        }

    interrupts: dict[str, dict[str, Any]] = {}
    interrupt_fields = {"created_at", "target_hash", "work_key", "work_kind"}
    for call_id, record in value["interrupts"].items():
        if (
            not valid_digest(call_id)
            or not isinstance(record, dict)
            or set(record) != interrupt_fields
            or not valid_legacy_timestamp(record.get("created_at"))
            or not valid_digest(record.get("target_hash"))
            or record.get("work_kind") not in {"pending", "worker"}
            or not isinstance(record.get("work_key"), str)
        ):
            raise StateCorruption("legacy v7 interrupt capability cannot be migrated safely")
        interrupts[call_id] = dict(record)

    migrated = new_state(session_id)
    migrated["dispatch_sequence"] = value["dispatch_sequence"]
    migrated["event_epoch"] = value["event_epoch"] + 1
    migrated["interrupts"] = interrupts
    migrated["last_event"] = "legacy-v7-migrated"
    migrated["pending"] = pending
    migrated["wait_issued_epoch"] = value["event_epoch"]
    migrated["workers"] = workers
    if workers or pending:
        migrated["legacy_recovery"] = {
            "kind": "migrated",
            "pending_count": len(pending),
            "quarantine_hash": "",
            "source_version": 7,
            "worker_count": len(workers),
        }
    return migrated


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def quarantine_state_bytes(
    state_path: Path, raw: bytes, version: int
) -> tuple[str, Path]:
    """Move legacy bytes to a collision-safe, idempotent owned quarantine."""
    if regular_file_bytes(state_path, "legacy session state") != raw:
        raise StateCorruption("legacy session state changed during recovery")
    digest = hashlib.sha256(raw).hexdigest()
    quarantine = private_directory(state_path.parent.parent / "quarantine")
    target = quarantine / f"{state_path.stem}.v{version}.{digest}.json"
    try:
        os.link(state_path, target, follow_symlinks=False)
        fsync_directory(quarantine)
    except FileExistsError:
        if regular_file_bytes(target, "legacy quarantine") != raw:
            raise StateCorruption("legacy quarantine collision is unsafe")
    # Keep the old state name until write_state() atomically replaces it with
    # the recovery barrier. A crash between these operations therefore leaves
    # both the original name and the durable quarantine, never apparent
    # missing state.
    return digest, target


def legacy_barrier_state(
    legacy: LegacyState, state_path: Path, session_id: str
) -> dict[str, Any]:
    digest, _ = quarantine_state_bytes(
        state_path, legacy.raw, legacy.version
    )
    state = new_state(session_id)
    state["event_epoch"] = 1
    state["last_event"] = "legacy-quarantined"
    state["legacy_recovery"] = {
        "kind": "quarantined-legacy",
        "pending_count": bounded_legacy_count(legacy.value.get("pending")),
        "quarantine_hash": digest,
        "source_version": legacy.version,
        "worker_count": bounded_legacy_count(legacy.value.get("workers")),
    }
    return state


def quarantinable_state_version(raw: bytes) -> int:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return -1
    if not isinstance(value, dict):
        return -1
    version = value.get("version")
    return version if type(version) is int and -1 <= version <= 1_000_000 else -1


def current_corruption_barrier_state(
    state_path: Path, session_id: str
) -> dict[str, Any]:
    """Preserve a safely readable malformed current object behind a barrier."""
    raw = regular_file_bytes(state_path, "corrupt current session state")
    version = quarantinable_state_version(raw)
    digest, _ = quarantine_state_bytes(state_path, raw, version)
    state = new_state(session_id)
    state["event_epoch"] = 1
    state["last_event"] = "current-state-quarantined"
    state["legacy_recovery"] = {
        "kind": "quarantined-current",
        "pending_count": -1,
        "quarantine_hash": digest,
        "source_version": version,
        "worker_count": -1,
    }
    return state


def operator_recovery_command(session_id: str) -> str:
    return (
        f"{OPERATOR_RECOVERY_PREFIX} {opaque_id(session_id)} "
        f"{OPERATOR_RECOVERY_CONFIRMATION}"
    )


def legacy_recovery_context(state: dict[str, Any], session_id: str) -> str:
    recovery = state["legacy_recovery"]
    assert recovery is not None
    if recovery["kind"] == "migrated":
        detail = (
            "A legacy v7 lifecycle state was migrated without inventing ledger "
            "metadata. Tracked worker and pending identities were preserved. "
            "Exactly one ledger-bypass recovery wait_agent is available for "
            "the current recovery epoch; conflicting dispatch and final remain "
            "blocked while legacy work is tracked."
        )
    elif recovery["kind"] == "quarantined-legacy":
        detail = (
            "A legacy v7 lifecycle state could not be migrated safely and was "
            "moved to a mode-600 content-addressed quarantine. Its current "
            "session remains behind a recovery barrier; exactly one guarded "
            "recovery wait_agent is available, and dispatch/final remain blocked."
        )
    else:
        detail = (
            "A malformed current-version lifecycle state was moved to a "
            "mode-600 content-addressed quarantine without treating it as "
            "empty. Its current session remains fail-closed behind a recovery "
            "barrier; exactly one guarded recovery wait_agent is available, "
            "and dispatch/final remain blocked."
        )
    return (
        detail
        + " Run one-shot list_agents. Only if it shows /root and no native "
        "children, submit this exact session-bound operator command: "
        + operator_recovery_command(session_id)
    )


def session_start(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = text_field(payload, "session_id")
    source = text_field(payload, "source", 32)
    if not session_id or source not in SESSION_SOURCES:
        raise StateCorruption("invalid SessionStart identity or source")
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    recovery_context = ""
    with session_lock(lock_path):
        try:
            state, existed = load_state(state_path, session_id)
        except LegacyState as legacy:
            load_recovery_control(recovery_path, session_id)
            try:
                state = migrate_v7_state(legacy.value, session_id)
                outcome = (
                    "legacy-v7-migrated"
                    if has_recovery_barrier(state)
                    else "legacy-v7-migrated-empty"
                )
            except StateCorruption:
                state = legacy_barrier_state(legacy, state_path, session_id)
                outcome = f"legacy-v{legacy.version}-quarantined"
            write_state(state_path, state)
            remove_path(recovery_path)
            existed = True
            if has_recovery_barrier(state):
                recovery_context = legacy_recovery_context(state, session_id)
            audit_record(
                audit_path,
                payload,
                "SessionStart",
                outcome,
                epoch=state["event_epoch"],
            )
        except StateCorruption:
            load_recovery_control(recovery_path, session_id)
            state = current_corruption_barrier_state(state_path, session_id)
            write_state(state_path, state)
            remove_path(recovery_path)
            existed = True
            recovery_context = legacy_recovery_context(state, session_id)
            audit_record(
                audit_path,
                payload,
                "SessionStart",
                "current-state-quarantined",
                epoch=state["event_epoch"],
            )
        else:
            load_recovery_control(recovery_path, session_id)
            if existed and has_recovery_barrier(state):
                recovery_context = legacy_recovery_context(state, session_id)
            # A successfully validated lifecycle boundary closes any prior
            # failure episode. A corrupt state cannot reach this reset.
            remove_path(recovery_path)
        prune_audit_directory(audit_path)
        audit_record(audit_path, payload, "SessionStart", "ready")
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": recovery_context or (
                "The interactive collaboration event gate is active. After "
                "dispatching bounded native workers with LEDGER_LANE, "
                "LEDGER_SLOT_CAPACITY, and LEDGER_CURRENT_ACTIVE headers, call "
                "wait_agent once. Its PreToolUse hook atomically registers the "
                "session-bound ledger from those root dispatch capabilities, "
                "checks the declared active count against observed dispatches, "
                "and rewrites the wait to 3600000ms; it remains interruptible by "
                "steered user input. Never poll or repeat a wait without a new "
                "dispatch, worker completion, non-timeout wait return, confirmed "
                "interruption, or steering event; those events invalidate the "
                "ledger. interrupt_agent removes only "
                "its exact tracked target after a matching successful response. "
                "Stop returns immediately and denies final while workers remain. "
                "Reconcile native MESSAGE/FINAL/error results before final and "
                "never promise a passive post-final wake. Workers must not spawn "
                "workers."
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
    call_key = pending_key(payload)
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
                    state["wait_call_hash"] = call_key
                    write_state(state_path, state)
            except Exception:
                pass
            audit_record(
                audit_path, payload, "PreToolUse", "wait-recovery-allowed"
            )
            return wait_allow(
                tool_input,
                "One failure-recovery wait was consumed and rewritten to "
                "3600000ms. Do not call wait_agent again without a newly "
                "authorized lifecycle, tool-return, or steering event.",
            )
        state, _ = load_state(state_path, session_id)
        if has_recovery_barrier(state):
            if state["wait_issued_epoch"] == state["event_epoch"]:
                audit_record(
                    audit_path,
                    payload,
                    "PreToolUse",
                    "wait-denied-legacy-recovery-consumed",
                    epoch=state["event_epoch"],
                )
                return wait_denial(
                    "The one legacy-state recovery wait_agent authorization "
                    "for this recovery epoch was already consumed. Do not retry "
                    "or use Stop to create a loop. Reconcile an actual worker "
                    "completion, or use the exact session-bound operator command "
                    "only after list_agents confirms no native children remain."
                )
            state["wait_issued_epoch"] = state["event_epoch"]
            state["wait_call_hash"] = call_key
            write_state(state_path, state)
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "wait-legacy-recovery-allowed",
                epoch=state["event_epoch"],
            )
            return wait_allow(
                tool_input,
                "One session-bound legacy-state recovery wait was consumed "
                "and rewritten to 3600000ms without constructing ledger "
                "metadata. Do not repeat it unless a real tracked worker "
                "completion creates a new recovery epoch.",
            )
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
        ledger = state["ledger"]
        if ledger is None:
            try:
                ledger = hook_owned_ledger(state)
            except StateCorruption:
                audit_record(
                    audit_path,
                    payload,
                    "PreToolUse",
                    "wait-denied-ledger-registration",
                    epoch=state["event_epoch"],
                )
                return wait_denial(
                    "wait_agent is blocked because the hook-owned session ledger "
                    "could not verify every root dispatch declaration. Do not retry "
                    "or poll; reconcile lifecycle state and dispatch a fresh, "
                    "completely declared root batch."
                )
            state["ledger"] = ledger
            state["ledger_verified"] = True
            write_state(state_path, state)
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "ledger-registered-hook",
                epoch=state["event_epoch"],
            )
        if ledger["epoch"] != state["event_epoch"]:
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "wait-denied-ledger-stale",
                epoch=state["event_epoch"],
            )
            return wait_denial(
                "wait_agent is blocked because a fresh session-bound coordination "
                "ledger is required for the current lifecycle epoch. Reconcile and "
                "use the next root spawn declarations before one normal wait; steering, "
                "completion, and confirmed interruption invalidate it. Do not retry "
                "or poll."
            )
        try:
            validate_ledger_coverage(state, ledger)
        except StateCorruption:
            audit_record(
                audit_path,
                payload,
                "PreToolUse",
                "wait-denied-ledger-coverage",
                epoch=state["event_epoch"],
            )
            return wait_denial(
                "wait_agent is blocked because the session-bound ledger does not "
                "map every tracked active or pending native dispatch exactly once. "
                "Do not retry or poll; reconcile lifecycle state and dispatch a "
                "fresh, completely declared root batch."
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
                "after a new dispatch, worker completion, non-timeout wait "
                "return, confirmed interruption, or steered user input."
            )
        state["wait_issued_epoch"] = state["event_epoch"]
        state["wait_call_hash"] = call_key
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
            "input. Reconcile every returned MESSAGE/FINAL/error or steering "
            "event before arming another authorized wait.",
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
            matched = oldest_pending(state) if is_new else None
            if not is_new:
                target_hashes = state["workers"][agent_id]["target_hashes"]
                dispatch_key = state["workers"][agent_id]["dispatch_key"]
                dispatch_metadata = state["workers"][agent_id]["dispatch_metadata"]
            elif matched is not None:
                target_hashes = list(matched[1]["target_hashes"])
                dispatch_key = matched[0]
                dispatch_metadata = matched[1]["dispatch_metadata"]
            else:
                target_hashes = [opaque_id(agent_id)]
                dispatch_key = ""
                dispatch_metadata = empty_dispatch_metadata()
            state["workers"][agent_id] = {
                "agent_type": text_field(payload, "agent_type"),
                "dispatch_key": dispatch_key,
                "dispatch_metadata": dispatch_metadata,
                "started_at": (
                    now
                    if is_new
                    else state["workers"][agent_id]["started_at"]
                ),
                "target_hashes": target_hashes,
                "turn_id": opaque_id(text_field(payload, "turn_id")),
                "updated_at": now,
            }
            if is_new:
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
                if has_recovery_barrier(state) and not has_active_work(state):
                    state["legacy_recovery"] = None
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
                    if has_recovery_barrier(state) and not has_active_work(state):
                        state["legacy_recovery"] = None
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
    state_path, lock_path, recovery_path, audit_path = state_paths(payload)
    active = False
    recovery_context = ""
    prompt = text_field(payload, "prompt", 512).strip()
    expected_command = operator_recovery_command(session_id)
    with session_lock(lock_path):
        state, existed = load_state(state_path, session_id)
        if prompt.startswith(OPERATOR_RECOVERY_PREFIX):
            if prompt != expected_command:
                recovery_context = (
                    "The legacy-state repair command was rejected because it "
                    "does not match this hook payload's authoritative session "
                    "identity and exact confirmation phrase. No state changed."
                )
                outcome = "operator-repair-identity-mismatch"
            elif existed and has_recovery_barrier(state):
                repaired = new_state(session_id)
                repaired["event_epoch"] = state["event_epoch"] + 1
                repaired["last_event"] = "operator-repaired-empty"
                write_state(state_path, repaired)
                remove_path(recovery_path)
                state = repaired
                recovery_context = (
                    "The explicit session-bound operator confirmation reset "
                    "the quarantined or migrated legacy barrier to valid empty "
                    "current state. Normal bounded spawn_agent followed by the "
                    "hook-owned ledger wait is available."
                )
                outcome = "operator-repaired-empty"
            elif existed and state["last_event"] == "operator-repaired-empty":
                recovery_context = (
                    "This exact session-bound operator recovery was already "
                    "applied; the valid empty current state is unchanged."
                )
                outcome = "operator-repair-duplicate"
            else:
                recovery_context = (
                    "The session-bound operator repair command was rejected "
                    "because no validated legacy recovery barrier exists. No "
                    "current-version state was discarded."
                )
                outcome = "operator-repair-no-barrier"
            audit_record(
                audit_path,
                payload,
                "UserPromptSubmit",
                outcome,
                epoch=state["event_epoch"],
            )
        elif existed and has_recovery_barrier(state):
            recovery_context = legacy_recovery_context(state, session_id)
            audit_record(
                audit_path,
                payload,
                "UserPromptSubmit",
                "legacy-recovery-steering-bounded",
                epoch=state["event_epoch"],
            )
        elif existed and has_active_work(state):
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
    if recovery_context:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": recovery_context,
            }
        }
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
        if has_recovery_barrier(state):
            message = legacy_recovery_context(state, session_id)
            message += (
                " Stop cannot mint another recovery epoch or continuation; "
                "final and conflicting dispatch remain denied."
            )
            audit_record(
                audit_path,
                payload,
                "Stop",
                "denied-legacy-recovery",
                epoch=state["event_epoch"],
            )
            if payload.get("stop_hook_active") is True:
                return {
                    "continue": False,
                    "stopReason": message,
                    "systemMessage": message,
                }
            return {"decision": "block", "reason": message}
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
                "exactly one fresh-ledger wait_agent now; PreToolUse will rewrite it to "
                "3600000ms. The native subscription uses no model polling turns "
                "and steered user input can interrupt it. Reconcile every "
                "returned MESSAGE/FINAL/error or steering event before another "
                "wait, and never promise a passive post-final wake."
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
        tool_name = text_field(payload, "tool_name", 128)
        if is_spawn_agent_hook_name(tool_name):
            return record_spawn_dispatch(payload)
        if is_interrupt_agent_hook_name(tool_name):
            return record_interrupt_request(payload)
        return guard_wait_agent(payload)
    if event == "PostToolUse":
        tool_name = text_field(payload, "tool_name", 128)
        if is_spawn_agent_hook_name(tool_name):
            return reconcile_spawn_dispatch(payload)
        if is_interrupt_agent_hook_name(tool_name):
            return reconcile_interrupt(payload)
        return reconcile_wait_return(payload)
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
            tool_name = text_field(payload, "tool_name", 128)
            if is_interrupt_agent_hook_name(tool_name):
                guarded_tool = "interrupt_agent"
            elif is_spawn_agent_hook_name(tool_name):
                guarded_tool = "spawn_agent"
            else:
                guarded_tool = "wait_agent"
            emit(
                wait_denial(
                    f"{guarded_tool} guard failed closed because session state could "
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
