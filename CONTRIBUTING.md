# Contributing

Thanks for improving the event gate.

## Before opening a change

1. Keep the change focused on this plugin. Do not include local marketplace,
   Codex configuration, runtime data, hook-trust records, or session logs.
2. Run the self-contained test suite:

   ```bash
   bash scripts/run-tests.sh
   ```

3. If the plugin payload changed, also run the official plugin-creator
   validator in an environment where that developer tool is installed.
4. Explain any lifecycle ordering assumption and add a deterministic regression
   case for state-machine changes.

## Pull request expectations

Describe the observable behavior change, security implications, and test
evidence. Runtime hook changes require maintainers to review and re-trust their
local hook definitions after reinstalling; they do not automatically activate
in existing Codex sessions. Exit every session that loaded the old snapshot
before reinstalling from a plain shell: a live session retains its resolved
cache path, which the reinstall may replace.

## Release discipline

Use strict semantic versions in `.codex-plugin/plugin.json`. For a local
snapshot refresh, preserve the base version and use the official
`update_plugin_cachebuster.py` helper; do not hand-edit marketplace or Codex
configuration files.
