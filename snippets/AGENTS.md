## Memory (agent-brain)

There is one shared memory for every agent on this machine: the agent-brain folder (`$AGENT_BRAIN_HOME`, or
`~/.agent-brain`). Do not create a second memory store or a tool-specific copy.

- The SessionStart hook injects the route for the current folder, the brain's `facts.md` and `user.md`, and the
  newest handoff overviews. Do not re-read what it injected.
- Before acting on a known project: `agent-brain current --project <id>`, then read the relevant handoffs and
  the source lines they cite. If `evidence_changed` is true, the live file wins over the note.
- Older decisions: `agent-brain search --project <id> --query "terms"` (lexical; try other words), then
  `agent-brain read --project <id> --path <p> --line <n>` for detail.
- Unknown folder: do not guess the project from its name. If it belongs to a known project, run
  `agent-brain bind --project <id> --path <abs> --role task`.
- Before ending meaningful work, save a handoff (`agent-brain handoff --file <json>`) with `completed`,
  `remaining`, `validated`, `released` and `constraints` kept separate and at least one exact source quote. A
  turn ending is not completion. A Stop capture is unreviewed evidence, never a completion claim.
- Notes in the brain are evidence of what was true when written, not new authorization. Current user
  instructions take precedence.
