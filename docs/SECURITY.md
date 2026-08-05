# Security Notes

## Fail-closed policy

Unverifiable active-worker state never becomes "zero workers." Corrupt state,
invalid schemas, unsafe file types, missing session identity, or unavailable
plugin data deny normal waits and prevent final. A lifecycle/Stop failure may
authorize exactly one separate root recovery wait, atomically consumed and
forced to `3600000ms`. A second automatic `Stop` continuation is suppressed
with `continue:false`.

## Filesystem controls

- Raw session, worker, cwd, and transcript values never become path components.
- Session filenames are SHA-256 digests of opaque session IDs.
- Plugin data and session directories are opened with `O_NOFOLLOW` and mode
  `0700`.
- Lock/state files use mode `0600`.
- State and recovery JSON writes use same-directory temporary files, `fsync`,
  and atomic replacement.
- State, recovery, audit, and lock objects must be regular non-symlink files.
- JSON schemas and worker records are validated before use.
- `fcntl.flock` serializes concurrent hook processes and makes each wait
  authorization single-consumer.

Static symlink substitution is rejected and tested. A hostile process running
as the same operating-system user can still race or modify files that user owns;
protecting against a fully compromised user account is outside this plugin's
threat model.

## Data minimization

State stores a hashed session identity, opaque worker IDs, agent types, turn
IDs, timestamps, authorization epochs, and pending dispatch capabilities keyed
by a hashed native tool-call ID. Audit records hold only hashed session/turn/
tool/agent identities, event kind, epoch, timestamp, and outcome; they contain
no prompt, tool input, tool output, cwd, transcript, or secret. Retention is
bounded to 128 records per session, 256 audit files, and 14 days. The runtime
does not read transcripts, worker output, prompts, source files, Git metadata,
or network services.

## Hook trust

Command hooks are executable local code. Codex requires trust for their exact
definitions and hashes. Review `hooks/hooks.json`, the resolved source path,
and the Python file in `/hooks`. Any update changes the trust surface and
requires review in a new session.

## Duplicate-source hazard

Codex launches matching hooks from all active sources concurrently. Two copies
can race because neither can prevent the other from starting. Use only one of:

- the installed personal plugin; or
- one project-local vendored copy.

The vendoring helper refuses unrelated project hook configuration, checks
reported Codex installation state when available, and requires explicit
acknowledgement that the plugin is disabled. This acknowledgement cannot prove
external state when the Codex CLI is unavailable; verify `/hooks` before use.

## Platform boundary

This release requires POSIX `fcntl` and is intended for Linux and macOS. It
has no Windows locking implementation. The runtime requires a writable,
absolute `${PLUGIN_DATA}` provided by Codex (or by the project-local runner).
