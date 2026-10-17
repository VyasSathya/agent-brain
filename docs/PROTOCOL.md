# Protocol

How sessions use the brain, and every limit the tool enforces. The tool uses the Python standard library and
never calls a model or a network service.

## Starting a task

The SessionStart hook routes the working directory by normalized, boundary-safe, longest registered path.
Source folders, task folders, staging copies and historical copies have distinct roles. A real git worktree
outside registered paths is mapped through its git common directory; subfolders of a monorepo keep their
route. Explicit aliases take precedence. Unknown paths stay unmapped: no substring or name guessing, no drive
scan, no remote lookup.

Read the relevant current handoffs and their evidence before acting. A source link is a historical
observation, not a promise that the file or a live service is unchanged: `current` reports
`evidence_changed` when a cited file's hash differs, and then the live file wins. All current tasks are kept;
two heads of the same task are a conflict that needs explicit reconciliation, never a silent pick.

```bash
agent-brain route --cwd /abs/path
agent-brain current --project my-app
agent-brain search --project my-app --query "pagination cursor"
agent-brain search --project my-app --query "duplicate rows" --offset 5 --limit 5
agent-brain bind --project my-app --path /abs/known/new/task --role task
```

When a new task folder belongs to a known project, bind it once. A path cannot be bound twice by accident;
changing an existing binding means editing `routes.json` by hand. Every registry change keeps a backup in
`backups/`. Do not run two `bind` commands at once; handoff and capture writes use unique immutable files and
are safe to run concurrently.

## Saving a handoff

Write a JSON file (anywhere; a `work/` folder in the task is a good place), then
`agent-brain handoff --file <file>`. Use a stable task slug across turns, not a new slug each time.

```json
{
  "project": "my-app",
  "task": "release-24",
  "objective": "The actual continuing objective",
  "overview": "Optional concise current state; never a replacement for evidence",
  "completed": ["Implemented change; not automatically released"],
  "remaining": ["Unfinished acceptance or follow-up"],
  "validated": ["Exact checks and outcomes, including limitations"],
  "released": [],
  "constraints": ["Source/staging/deployment restrictions still in force"],
  "supersedes": ["previous-current-id-if-updating"],
  "resolved_previous": ["Exact prior remaining item deliberately completed or cancelled"],
  "parked": ["Open item recorded but not being worked on now"],
  "evidence": [{"path": "/abs/source.md", "line": 12, "quote": "Exact substring on that line"}]
}
```

Rules the tool enforces:

- All five state lists are required, even when empty. Empty means "nothing claimed", not "nothing happened".
- Every evidence quote must appear on the cited line; the file's hash is stored. Relative evidence paths are
  resolved against the handoff file's folder.
- Updating a task that already has a current handoff must list it in `supersedes`, or set `"parallel": true`
  for a deliberate branch. Without this rule, updates pile up as sibling heads of one task.
- Supersession only works within the same project and task, and only on current heads.
- Every previous `remaining` item must reappear in `remaining`, `parked` or `resolved_previous`. Every
  previous constraint must reappear in `constraints` or `resolved_constraints`. Evidence should support the
  resolution.
- Bounds per new handoff: 40 evidence entries, 60 items per list, 1,500 characters per item, 1,200 characters
  of overview, 128 KB per record. Cite what supports this update; earlier evidence stays in the superseded
  chain. These bounds exist because a handoff that copies its whole history forward on every update grows
  without limit.
- Superseded handoffs move to `handoffs/<project>/archive/`; search and read still cover them.

The tool checks structure and provenance, not the truth of the author's interpretation. Never mark a task
complete because a turn ended. Preserve other tasks when reconciling. `reconcile --project <id> --task <slug>`
merges sibling heads into one whose remaining items and constraints are the union of all of them, with
nothing marked resolved.

## Retrieval and limits

- Startup context is capped by `startup_budget` (8,000 characters by default). The startup files
  (`facts.md`, `user.md`) load first, then up to three overviews of handoffs active within `active_days`
  (14 by default). Sections that do not fit are named with their paths, never cut mid-text.
- `current` returns up to five heads per page with `next_offset`, newest first, only tasks updated within
  `active_days` unless `--all` is given (`older_not_shown` says how many were hidden).
- `search` reads only the selected project's registered evidence files, project notes and handoffs. Current
  handoffs rank above older evidence and superseded handoffs are labeled. Page size defaults to five, max ten.
  Excerpts are capped at 900 characters and marked when shortened. Results carry absolute path, exact line,
  total matches and the next offset. It is lexical search, not semantic: try other wording.
- Cross-project notes (`global_files` in `brain.json`) are searched only with an explicit `--project global`.
- `read --project <id> --path <p> --line <n> --count 40` returns exact text with line and column. Follow
  `next_line` / `next_column` for long lines. Reads refuse paths outside that project's registered evidence,
  notes, handoffs and captures.
- Search and read target a 6,000-character JSON payload. Files stay whole on disk. If sources change between
  pages, restart from offset 0. No index is built; scan time grows with the registered Markdown.

## Stop capture

The Stop hook stores the session's final reply, session id, route and transcript path under
`captures/<project>/`. If the hook payload has no final text, it reads at most the last 256 KB of the
transcript and takes the newest assistant reply (Codex rollout rows, or Claude Code rows with text blocks;
subagent rows are skipped). Missing text is recorded explicitly. It never reads a whole long transcript, never
calls a model, never infers completion and never touches a handoff. Captures are deduplicated by session,
transcript, folder and content. Startup points to the newest unreviewed capture.

A capture is unreviewed evidence, not a fresh instruction. The original transcript stays the source for the
user's exact words and for interrupted work. Saving structured handoffs at milestones is the agent's job; a
crash before Stop cannot be captured.

## Two machines

Make the brain folder a git repository and set `"git_sync": true`. `sync` commits local changes and, once a
remote exists, pulls with rebase and pushes. SessionStart pulls (6 s bound); Stop commits and pushes (8 s
bound). Failures are reported in the hook result and never block a session; a failed rebase is aborted and
local commits are kept. Captures, backups and logs stay machine-local (`.gitignore`).

The first machine's paths are canonical: handoffs and `routes.json` store them. `machines.json` maps canonical
prefixes to another machine's prefixes, keyed by lowercased hostname or `BRAIN_MACHINE`. Routing, evidence
checks, `bind` and `read` translate through it. Set it up with
`agent-brain map-machine --map C:/code=~/code`.

## Two tools

Claude Code and Codex run the same commands: `hook-start` prints the `hookSpecificOutput.additionalContext`
shape both expect, and `hook-stop` captures the final reply. `catchup --days N` is the read-only overview for
a cold start: current heads across projects with remaining items, git branch / last commit / dirty count per
registered source repo, the latest Codex threads (read-only from its state database, falling back to rollout
files) and Claude Code sessions (from `~/.claude/projects`), and unreviewed captures. Each part runs in
parallel under a 3.8 s budget.

Optional: `render_agents` in `brain.json` renders Codex's global `AGENTS.md` from rule files kept in the
brain, so one copy of standing rules serves both tools (Claude imports the shared file with `@path`).
