# collaborative-vibe-coder

`collaborative-vibe-coder` is a repo-local coordination layer for running multiple coding agents against the same repository.

It is built for setups like:

- codex -> codex
- codex -> claude
- claude -> claude
- one monitor + one worker
- one monitor + many workers

Shared state lives in `.collab/` inside the target repo. The tool can:

- launch live Codex or Claude sessions in `tmux`
- keep a task board, messages, heartbeats, and event history
- run a monitor loop that periodically reviews worker progress
- keep separate scratchpad logs for each agent

## Install

```bash
pip install git+https://github.com/Zhichenzzz/collaborative-vibe-coder.git
```

For local editable development:

```bash
git clone https://github.com/Zhichenzzz/collaborative-vibe-coder.git
cd collaborative-vibe-coder
pip install -e .
```

This installs:

- `vibe-collab`
- `vibe-supervise`

## Recommended Usage

Inside any target repo:

```bash
cd /path/to/repo

vibe-supervise start \
  --worker-actions "Describe exactly what the worker should do." \
  --monitor-goal "Describe exactly what the monitor should accept as done."
```

This starts:

- one worker session
- one monitor session
- one background monitor loop
- separate scratchpads for both agents

Defaults:

- monitor style: `macro`
- monitor interval: `300` seconds
- scratchpad: enabled
- full access: enabled

Stop the default setup with:

```bash
vibe-supervise stop
```

## Megatron-LM Example

```bash
cd /path/to/Megatron-LM

vibe-supervise start \
  --worker-actions "Reproduce the current failure, debug Megatron-LM, edit code and config, rerun the relevant commands, and keep iterating until the issue is resolved." \
  --monitor-goal "Stay high-level. Verify the worker is converging on the root cause and only accept completion when the reproduced failure is gone and the validation evidence is strong." \
  --monitor-style macro
```

There is also an example wrapper script:

```bash
./scripts/example_megatron_lm_debug_supervision.sh /path/to/Megatron-LM
```

## User-Controlled Inputs

Nothing is hardcoded to a specific repo.

You control:

- `--worker-actions`
- `--monitor-goal`
- `--monitor-style`
- `--interval-seconds`
- `--env KEY=VALUE`
- `--gpu`
- `--worker-prompt`
- `--monitor-prompt`
- `--worker-name`
- `--monitor-name`

## Monitor Modes

- `macro`: default, higher-level supervision, default interval `300`
- `micro`: tighter supervision, default interval `60`

If you want the monitor to manage broadly rather than micromanage, keep `--monitor-style macro`.

## Scratchpads

Each live agent can write to its own scratchpad log.

Paths:

- `.collab/runtime/scratchpads/<worker>.log`
- `.collab/runtime/scratchpads/<monitor>.log`

Read them with:

```bash
vibe-collab session scratchpad --agent codex-worker-1 --lines 120
vibe-collab session scratchpad --agent codex-monitor --lines 120
```

Scratchpads include:

- launch metadata
- bootstrap prompt
- prompts injected with `session send`
- tmux pane output captured during the session

## Useful Commands

```bash
vibe-collab session list
vibe-collab session attach --agent codex-worker-1
vibe-collab session attach --agent codex-monitor
vibe-collab session scratchpad --agent codex-worker-1 --lines 120
vibe-collab session scratchpad --agent codex-monitor --lines 120
vibe-collab session logs --agent codex-worker-1 --lines 80
vibe-collab session send --agent codex-worker-1 --text "Focus on the next milestone."
vibe-collab board
```

## Lower-Level Workflow

If you do not want the one-command launcher, you can still use the raw primitives:

```bash
vibe-collab init
vibe-collab agent register ...
vibe-collab task create ...
vibe-collab session launch ...
vibe-collab monitor run ...
```

## Layout

After initialization, the target repo gets:

```text
.collab/
  meta.json
  .lock
  agents/
  tasks/
  messages/
  heartbeats/
  logs/events.jsonl
  runtime/
    sessions/
    scratchpads/
```

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
