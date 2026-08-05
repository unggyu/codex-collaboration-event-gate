# Architecture

## Goal

Keep a root Codex coordinator interactive while native workers run. The plugin
uses lifecycle hooks for state and one native `wait_agent` call for blocking;
the `Stop` hook is an enforcement check, not the waiter.

## Components

| Component | Responsibility |
| --- | --- |
| `SessionStart` | Validate plugin data and inject the coordination contract |
| `PreToolUse(spawn_agent)` | Record a pending dispatch before native worker creation can race the lifecycle hook |
| `PostToolUse(spawn_agent)` | Clear explicitly failed dispatches and retain successful pending work until reconciliation |
| `SubagentStart` | Promote the oldest session-local pending dispatch to a worker; record unpaired starts safely |
| `SubagentStop` | Remove a worker or reconcile a dropped `SubagentStart` pending dispatch |
| `UserPromptSubmit` | Re-arm one wait when steering arrives with active workers |
| `PreToolUse` | Deny child/repeated waits; allow one root wait and force `3600000ms` |
| `Stop` | Return immediately; deny final while workers remain |
| `SessionEnd` | Remove transient state for a closed parent session |
| Opaque audit | Retain bounded, hashed lifecycle outcomes under `PLUGIN_DATA/audit` |
| Bundled skill | Teach the root reconciliation and no-polling workflow |

## State machine

Each session uses a SHA-256 filename beneath `${PLUGIN_DATA}/sessions`. Its
state contains active workers, pending dispatch capabilities keyed by hashed
tool-call id, `event_epoch`, `wait_issued_epoch`, and
`stop_continuation_epoch`. A pending capability is active for `wait_agent` and
`Stop` purposes until a native start promotes it, an explicit spawn failure
clears it, or a later stop reconciles a missing start. This closes the native
delivery gap where `spawn_agent` returns before `SubagentStart` has updated
state. Current Codex native lifecycle payloads identify a child session/turn,
not the parent `spawn_agent` tool call or turn, so promotion is strictly
session-local FIFO: one start (or missing-start stop fallback) consumes exactly
one oldest pending dispatch. A lock-protected monotonic dispatch sequence, not
wall-clock timestamp ordering, makes concurrent dispatch ordering deterministic.

| Event | State transition | Next allowed action |
| --- | --- | --- |
| Pre-dispatch | Add pending tool-call capability; increment epoch | One root wait |
| Native worker start | Promote matching pending worker; no extra epoch | Same dispatch wait |
| Explicit spawn failure | Remove pending; increment epoch | One wait only if other work remains |
| Unpaired worker start | Add worker; increment epoch | One root wait |
| First root wait | Set `wait_issued_epoch` | No repeat in this epoch |
| Worker completion | Remove worker or unpaired pending; increment epoch | One wait if work remains |
| User steering | Increment epoch | One wait after reconciliation |
| New dispatch | Add worker; increment epoch | One wait |
| First active-worker Stop | Increment epoch; mark continuation | One wait from immediate continuation |
| Repeated active-worker Stop | No transition; `continue:false` | Explicit recovery, no loop |
| Zero-worker Stop | Remove session/recovery state | Final may pass |

Duplicate `SubagentStart` and unrelated `SubagentStop` events do not create a
new authorization. File locking serializes concurrent command-hook processes.
Audit records contain only epoch/outcome/event plus SHA-256 identities for the
session, turn, tool call, and agent when present; they retain at most 128
records per session, 256 session files, and 14 days.

## Why Stop does not wait

A synchronous command hook retains the parent turn, but its process also keeps
the Codex composer unavailable on observed current clients. Native
`wait_agent` already provides a blocking event subscription that returns
early for steered user input. The plugin therefore spends one model/tool
transition to arm that native wait and leaves `Stop` immediate.

The hook API can return context, deny a tool, rewrite a tool input, or continue a
turn. It cannot invoke `wait_agent` itself. Consequently, dispatch,
completion, and steering can authorize a wait automatically, but the root model
must issue the next tool call.

## Completion semantics

A native worker FINAL/error notification is evidence to reconcile, not proof
that the user's outcome is complete. The root compares results with acceptance
criteria, dispatches authorized follow-up work when needed, and responds only
after the active map is empty. There is no passive post-final wake mechanism.

## Project independence

The installed hook command resolves through `${PLUGIN_ROOT}`. The hook does
not inspect the current directory, a Git repository, project files, transcript,
or worktree topology. Runtime state resolves only through `${PLUGIN_DATA}`.
The vendoring helper generates an absolute runner path for project hooks and
provides equivalent variables without requiring Git.
