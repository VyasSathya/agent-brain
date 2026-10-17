---
name: agent-brain
description: Use when asked what was recently worked on, where something lives, prior decisions or handoffs, what is still remaining on a task, or when ending meaningful implementation work and a source-linked handoff should be saved. Operates the single shared agent-brain folder that Claude Code and Codex both use; do not create a second memory store.
---

# agent-brain

One memory shared by every coding agent on this machine. It answers: which project is this folder, what is the
current state of each task, what was checked, what is still open, and which source lines support that.

It is not a task manager, not a replacement for a repo's `AGENTS.md` / `CLAUDE.md` / `README`, and not a
transcript dump.

## Tool

```
agent-brain <command>          # or: python -m agent_brain <command>
```

The SessionStart hook prints the exact command line (with `--brain <dir>`) at the top of the injected context.

| Command | Use |
|---|---|
| `route --cwd <dir>` | Resolve a folder to project + role |
| `current --project <id>` | Current task heads (5 per page, `next_offset`, `evidence_changed`) |
| `search --project <id> --query "terms"` | Lexical search of that project's notes, evidence and handoffs |
| `read --project <id> --path <p> --line <n> --count 40` | Exact text at a returned cursor |
| `bind --project <id> --path <abs> --role task` | Register a new task folder for a known project |
| `handoff --file <json>` | Save a validated, source-linked handoff |
| `catchup --days 7` | Read-only overview of everything active |

`--project global` is the explicit opt-in for cross-project notes.

## Reading

Smallest relevant first.

1. SessionStart already injected the route and the startup files. Do not re-read them.
2. `current --project <id>`, then read the relevant handoffs and their cited sources. If `evidence_changed`
   is true, read the live file; it wins over the note.
3. The project notes (`projects/<id>.md` in the brain) and the repo's own `AGENTS.md` / `README`.
4. `search` for older decisions. It is lexical; try other words.

A later user instruction overrides any note. Historical notes can hold obsolete claims; current handoffs take
precedence.

## Saving a handoff

Before ending meaningful work. Use a stable task slug across turns.

```json
{
  "project": "my-app",
  "task": "release-24",
  "objective": "The continuing objective",
  "overview": "Concise current state, <=1200 chars",
  "completed": ["Implemented X; not released"],
  "remaining": ["Unfinished acceptance or follow-up"],
  "validated": ["Exact checks and outcomes, including limits"],
  "released": [],
  "constraints": ["Restrictions still in force"],
  "supersedes": ["previous-current-id-if-updating"],
  "resolved_previous": ["Prior remaining item deliberately completed or cancelled"],
  "evidence": [{"path": "/abs/source.md", "line": 12, "quote": "exact substring on that line"}]
}
```

- All five lists are required, even empty. Keep implemented, checked and shipped apart.
- Updating a task with a current head: list it in `supersedes` (or `"parallel": true` for a real branch).
- Every previous `remaining` item reappears in `remaining`, `parked` or `resolved_previous`; constraints in
  `constraints` or `resolved_constraints`.
- Max 40 evidence entries, 60 items per list, 1,500 chars per item. Cite what supports this update only.

The tool checks structure and provenance, not whether the interpretation is true.

## Failure modes

- Treating a Stop capture as a completion claim. It is unreviewed evidence.
- Marking work complete because the turn ended.
- Guessing a project from a folder name instead of routing or binding it.
- Starting a second memory store next to this one.
