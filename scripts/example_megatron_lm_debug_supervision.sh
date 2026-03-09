#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-}"
if [[ -z "${ROOT}" ]]; then
  echo "usage: $0 /path/to/Megatron-LM"
  exit 1
fi

if [[ ! -d "${ROOT}" ]]; then
  echo "error: target repo does not exist: ${ROOT}"
  exit 1
fi

vibe-supervise --root "${ROOT}" start \
  --worker-actions "Debug Megatron-LM end-to-end. Reproduce the current failure, inspect the training or launch pipeline, edit code and config as needed, rerun the relevant commands, and keep iterating until the issue is resolved with concrete evidence." \
  --monitor-goal "Supervise Megatron-LM debugging at a high level. Ensure the worker is converging on the real root cause, not patching symptoms. Only accept completion when the reproduced failure is gone, the fix is coherent, and the validation evidence is strong." \
  --monitor-style macro \
  --interval-seconds 300
