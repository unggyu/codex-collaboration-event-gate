# Codex Collaboration Event Gate

[![CI](https://github.com/unggyu/codex-collaboration-event-gate/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/unggyu/codex-collaboration-event-gate/actions/workflows/ci.yml)

`codex-collaboration-event-gate` is a local Codex plugin for coordinating
native subagents without turning the parent into a polling loop. It keeps the
parent turn interactive while workers run, authorizes one native completion
subscription at a time, and prevents a final response while tracked work
remains.

This is executable hook code. Review it before trusting it.

Source: <https://github.com/unggyu/codex-collaboration-event-gate>

## What it guarantees

- Records native dispatches before `SubagentStart`, closing the observed
  dispatch-to-lifecycle delivery gap.
- Rewrites the one authorized root `wait_agent` timeout to `3600000` ms. The
  native subscription is interruptible by a new user message.
- Requires a fresh, session-bound coordination ledger before every normal wait.
  It checks declared lanes, capacity, complete visible batch intent, dispatch
  coverage, and exact exclusive-gate conflicts before the parent can wait.
- Re-arms exactly one subscription after a worker completion, a non-timeout
  wait return (including an intermediate worker message), a new dispatch, or
  user steering while work remains.
- Reconciles a successful `interrupt_agent` only when its Pre/Post tool-call
  identity and exact worker or pending-dispatch target match. Other workers are
  never removed by FIFO fallback during interruption.
- Makes `Stop` immediate and nonblocking. It denies a final while a worker or
  pending dispatch is tracked, without holding the composer hostage.
- Pins the exact loaded hook runtime by SHA-256 under `PLUGIN_DATA` on the
  first lifecycle event. A session can still reconcile `SubagentStop` and
  `Stop` if a later reinstall removes its versioned plugin cache directory.
- Rejects repeated waits, child waits, recursive worker dispatches, malformed
  state, and duplicate lifecycle events safely.
- Stores bounded, local, hashed lifecycle metadata under `PLUGIN_DATA`.

## What it does not do

- It cannot invoke `wait_agent` itself. The coordinator must call it after a
  dispatch and after each newly authorized lifecycle or non-timeout wait event.
- It does not poll, sleep, watch files, use FIFOs, inspect transcripts or
  project source, contact a network service, or create a post-final wake-up.
- It does not decide whether a worker result satisfies user acceptance
  criteria; the parent must reconcile results before responding.
- It does not infer lanes from a transcript or encrypted task content. Every
  root dispatch must carry the fixed ledger and batch-intent headers;
  incomplete, duplicate, phantom, or count-only coverage fails a normal wait
  closed. It can prove only all coordinator-declared positions dispatched, not
  an independent lane the coordinator failed to declare.
- It does not support Windows in this release; the state lock uses POSIX
  `fcntl`.

## Architecture

```text
root dispatch ──PreToolUse──> pending dispatch ──SubagentStart──> active worker
      │                              │                                  │
      └─ hook-owned ledger ─ one wait_agent <──── worker/steering event ───┘
                                  (ledger invalidated; wait is 3600000 ms)

root final ──Stop──> immediate denial while work remains; release when empty
```

The hook uses default plugin discovery at `hooks/hooks.json`. Its loaded
command validates the `${PLUGIN_ROOT}` hook by SHA-256, then creates and
prefers a private content-addressed runtime pin under `${PLUGIN_DATA}`. All
session state also remains under `${PLUGIN_DATA}`.
The detailed state machine and threat model are in
[Architecture](docs/ARCHITECTURE.md) and [Security](SECURITY.md).

### Codex v0.146.0 correlation limitation

Current native lifecycle payloads identify a child session/turn but do not
carry the parent `spawn_agent` tool-call identity. The plugin therefore pairs
accepted same-session dispatches with `SubagentStart`/missing-start
`SubagentStop` events in deterministic FIFO order. It is deliberately scoped
to bounded, non-recursive workers. If a future Codex version exposes a direct
parent-call correlation ID, the pairing strategy should be revisited.

## Coordination ledger

There is no external registrar: Codex plugins expose no manifest command/tool
surface that can safely receive the hook-only `PLUGIN_DATA`. The callable
new-session protocol is ordinary root `spawn_agent` followed by `wait_agent`.
Codex Multi-Agent V2 encrypts `spawn_agent.message` before the blocking hook
sees it. V2 callers therefore place the fixed non-secret coordination
declaration in the visible task name:

```text
cceg2_<n|u>_<r|w>_<lane>_<capacity>_<final_active>_<batch>_<position>_<size>_<d|h|u>
```

The codes mean non-UI/UI, read-only/isolated-write, unique lowercase
alphanumeric lane and batch IDs of at most 32 characters, bounded decimal
capacity/final-active/position/size, and default/novel-high-UI/user-requested-
Sol policy. Final active must equal capacity and position is in `1..size`. A
one-worker read-only example is `cceg2_n_r_isolatedsmoke_1_1_smoke1_1_1_d`.
The native `model` and `fork_turns` fields remain explicit. Plaintext V1
callers retain the fixed message-header form. On the first normal root
`wait_agent`, `PreToolUse` constructs the fresh ledger atomically from those
same-session pending/active dispatch capabilities and verifies that every
position in the current batch intent was dispatched; no caller supplies a
session identity or filesystem capability.

Every tracked active or pending dispatch must map to exactly one declared lane.
Missing, duplicate, and phantom mappings fail closed. Exact active
`exclusive-gate` strings must not repeat. Deferral remains a coordinator policy
outside the hook protocol; this gate only authorizes the bounded dispatched
batch it can verify.

Shared capacity is fixed while any dispatch from a batch remains tracked. An
initial two-worker batch declares capacity/final-active/batch-size `2`, one
common batch ID, and positions `1` and `2` on its two back-to-back spawns.
`wait_agent` is denied until both positions are recorded. A later follow-up
after a verified wait must declare exactly the currently free slots as a new
complete batch. Attempts to reuse a tracked lane, replace an incomplete
intent, widen a live batch, omit/duplicate a position, or exceed capacity are
denied before state mutation.

Gate strings are exact. Supported contracts include `git-ref:origin/qa`,
`git-ref:origin/develop`, `git-ref:origin/main`,
`github-pr:<repo>:<head>:<base>`, `qa-deploy:<environment>`, and
`production-deploy:<service>`. Two planned/active QA ref writers conflict; QA
and develop ref writers do not.

For plaintext V1, the hook parses only fixed headers: `CLASSIFICATION`,
`PARALLELISM_CLASS`, `EXCLUSIVE_GATE`, `NOVEL_UI_COMPLEXITY`,
`SOL_OVERRIDE_REASON`, `LEDGER_LANE`, `LEDGER_SLOT_CAPACITY`,
`LEDGER_CURRENT_ACTIVE`, `LEDGER_BATCH_ID`, `LEDGER_BATCH_POSITION`, and
`LEDGER_BATCH_SIZE`. For encrypted V2, it parses only the strict `cceg2_`
task-name capability and rejects an opaque message without one. V2 capabilities
support read-only and isolated-write work; exact exclusive gates and
`terra-blocked:<specific evidence>` fail closed because the hook cannot verify
their encrypted plaintext values. Non-UI still requires explicit
`gpt-5.6-terra`; `fork_turns=all` is rejected; Sol requires the supported
novel-high-UI or user-requested capability. Mixing the two metadata surfaces is
rejected. Missing or invalid declarations are reported as a bounded
input-contract denial naming the first invalid field. The root may correct that
declaration and retry the same dispatch once; it is not reported as
lifecycle-state corruption and does not authorize a recovery wait.
Unpaired lifecycle events have no spawn metadata, so the hook records that
boundary instead of inventing a cross-check.

## Upgrade and crash recovery

Every lifecycle file is selected only from the hook payload's `session_id`.
That field is authoritative for `SessionStart`, root `PreToolUse` and
`PostToolUse`, `UserPromptSubmit`, `Stop`, and `SessionEnd`. `turn_id`, native
tool-call IDs, and agent IDs correlate events inside that session; they never
replace its identity. The runtime does not use `CODEX_THREAD_ID`. A resumed
thread may receive a different lifecycle `session_id`, in which case it gets a
separate hash/lock and the prior identity's state is left untouched.

On `SessionStart`, a strict v7 state is migrated atomically to v13. Worker,
pending-dispatch, interrupt, timestamp, target-hash, sequence, and epoch data
are preserved. Missing ledger fields are marked unobserved; the plugin does
not synthesize lanes, models, counts, dispatch mappings, or completion events.
While migrated work remains, conflicting dispatch and final are blocked and
one ledger-bypass recovery wait is available after native workers are confirmed.
Only a real tracked completion creates another recovery epoch.

A valid v13 state with active work also becomes uncertain when it crosses a
`startup`, `resume`, or `clear` boundary: the native registry may no longer
contain the workers whose completion events were missed. `SessionStart`
preserves those records but clears their stale ledger, wait, interrupt, and
Stop capabilities and creates a `resumed-current` recovery barrier. It does not
perform this conversion for `compact`, where native workers can still be live
inside the same lifecycle. The preserved file is evidence to reconcile, not
proof that a native child still exists.

If a legacy file cannot be migrated without guessing, its original bytes are
moved to a mode-`0600`, content-addressed quarantine under `PLUGIN_DATA` and a
valid v13 recovery barrier replaces it. A malformed v13 state is different: it
is never migrated as legacy or treated as empty. Its bytes are quarantined
behind a distinct current-corruption barrier and it remains fail-closed until
the same explicit native-empty operator confirmation. Missing state remains
the normal empty-session case: `SessionStart` creates and atomically persists
an owned mode-`0600` empty v13 state under the authoritative session lock
before reporting the gate ready.

The recovery barrier reports the SHA-256 identity of the current hook payload
and an exact command of this form:

```text
/collaboration-recover-empty <current-session-sha256> confirm-native-root-only
```

Run one-shot `list_agents` before any recovery wait. If it shows `/root` and no
native children, do not call `wait_agent`; submit the reported command. Only if
real native children are listed should the coordinator consume the single
recovery wait. `UserPromptSubmit` binds the command to its own payload
`session_id`, takes that session's lock, and resets only a validated recovery
barrier. It has no path or arbitrary session argument, is idempotent, and can
reset legacy, resumed-current, or current-corruption barriers only after the
same explicit native-empty confirmation.
The hook cannot independently call `list_agents`; the exact confirmation is an
operator attestation. General steering and repeated `Stop` events do not mint
additional recovery waits.

For a one-time stranded-session recovery after installing a fixed snapshot:

1. Start or resume the affected thread and read its `SessionStart` recovery
   message. Do not derive a filename from `CODEX_THREAD_ID`.
2. Run one-shot `list_agents` before calling `wait_agent`.
3. If and only if it lists `/root` alone, do not wait; submit the exact
   operator command printed by that `SessionStart` message.
4. If real native children are listed, consume the one recovery wait and
   reconcile their native completion events; repeat the diagnosis only after
   that event returns.
5. Dispatch one bounded worker and verify normal ledger registration, the
   `3600000ms` wait rewrite, completion, and final release.

## Requirements

- A Codex client that supports plugins and the listed lifecycle hooks.
- Python 3 on Linux or macOS (standard library only; `fcntl` is required).
- A writable absolute `PLUGIN_DATA` supplied by Codex.
- Permission to review and trust local command hooks.

No package manager, network access, service account, or Python dependency is
required to run the hook or its test suite.

## Install from a standalone checkout

Codex installs plugins from a configured marketplace snapshot. For the default
personal marketplace, place a clone at:

```bash
mkdir -p "$HOME/plugins"
git clone https://github.com/unggyu/codex-collaboration-event-gate.git \
  "$HOME/plugins/codex-collaboration-event-gate"
```

The personal marketplace entry must name this plugin and use the standard
relative local source `./plugins/codex-collaboration-event-gate`. Create that
entry with the official Codex plugin tooling; do not copy personal marketplace
or configuration files from another machine.

Then install the marketplace snapshot:

```bash
codex plugin add codex-collaboration-event-gate@personal
```

After installing from a plain shell with every session that loaded the prior
snapshot closed:

1. Open `/hooks` in Codex.
2. Confirm there is exactly one collaboration-gate source and inspect the
   resolved command, source path, and timeout.
3. Review and trust the hook definitions.
4. Start a new Codex session. Existing sessions retain their loaded hooks.
5. Test a bounded worker dispatch, a steering message while its wait is active,
   one re-arm, worker completion, and final release.

Do not enable a project-local vendored copy at the same time. Matching Codex
hooks from two sources run concurrently and cannot cancel each other before
launch.

## Update a local checkout

Run the self-contained checks first:

```bash
bash scripts/run-tests.sh
```

Before reinstalling, exit every Codex session that loaded the previous plugin
snapshot. Run the reinstall command from a plain shell, not from a Codex
session using this plugin. Running sessions retain their old trusted hook
definitions and do not activate an update. This release's runtime pin lets an
already-started session finish safely if its cache is removed unexpectedly,
but it is not a hot-update mechanism. Older releases, or a cache deletion that
races the first lifecycle hook before its pin is durable, can still fail before
the hook starts.

When the plugin payload changes, use the official `plugin-creator` update flow
from an environment where that developer tool is available:

```bash
python3 <plugin-creator-root>/scripts/update_plugin_cachebuster.py .
python3 <plugin-creator-root>/scripts/read_marketplace_name.py
codex plugin add codex-collaboration-event-gate@<marketplace-name>
```

The helper changes only the Codex build metadata suffix while preserving the
semantic version. It is intentionally not part of CI. After the plain-shell
reinstall finishes, start a new session, reopen `/hooks`, confirm exactly one
collaboration-gate source, and review and trust the changed snapshot. Never
hand-edit the installed cache, marketplace
database, or Codex configuration to force an update.

## Test and validate

From the repository root:

```bash
bash scripts/run-tests.sh
git diff --check
```

The test suite uses only Bash and Python's standard library. It checks the
manifest, portable paths, syntax, atomic state behavior, FIFO lifecycle
correlation, ledger coverage/gates/model policy, wait rewriting, steering
invalidation, immediate Stop release, installed-cache removal after runtime
pinning, authoritative-session upgrade recovery, quarantine/operator repair,
and fail-closed storage handling.
GitHub Actions runs the same suite on a clean Ubuntu checkout.

Where the official `plugin-creator` skill is installed, also run:

```bash
python3 <plugin-creator-root>/scripts/validate_plugin.py .
```

## Security and privacy

The runtime reads hook payloads and writes private local state plus an exact
content-addressed copy of its own trusted hook code. It uses hashed identifiers
in bounded audit records and never retains prompts, tool inputs/outputs,
project paths, transcript contents, project source files, Git data, or
credentials. See [SECURITY.md](SECURITY.md) for disclosure guidance and
[Architecture](docs/ARCHITECTURE.md) for exact retention limits.

## Project-local vendoring

The installed plugin is the supported path. For an explicitly local-only
environment, `scripts/vendor-project-local.py` can install one owned project
copy after it confirms that the installed plugin is disabled. It refuses to
merge with unrelated hook configuration. This is an operational fallback, not
a way to run both sources together.

```bash
python3 scripts/vendor-project-local.py install \
  --project /absolute/path/to/project \
  --confirm-plugin-disabled
```

Use `update` or `uninstall` with the same `--project` argument to maintain the
owned copy. Re-review project hooks and start a new session after every change.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). This source
tree intentionally contains no personal marketplace data, trust database,
installed cache, runtime state, session record, or credentials. Hosting this
source repository does not publish the plugin through an official universal
plugin marketplace.
