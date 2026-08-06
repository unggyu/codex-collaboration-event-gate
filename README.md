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
- Re-arms exactly one subscription after a worker completion, a non-timeout
  wait return (including an intermediate worker message), a new dispatch, or
  user steering while work remains.
- Reconciles a successful `interrupt_agent` only when its Pre/Post tool-call
  identity and exact worker or pending-dispatch target match. Other workers are
  never removed by FIFO fallback during interruption.
- Makes `Stop` immediate and nonblocking. It denies a final while a worker or
  pending dispatch is tracked, without holding the composer hostage.
- Rejects repeated waits, child waits, recursive worker dispatches, malformed
  state, and duplicate lifecycle events safely.
- Stores only bounded, local, hashed lifecycle metadata under `PLUGIN_DATA`.

## What it does not do

- It cannot invoke `wait_agent` itself. The coordinator must call it after a
  dispatch and after each newly authorized lifecycle or non-timeout wait event.
- It does not poll, sleep, watch files, use FIFOs, inspect transcripts or
  project source, contact a network service, or create a post-final wake-up.
- It does not decide whether a worker result satisfies user acceptance
  criteria; the parent must reconcile results before responding.
- It does not support Windows in this release; the state lock uses POSIX
  `fcntl`.

## Architecture

```text
root dispatch ──PreToolUse──> pending dispatch ──SubagentStart──> active worker
      │                              │                                  │
      └── one wait_agent <───────────┴────────── worker/steering event ───┘
                                   (rewritten to 3600000 ms)

root final ──Stop──> immediate denial while work remains; release when empty
```

The hook uses default plugin discovery at `hooks/hooks.json`. Command paths
resolve through `${PLUGIN_ROOT}`; only runtime state uses `${PLUGIN_DATA}`.
The detailed state machine and threat model are in
[Architecture](docs/ARCHITECTURE.md) and [Security](SECURITY.md).

### Codex v0.146.0 correlation limitation

Current native lifecycle payloads identify a child session/turn but do not
carry the parent `spawn_agent` tool-call identity. The plugin therefore pairs
accepted same-session dispatches with `SubagentStart`/missing-start
`SubagentStop` events in deterministic FIFO order. It is deliberately scoped
to bounded, non-recursive workers. If a future Codex version exposes a direct
parent-call correlation ID, the pairing strategy should be revisited.

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

After installing:

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
session using this plugin. Running sessions retain the resolved cache path;
reinstalling while one is alive can remove that path and make its later hooks
fail before Python starts.

When the plugin payload changes, use the official `plugin-creator` update flow
from an environment where that developer tool is available:

```bash
python3 <plugin-creator-root>/scripts/update_plugin_cachebuster.py .
python3 <plugin-creator-root>/scripts/read_marketplace_name.py
codex plugin add codex-collaboration-event-gate@<marketplace-name>
```

The helper changes only the Codex build metadata suffix while preserving the
semantic version. It is intentionally not part of CI. After the plain-shell
reinstall finishes, start a new session, reopen `/hooks`, and review and trust
the changed snapshot. Never hand-edit the installed cache, marketplace
database, or Codex configuration to force an update.

## Test and validate

From the repository root:

```bash
bash scripts/run-tests.sh
git diff --check
```

The test suite uses only Bash and Python's standard library. It checks the
manifest, portable paths, syntax, atomic state behavior, FIFO lifecycle
correlation, wait rewriting, steering re-arm, immediate Stop release, and
fail-closed storage handling. GitHub Actions runs the same suite on a clean
Ubuntu checkout.

Where the official `plugin-creator` skill is installed, also run:

```bash
python3 <plugin-creator-root>/scripts/validate_plugin.py .
```

## Security and privacy

The runtime reads hook payloads and writes private local state only. It uses
hashed identifiers in bounded audit records and never retains prompts, tool
inputs/outputs, project paths, transcript contents, source files, Git data, or
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
