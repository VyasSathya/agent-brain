# Hooks

`agent-brain install-hooks --claude --codex` writes these for you (with backups, without duplicating). To do it
by hand, merge one of these files into your settings and replace both `/abs/path/...` placeholders:

- Claude Code: `claude-settings.example.json` -> `~/.claude/settings.json`
- Codex: `codex-hooks.example.json` -> `~/.codex/hooks.json` (Codex asks you to trust new hooks once)

Both tools send the hook JSON on stdin and accept the same output: `hook-start` prints
`{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}`, and `hook-stop`
stores the final reply as unreviewed evidence. Neither command ever exits with an error, so a broken or
missing brain degrades to a short notice instead of blocking the session.

On Windows use the full path to `python.exe` if `python` is not on the PATH the agent's hook runner sees.
