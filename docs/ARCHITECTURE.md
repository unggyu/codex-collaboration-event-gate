# Architecture

## Goal

Keep a root Codex coordinator interactive while native workers run. The plugin
uses lifecycle hooks for state and one native `wait_agent` call for blocking;
the `Stop` hook is an enforcement check, not the waiter.

## Components

| Component | Responsibility |
| --- | --- |
| `SessionStart` | Atomically initialize missing current state, validate existing state, migrate strict legacy state or create a quarantine barrier, and inject the coordination/recovery contract |
| `PreToolUse(spawn_agent)` | Record a pending dispatch before native worker creation can race the lifecycle hook |
| `PreToolUse(wait_agent)` | Atomically build and validate one bounded, hash-only coordination ledger from the current root spawn capabilities |
| `PostToolUse(spawn_agent)` | Clear explicitly failed dispatches and retain successful pending work until reconciliation |
| `Pre/PostToolUse(interrupt_agent)` | Bind one interruption to an exact tracked target and remove it only after a confirmed successful response |
| `PostToolUse(wait_agent)` | Re-arm one subscription after a correlated non-timeout return while work remains |
| `SubagentStart` | Promote the oldest session-local pending dispatch to a worker; record unpaired starts safely |
| `SubagentStop` | Remove a worker or reconcile a dropped `SubagentStart` pending dispatch |
| `UserPromptSubmit` | Re-arm one wait when normal steering arrives, or apply an exact session-bound operator repair to a validated legacy barrier |
| `PreToolUse` | Deny child/repeated waits; allow one root wait and force `3600000ms` |
| `Stop` | Return immediately; deny final while workers remain |
| `SessionEnd` | Remove transient state for a closed parent session |
| Opaque audit | Retain bounded, hashed lifecycle outcomes under `PLUGIN_DATA/audit` |
| Bundled skill | Teach the root reconciliation and no-polling workflow |

## State machine

Each session uses a SHA-256 filename beneath `${PLUGIN_DATA}/sessions`. Its
state contains active workers, pending dispatch capabilities keyed by hashed
tool-call id, exact-target interrupt capabilities, `event_epoch`,
`wait_issued_epoch`, the hashed in-flight wait call, and
`stop_continuation_epoch`. A fresh ledger, when required for a normal wait,
contains only hashes of declared lane IDs, gates, dependencies, blockers, and
active dispatch mappings plus normalized policy fields. A pending capability is active for `wait_agent` and
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
| Non-timeout wait return | Match the in-flight tool-call id; increment epoch if work remains | One root wait after reconciling returned messages/results |
| Wait timeout/failure | Clear the in-flight tool-call id without changing epoch | No retry or polling wait |
| Worker completion | Remove worker or unpaired pending; increment epoch | One wait if work remains |
| Interrupt request | Bind tool-call id to one exact worker/pending target | Await native response; no lifecycle epoch yet |
| Confirmed interrupt result | Remove only the bound target; increment epoch | One wait if other work remains |
| User steering | Increment epoch | One wait after reconciliation |
| New dispatch | Add worker; increment epoch | One wait |
| First normal root wait | Derive active count, validate capacity, lanes, exact gates, and one-to-one mappings from observed spawn metadata | One normal wait |
| First active-worker Stop | Increment epoch; mark continuation | One wait from immediate continuation |
| Repeated active-worker Stop | No transition; `continue:false` | Explicit recovery, no loop |
| Zero-worker Stop | Remove session/recovery state | Final may pass |
| Strict v7 SessionStart | Preserve legacy work as unobserved metadata; clear stale wait/Stop capabilities; increment epoch | One ledger-bypass recovery wait, no conflicting dispatch/final |
| Unsafe legacy SessionStart | Content-addressed quarantine plus valid current recovery barrier | One recovery wait or exact operator confirmation after native-empty diagnosis |
| Missing-state SessionStart | Persist an owned mode-`0600` empty current state under the authoritative lock | Normal bounded dispatch |
| Malformed current state | Preserve bytes in current-corruption quarantine; create a distinct valid barrier | Fail closed; one guarded wait or exact operator confirmation after native-empty diagnosis |
| Session-bound operator confirmation | Verify exact current payload session hash and legacy barrier under lock; reset to empty current state | Normal bounded spawn and hook-owned ledger |

Ledger state is invalidated by every epoch advance, including steering, worker
completion/error, spawn failure, and confirmed interruption. The first normal
root wait for the new epoch rebuilds it only from still-tracked root spawn
capabilities; it has no external registrar or caller-provided session identity.
Completion removes
the worker and releases its dispatch record; it does not mark a dependency
accepted. Duplicate `SubagentStart`, unrelated `SubagentStop`, duplicate wait returns,
and untracked or mismatched interrupt results do not create a new
authorization. File locking serializes concurrent command-hook processes.
Audit records contain only epoch/outcome/event plus SHA-256 identities for the
session, turn, tool call, and agent when present; they retain at most 128
records per session, 256 session files, and 14 days.

