---
name: codex-collaboration-event-gate
description: Coordinate native Codex subagents with the installed event-driven Stop gate. Use for bounded multi-agent work where a root coordinator must retain its current turn until workers finish, avoid wait_agent polling, reconcile native FINAL/error results, or explain plugin hook trust and new-session activation.
---

# Codex Collaboration Event Gate

Use bounded native Codex workers from the root coordinator. Tell every worker
not to spawn workers, give it explicit scope and completion criteria, and let
independent tasks run concurrently.

Every root `spawn_agent` call must declare its ledger lane, shared capacity,
final active count, batch ID, batch position, batch size, classification, and
parallelism class. A visible batch intent is complete only when every position
from `1` through its declared batch size has passed `spawn_agent` in the same
session; otherwise `wait_agent` fails closed. Do not use a script, session ID,
or `PLUGIN_DATA` registrar. Missing, duplicate, phantom, count-only, or
incomplete batch coverage fails closed.

Shared capacity is immutable while any dispatch remains tracked. An initial
two-worker batch uses capacity/final-active/batch-size `2`, a common batch ID,
and positions `1` and `2` on its two back-to-back spawns. A valid one-worker
batch uses all three values as `1`, with position `1`. A later follow-up after
a verified wait must declare exactly the currently free slots as a new batch;
it cannot replace an incomplete intent. The hook denies duplicate lanes, live
capacity changes, duplicate/missing batch positions, and over-capacity
follow-ups before state mutation.

Codex Multi-Agent V2 encrypts `message` before `PreToolUse`, so put the fixed
non-secret declaration in the visible `task_name` field using exactly:

```text
cceg2_<n|u>_<r|w>_<lane>_<capacity>_<final_active>_<batch>_<position>_<size>_<d|h|u>
```

Use `n` for non-UI or `u` for UI; `r` for read-only or `w` for
isolated-write; unique 1-32 character lowercase alphanumeric lane and batch
IDs; decimal capacity and size from 1-64; and `d` for default policy, `h` for
novel high-complexity UI, or final `u` for an explicit user-requested Sol
override. `final_active` must equal capacity and position is in `1..size`.
For example, a single non-UI read-only Terra worker uses
`task_name: cceg2_n_r_isolatedsmoke_1_1_smoke1_1_1_d`. Keep the actual task
only in the encrypted `message`, use explicit `model: gpt-5.6-terra`, and use
`fork_turns: none`.

The encrypted V2 capability deliberately does not support `exclusive-gate` or
`terra-blocked:<evidence>`: the hook cannot validate their exact plaintext
values at this boundary and fails closed. Use a non-exclusive bounded worker,
or a plaintext V1 surface when exact gate metadata is required.

On a plaintext V1 spawn surface only, put `CLASSIFICATION`,
`PARALLELISM_CLASS`, `LEDGER_LANE`, `LEDGER_SLOT_CAPACITY`, and
`LEDGER_CURRENT_ACTIVE`, `LEDGER_BATCH_ID`, `LEDGER_BATCH_POSITION`, and
`LEDGER_BATCH_SIZE` as literal lines at the start of the worker message;
add `EXCLUSIVE_GATE` for an exclusive lane. Non-UI workers use explicit
`gpt-5.6-terra`; never use `fork_turns=all`. Use Sol only for explicit novel
high-complexity UI or `SOL_OVERRIDE_REASON: user-requested` or
`terra-blocked:<specific evidence>`. Do not combine plaintext headers with a
`cceg2_` task capability. If the spawn hook names a missing or invalid
declaration, correct that exact input and retry the same dispatch once. This
bounded correction is not a wait retry and does not authorize recovery.

After dispatch, call `wait_agent` exactly once. `PreToolUse` rewrites it to
`3600000ms`; the native subscription consumes no model polling turns and
remains interruptible by steered user input. When one worker completes or the
wait returns for an intermediate message, or the user steers, reconcile that
event before arming the one newly authorized wait. Never use short, repeated,
or polling waits. A timeout does not authorize a retry.

If a worker must be interrupted, call `interrupt_agent` for its exact native id
or canonical task name. The gate removes only that bound target after the
matching successful tool response; reconcile the interruption before waiting
for any workers that remain.

`Stop` never waits. If final is attempted with active workers, it returns
immediately with one continuation that instructs the root to arm the authorized
wait; a repeated final attempt fails closed without another continuation.
Use `list_agents` only for one-shot diagnosis or recovery reconciliation.
After a hook failure, use one recovery wait only when the hook explicitly says
it was authorized.

On upgrade/resume, treat the hook payload `session_id` as authoritative; never
derive state from `CODEX_THREAD_ID`. `SessionStart` may preserve strict legacy
workers, preserve uncertain current v13 work behind a `resumed-current`
barrier, or quarantine unsafe legacy bytes. Before any recovery wait, run
one-shot `list_agents`. If it shows `/root` alone, do not wait; the operator may
submit the exact
`/collaboration-recover-empty <current-session-sha256>
confirm-native-root-only` command printed by `SessionStart`. Never construct a
hash or command yourself. Consume the reported recovery wait only when
`list_agents` shows real native children. A malformed current-version state
remains fail-closed behind a distinct quarantine barrier and requires the
same explicit native-empty operator confirmation.

Treat worker completion as an event, not proof that the requested outcome is
complete. Never promise a passive wake after the parent turn has ended.

The hook can prove only that all positions explicitly declared by the
coordinator were dispatched before the wait. It cannot inspect encrypted task
content or infer an omitted independent lane; enumerate every known eligible
lane in the batch intent before dispatching.

Never close, stop, or replace the host Codex/Orca terminal to escape a hook
error. Report the exact failing hook and preserve the session for the operator.
Do not create a symlink from a missing old cache path to a newer snapshot. A
current release pins its digest-bound hook runtime under `PLUGIN_DATA`, but an
older session that reports a missing hook executable still requires an
operator-controlled session transition.

This design spends one model/tool transition to arm each event subscription in
exchange for keeping the composer responsive. Hooks can authorize a new wait
after dispatch, completion, a correlated non-timeout wait return, a confirmed
interruption, or `UserPromptSubmit` steering, but Codex exposes no hook API that
can invoke `wait_agent` automatically; the root must make that tool call.

Before updating, exit every Codex session that loaded the current snapshot and
run the reinstall from a plain shell. A live session retains its resolved cache
path and trusted definitions. The private digest-bound runtime pin is a safety
net for unexpected cache removal, not permission to hot-update a live session.
After install or update, start a new Codex session, open `/hooks`, review the
plugin source and exact command hashes, and trust them; an already-running
session is not an activation test. Keep only one active copy: use either the
installed plugin or a project-local vendored copy, never both.
