#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

bash -n "$ROOT/scripts/run-tests.sh"
bash -n "$ROOT/scripts/test-stop-gate.sh"
bash -n "$ROOT/scripts/test-wait-agent-guard.sh"
python3 -m py_compile   "$ROOT/hooks/codex-collaboration-lifecycle.py"   "$ROOT/scripts/vendor-project-local.py"   "$ROOT/scripts/test-plugin-contract.py"
python3 "$ROOT/scripts/validate-plugin-manifest.py"
python3 "$ROOT/scripts/check-portability.py"
python3 "$ROOT/scripts/test-plugin-contract.py"
bash "$ROOT/scripts/test-stop-gate.sh"
bash "$ROOT/scripts/test-wait-agent-guard.sh"

printf 'PASS: all codex-collaboration-event-gate tests completed.\n'
