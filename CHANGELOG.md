# Changelog

All notable changes to this project are documented here.

## Unreleased

- Reconcile native `interrupt_agent` responses whose completed prior status is
  encoded as a single-key tagged object carrying the worker result.
- Replace the visible V2 declaration with `cceg2_` and require a bounded,
  session-bound batch ID, position, and size. `wait_agent` now fails closed
  until every coordinator-declared position is dispatched; an incomplete
  intent cannot be replaced, while complete one-worker batches and steering/
  recovery re-arms remain valid. Document that omitted task semantics remain
  unenforceable at the hook boundary.
- Pin the exact loaded hook runtime under `PLUGIN_DATA` and execute it through
  a digest-bound loader, allowing live sessions to reconcile worker completion
  and final Stop after their installed cache snapshot is removed.
- Instruct coordinators never to close their own Codex/Orca terminal in
  response to a hook failure.
- Convert active current v13 state crossing a startup, resume, or clear
  boundary into a native-reconciliation barrier. Coordinators now inspect
  `list_agents` before any recovery wait and repair root-only stale state
  without waiting; compacted live turns remain on the normal lifecycle path.
- Reject duplicate lanes, live-batch capacity changes, and over-capacity
  follow-up dispatches before state mutation, preventing invalid native work
  from starting or stranding later lifecycle hooks.
- Support Codex Multi-Agent V2's encrypted `spawn_agent.message` boundary with
  a strict visible `cceg2_` task-name capability for non-secret coordination
  metadata; retain plaintext fixed headers for V1.
- Fail closed before state mutation for encrypted V2 dispatches with a missing
  or malformed capability, mixed metadata surfaces, or policies whose exact
  gate/evidence value is not visible to the local hook.
- Persist a valid empty v13 state during a brand-new `SessionStart` instead of
  leaving only its lock file.
- Report missing or invalid root spawn declarations as correctable
  input-contract denials instead of generic lifecycle-state corruption.
- Recover strict v7 state on a new `SessionStart` by preserving lifecycle
  identities as explicitly unobserved ledger metadata and authorizing one
  bounded ledger-bypass wait.
- Quarantine unsafe legacy and malformed current-version bytes atomically
  behind distinct fail-closed recovery barriers.
- Add an idempotent operator-confirmed empty-native repair bound only to the
  hook payload `session_id`, never `CODEX_THREAD_ID` or a caller-selected path.
- Require owned mode-`0600` runtime objects and cover upgrade identity,
  consumed recovery, lock contention, interrupted writes, and quarantine
  collisions deterministically.
- Replace the uncallable external ledger script with a hook-owned protocol:
  root `spawn_agent` metadata binds a fresh session ledger during the first
  normal `wait_agent`, without exposing `PLUGIN_DATA` or accepting a caller
  session ID.
- Require a one-to-one mapping between every tracked active/pending native
  dispatch and active ledger lane; reject missing, duplicate, phantom, and
  count-only coverage while deriving active count from observed dispatches.
- Enforce exact exclusive-gate conflicts, lane capacity, observed spawn model
  policy, and epoch invalidation after steering/completion.
- Reconcile successful `interrupt_agent` calls against one exact tracked
  target, without relying on `SubagentStop` or affecting sibling workers.
- Re-arm one wait after a correlated non-timeout `wait_agent` return so
  intermediate worker messages cannot strand the parent turn.
- Add deterministic regressions for two-worker interruption, duplicate stop,
  intermediate-message re-arm, and timeout no-polling behavior.
- Document the required plain-shell reinstall boundary so an active Codex
  session cannot lose the cache path of its loaded hook snapshot.

## 0.2.0

- Introduce session-bound parallelism-ledger enforcement.

## 0.1.0

Initial public-source preparation.

- Event-driven native Codex subagent lifecycle gate.
- One authorized, interruptible native wait subscription per lifecycle event.
- FIFO correlation fallback for Codex v0.146.0 lifecycle payloads.
- Portable deterministic tests, GitHub Actions CI, and local installation
  guidance.