## Lifecycle identity and upgrade boundary

The authoritative parent identity for every root hook is the payload
`session_id`. `SessionStart`, root tool hooks, `UserPromptSubmit`, `Stop`, and
`SessionEnd` all hash that exact field into the same state/lock prefix.
Subagent events also carry the parent lifecycle `session_id`; their `turn_id`
and `agent_id` are correlation data only. Environment thread identifiers are
not consulted. If a client/WSL restart creates a new lifecycle `session_id`,
the new identity gets isolated empty state and the old hashed state is neither
discovered globally nor deleted.

| Hook boundary | Authoritative state identity | Correlation-only fields |
| --- | --- | --- |
| `SessionStart` | payload `session_id` | `source` selects startup/resume/clear/compact behavior |
| root `PreToolUse`/`PostToolUse` for spawn, wait, interrupt | payload `session_id` | `tool_use_id`, `turn_id`, exact interrupt target |
| `UserPromptSubmit` | payload `session_id` | `turn_id`; exact bounded operator line when present |
| `Stop`/`SessionEnd` | payload `session_id` | `stop_hook_active` only controls response shape |
| `SubagentStart`/`SubagentStop` | payload parent `session_id` | child `agent_id`, `agent_type`, `turn_id` |
| resumed thread after client/WSL restart | new hook payload `session_id` | no environment-variable alias or global state discovery |

Legacy recovery is entered only from `SessionStart`, under the authoritative
session lock. Strict v7 worker and pending identities can be copied into the
current schema with explicitly unobserved dispatch metadata. No normal ledger
is built for that recovery barrier. Unsafe legacy bytes are hard-linked into a
content-addressed quarantine before the old state name is unlinked, retaining
at least one mode-`0600` copy across interruption and making duplicate recovery
idempotent. A collision with different bytes fails closed and preserves both
the original state name and existing quarantine object.

Safely readable malformed current-version bytes follow the same durable copy
sequence but use a distinct `quarantined-current` barrier. They are never
migrated, interpreted as workers, or considered empty. Unsafe filesystem
objects cannot enter either quarantine path.

Legacy barriers use one recovery wait per event epoch. Wait returns, ordinary
steering, and `Stop` do not manufacture another recovery epoch; an actual
tracked completion may. When native `list_agents` is empty, the operator may
submit the exact recovery command emitted by `SessionStart`. The command
contains the current session hash but no selectable path/session parameter, so
`UserPromptSubmit` can affect only its own payload identity.

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

## Hook-owned ledger and metadata boundary

The only callable registration surface that a newly activated Codex session
actually has is the root's normal `spawn_agent` call. Each root dispatch
therefore declares `LEDGER_LANE`, `LEDGER_SLOT_CAPACITY`, and a shared
`LEDGER_CURRENT_ACTIVE` in addition to its fixed coordination metadata. On the
first normal `wait_agent`, the hook binds those declarations to its
same-session pending/active dispatch capabilities, verifies the declared count
against the observed count, and retains only hashes. After a verified ledger,
lifecycle events refresh only the surviving bound count inside the hook for the
next epoch. A caller cannot provide a session id, filesystem path, or
registration capability.

Every tracked active/pending native dispatch must bind to exactly one active
lane. The hook rejects missing coverage, multiple lane mappings for one
dispatch, phantom mappings, and count-only coverage. `exclusive-gate` lanes
use exact, normalized strings. The taxonomy includes
`git-ref:origin/<branch>` (including `qa`, `develop`, and `main`),
`github-pr:<repo>:<head>:<base>`, `qa-deploy:<environment>`, and
`production-deploy:<service>`. Duplicate active/planned exact gates are denied;
different ref strings such as QA and develop do not conflict.

The hook rejects a root batch whose declared capacities disagree, whose active
work exceeds its declared capacity, or whose exact active exclusive gates
conflict. Deferral/dependency planning remains outside the hook protocol: only
already-dispatched native work is authorization-relevant.

For native spawn payloads, the hook can inspect `model`, `fork_turns`, and fixed
coordination headers in the spawn message. It enforces explicit Terra for
non-UI, rejects `fork_turns=all`, and narrowly permits Sol. Current unpaired
native lifecycle events do not expose a parent spawn payload; the hook does not
invent missing metadata and records that verification boundary instead.

Fixed spawn declarations are validated before lifecycle state is read or
mutated. A missing or invalid declaration produces a specific input-contract
denial naming the first invalid field. The root may correct and retry that
dispatch once; the denial neither creates pending work nor enters generic
state recovery.

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
