# Repository development conventions

This repository packages a local Codex plugin. Keep it portable: source code,
tests, and CI must run from a clean POSIX checkout with Python 3 and standard
shell tools. Do not add dependencies on a particular home directory, Codex
install cache, marketplace database, active terminal, transcript, or plugin
runtime data.

## Working rules

- Preserve the event-driven contract: no polling loop, sleep loop, FIFO, or
  external watcher. The hook only records lifecycle state and authorizes one
  native `wait_agent` subscription.
- Keep `hooks/hooks.json` on default discovery and resolve commands with
  `${PLUGIN_ROOT}`. Runtime state belongs only below `${PLUGIN_DATA}`.
- Treat hook input as untrusted. Keep state-file validation, locking, and
  fail-closed behavior covered by deterministic tests.
- Do not commit `.codex` config, marketplace files, trusted-hook databases,
  plugin caches, runtime audit/state data, session identifiers, or test output.
- Run `bash scripts/run-tests.sh` before committing. It includes static
  portability checks, syntax checks, and deterministic lifecycle tests.
- When changing plugin payload files, validate with the official
  `plugin-creator` validator where it is available, then use its
  `update_plugin_cachebuster.py` helper before reinstalling a local snapshot.
  The helper and Codex CLI are developer tools, not CI dependencies.

## Scope

This project is intentionally local-first. Do not create a marketplace entry,
remote, release, hook trust record, or live Codex session as part of a normal
code change. Those actions require an explicit maintainer decision.
