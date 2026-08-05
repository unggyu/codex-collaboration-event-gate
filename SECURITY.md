# Security policy

## Reporting a vulnerability

Do not include exploit details in a public issue. Use the private reporting
channel associated with the repository once it is published. Until then, keep
the report private with the maintainer who supplied the checkout.

Please include the affected plugin version, Codex version, platform, a minimal
reproduction, and whether the issue requires control of the same operating
system account.

## Security model

The hook treats lifecycle payloads and plugin runtime storage as untrusted. It
uses private plugin-data directories, regular-file checks, atomic writes,
file locks, schema validation, hashed audit identifiers, and fail-closed
decisions when active-worker state cannot be established.

The plugin does not read project sources, transcripts, prompts, worker output,
Git metadata, or network services. It does not send telemetry. Runtime state
is local and bounded; see [Architecture](docs/ARCHITECTURE.md) for retention
details.

## Supported environment

The current implementation supports Linux and macOS with Python 3 and POSIX
`fcntl` file locking. Windows is not supported. Hook definitions are local
executable code: review them in Codex `/hooks` and trust only the installed
snapshot you intend to run.
