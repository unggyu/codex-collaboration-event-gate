# Security Notes

## Fail-closed policy

Unverifiable active-worker state never becomes "zero workers." Corrupt state,
invalid schemas, unsafe file types, missing session identity, or unavailable
plugin data deny normal waits and prevent final. A lifecycle/Stop failure may
authorize exactly one separate root recovery wait, atomically consumed and
forced to `3600000ms`. A second automatic `Stop` continuation is suppressed
with `continue:false`.

Incompatible legacy state is not equivalent to malformed current state.
Strictly validated v7 records migrate with worker/pending identities preserved
and ledger metadata explicitly unobserved. Unsafe legacy bytes move to a
content-addressed quarantine and leave a current recovery barrier. Malformed
v12 state is never migrated or treated as empty; its bytes enter a distinct
current-corruption barrier that requires the same explicit native-empty
operator confirmation. A recovery barrier blocks final and conflicting
dispatch and does not let repeated steering/Stop/wait-return events create an
infinite recovery loop.

## Filesystem controls

- Raw session, worker, cwd, and transcript values never become path components.
- Session filenames are SHA-256 digests of opaque session IDs.
- Plugin data and session directories are opened with `O_NOFOLLOW`, require
  the current OS owner, and require exact mode `0700`.
- Lock/state/recovery/audit/quarantine files require the current OS owner and
  exact mode `0600`.
- State and recovery JSON writes use same-directory temporary files, `fsync`,
  and atomic replacement.
- State, recovery, audit, and lock objects must be regular non-symlink files.
- JSON schemas and worker records are validated before use.
- `fcntl.flock` serializes concurrent hook processes and makes each wait
  authorization single-consumer.

Legacy quarantine uses a SHA-256 content address. The owned state is linked to
the non-overwriting quarantine name and directory metadata is synced before
the old name is removed. An interrupted move retains at least one copy; a
same-name/different-content collision fails closed.

Static symlink substitution is rejected and tested. A hostile process running
as the same operating-system user can still race or modify files that user owns;
protecting against a fully compromised user account is outside this plugin's
threat model.

## Data minimization

State stores a hashed session identity, opaque worker IDs, agent types, turn
IDs, timestamps, authorization epochs, pending dispatch capabilities, exact
target hashes for interruption, a hashed in-flight wait call, and normalized
ledger fields. Lane IDs, exclusive gates, dependencies, external approval
blockers, and dispatch mappings are SHA-256 hashes; prompt and task content are
never retained. Native tool
call IDs and interruption targets are never stored in state verbatim. Audit
records hold only hashed session/turn/tool/agent identities, event kind, epoch,
timestamp, and outcome; they contain no prompt, tool input, tool output, cwd,
transcript, or secret. Retention is
bounded to 128 records per session, 256 audit files, and 14 days. The runtime
does not read transcripts, worker output, prompts, source files, Git metadata,
or network services.

The only prompt content inspected for recovery is an exact, bounded
`/collaboration-recover-empty <current-session-sha256>
confirm-native-root-only` control line delivered directly to
`UserPromptSubmit`; it is compared and never stored or audited. The command is
bound to the hook payload's `session_id` and has no arbitrary path or session
selector. It is accepted only for a validated legacy or current-corruption
recovery barrier. The
operator must first confirm one-shot `list_agents` shows `/root` alone because
the hook cannot call the native registry itself.

The hook never exposes `PLUGIN_DATA` or a registrar command to the model. It
constructs the ledger only from fixed metadata in root `spawn_agent` payloads
and same-session pending/active capabilities, derives active count from the
observed dispatches, and fails closed when a lane is missing, duplicate,
phantom, over capacity, or inconsistent with observed metadata. Unpaired
native lifecycle events have no parent spawn payload; that is an explicit
verification boundary, not a guessed policy assertion.

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
