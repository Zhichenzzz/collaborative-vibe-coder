# Collaboration Contract

This repository is built for multiple coding agents sharing the same working tree.

Every agent must use the repo-local collaboration protocol before touching code:

1. Run `PYTHONPATH=src python -m collaborative_vibe_coder init` once per repo.
2. Register yourself with `agent register`.
   You can also let the repo do this for you with `session launch`.
3. Claim a task before making edits.
4. Send a heartbeat whenever you start a meaningful work block.
5. Update the task when you are blocked, handing off, or done.

## Roles

### Monitor

- creates tasks
- assigns work to workers
- watches `board` and `events`
- inspects `session logs` for worker output
- uses `session send` to push workers with concrete next actions
- reviews blocked tasks and stale agents
- uses direct messages for explicit handoffs

### Worker

- claims a task before editing
- writes short progress summaries on task updates
- sends a message when blocked on another worker
- keeps heartbeats current while active

### Reviewer

- claims review tasks explicitly
- closes the loop by marking tasks `done` or sending them back to `open`

## Conventions

- Use stable agent names such as `codex-monitor`, `codex-worker-1`, `claude-worker-1`.
- If you want a live terminal for an agent, launch it through `session launch` and reconnect with `session attach`.
- If you want an automated supervisor loop, run `monitor run` against the monitor agent.
- Keep one task focused on one unit of work.
- Reference task ids in messages when the message changes delivery expectations.
- If a worker disappears, the monitor should inspect the last heartbeat and either reassign or reopen the task.
