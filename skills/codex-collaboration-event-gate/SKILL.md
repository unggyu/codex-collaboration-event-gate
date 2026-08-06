---
name: codex-collaboration-event-gate
description: Coordinate native Codex subagents with the installed event-driven Stop gate. Use for bounded multi-agent work where a root coordinator must retain its current turn until workers finish, avoid wait_agent polling, reconcile native FINAL/error results, or explain plugin hook trust and new-session activation.
---

# Codex Collaboration Event Gate

Use bounded native Codex workers from the root coordinator. Tell every worker
not to spawn workers, give it explicit scope and completion criteria, and let
independent tasks run concurrently.

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

Treat worker completion as an event, not proof that the requested outcome is
complete. Never promise a passive wake after the parent turn has ended.

This design spends one model/tool transition to arm each event subscription in
exchange for keeping the composer responsive. Hooks can authorize a new wait
after dispatch, completion, a correlated non-timeout wait return, a confirmed
interruption, or `UserPromptSubmit` steering, but Codex exposes no hook API that
can invoke `wait_agent` automatically; the root must make that tool call.

Before updating, exit every Codex session that loaded the current snapshot and
run the reinstall from a plain shell. A live session retains its resolved cache
path, which the reinstall may remove. After install or update, start a new
Codex session, open `/hooks`, review the plugin source and exact command hashes,
and trust them; an already-running session is not an activation test. Keep only
one active copy: use either the installed plugin or a project-local vendored
copy, never both.
