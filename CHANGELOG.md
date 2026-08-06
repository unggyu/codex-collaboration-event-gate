# Changelog

All notable changes to this project are documented here.

## Unreleased

- Reconcile successful `interrupt_agent` calls against one exact tracked
  target, without relying on `SubagentStop` or affecting sibling workers.
- Re-arm one wait after a correlated non-timeout `wait_agent` return so
  intermediate worker messages cannot strand the parent turn.
- Add deterministic regressions for two-worker interruption, duplicate stop,
  intermediate-message re-arm, and timeout no-polling behavior.
- Document the required plain-shell reinstall boundary so an active Codex
  session cannot lose the cache path of its loaded hook snapshot.

## 0.1.0

Initial public-source preparation.

- Event-driven native Codex subagent lifecycle gate.
- One authorized, interruptible native wait subscription per lifecycle event.
- FIFO correlation fallback for Codex v0.146.0 lifecycle payloads.
- Portable deterministic tests, GitHub Actions CI, and local installation
  guidance.
