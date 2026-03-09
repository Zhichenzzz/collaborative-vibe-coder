from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from collaborative_vibe_coder.session import SessionManager
from collaborative_vibe_coder.store import CollabError, CollabStore


def default_interval_for_style(style: str) -> int:
    if style == "micro":
        return 60
    return 300


class SupervisionManager:
    """High-level convenience layer for starting and stopping worker+monitor supervision."""

    def __init__(self, store: CollabStore, session_manager: SessionManager) -> None:
        self.store = store
        self.session_manager = session_manager

    def start(
        self,
        *,
        monitor_goal: str,
        worker_actions: str = "",
        task_title: str = "",
        task_id: str | None = None,
        requester_name: str = "human-owner",
        worker_name: str = "codex-worker-1",
        worker_kind: str = "codex",
        monitor_name: str = "codex-monitor",
        monitor_kind: str = "codex",
        gpu: str | None = None,
        env_vars: list[str] | None = None,
        interval_seconds: int | None = None,
        monitor_style: str = "macro",
        worker_prompt: str = "",
        monitor_prompt: str = "",
        full_access: bool = True,
        enable_search: bool = True,
        scratchpad: bool = True,
        no_loop: bool = False,
        orchestrator_session: str | None = None,
        worker_command: str | None = None,
        monitor_command: str | None = None,
        max_ticks: int | None = None,
    ) -> dict[str, Any]:
        self.store.init()
        interval_seconds = interval_seconds or default_interval_for_style(monitor_style)
        orchestrator_session = orchestrator_session or f"vibe-orchestrator-{monitor_name}"

        env_vars = list(env_vars or [])
        if gpu is not None:
            env_vars.append(f"CUDA_VISIBLE_DEVICES={gpu}")

        self.store.register_agent(
            name=requester_name,
            kind="human",
            role="requester",
            purpose="launches supervised work",
            status="active",
        )

        if task_id:
            task = self.store._require_task(task_id)
        else:
            task_description = "\n\n".join(
                part
                for part in [
                    f"Worker actions:\n{worker_actions}" if worker_actions else "",
                    f"Monitor goal:\n{monitor_goal}",
                ]
                if part
            )
            task = self.store.create_task(
                title=task_title or self._default_task_title(monitor_goal),
                description=task_description or monitor_goal,
                created_by=requester_name,
                priority="high",
            )
            task_id = task["id"]

        worker_prompt = worker_prompt or self._default_worker_prompt(
            worker_actions=worker_actions,
            monitor_goal=monitor_goal,
            gpu=gpu,
            env_vars=env_vars,
        )
        monitor_prompt = monitor_prompt or self._default_monitor_prompt(
            worker_actions=worker_actions,
            monitor_goal=monitor_goal,
            style=monitor_style,
            gpu=gpu,
            env_vars=env_vars,
        )

        worker_session = self.session_manager.launch(
            agent_name=worker_name,
            kind=worker_kind,
            role="worker",
            task_id=task_id,
            extra_prompt=worker_prompt,
            command_override=worker_command,
            env_vars=env_vars,
            full_access=full_access,
            enable_search=False,
            monitor_style=monitor_style,
            scratchpad=scratchpad,
        )
        monitor_session = self.session_manager.launch(
            agent_name=monitor_name,
            kind=monitor_kind,
            role="monitor",
            task_id=task_id,
            watch_workers=[worker_name],
            goal=monitor_goal,
            interval_seconds=interval_seconds,
            extra_prompt=monitor_prompt,
            command_override=monitor_command,
            env_vars=env_vars,
            full_access=full_access,
            enable_search=enable_search,
            monitor_style=monitor_style,
            scratchpad=scratchpad,
        )

        orchestrator_started = False
        if not no_loop:
            if self._tmux_session_exists(orchestrator_session):
                raise CollabError(f"tmux session already exists: {orchestrator_session}")
            command = [
                sys.executable,
                "-m",
                "collaborative_vibe_coder",
                "--root",
                str(self.store.root),
                "monitor",
                "run",
                "--monitor",
                monitor_name,
                "--interval-seconds",
                str(interval_seconds),
            ]
            if max_ticks is not None:
                command.extend(["--max-ticks", str(max_ticks)])
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    orchestrator_session,
                    "-c",
                    str(self.store.root),
                    shlex.join(command),
                ],
                check=True,
            )
            orchestrator_started = True

        return {
            "root": str(self.store.root),
            "task_id": task_id,
            "task_title": task["title"],
            "monitor_goal": monitor_goal,
            "worker_actions": worker_actions,
            "worker_name": worker_name,
            "worker_tmux": worker_session["tmux_session"],
            "monitor_name": monitor_name,
            "monitor_tmux": monitor_session["tmux_session"],
            "monitor_style": monitor_style,
            "interval_seconds": interval_seconds,
            "orchestrator_session": orchestrator_session if orchestrator_started else None,
            "full_access": full_access,
            "gpu": gpu,
            "env_vars": env_vars,
            "search": enable_search,
            "scratchpad": scratchpad,
            "worker_scratchpad_path": worker_session.get("scratchpad_path"),
            "monitor_scratchpad_path": monitor_session.get("scratchpad_path"),
        }

    def stop(
        self,
        *,
        worker_name: str = "codex-worker-1",
        monitor_name: str = "codex-monitor",
        orchestrator_session: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "worker_name": worker_name,
            "monitor_name": monitor_name,
            "orchestrator_session": orchestrator_session or f"vibe-orchestrator-{monitor_name}",
            "stopped": [],
            "missing": [],
        }
        for agent_name in (worker_name, monitor_name):
            try:
                self.session_manager.stop(agent_name=agent_name)
                result["stopped"].append(agent_name)
            except CollabError:
                result["missing"].append(agent_name)
        orchestrator = result["orchestrator_session"]
        if self._tmux_session_exists(orchestrator):
            subprocess.run(["tmux", "kill-session", "-t", orchestrator], check=True)
            result["stopped"].append(orchestrator)
        else:
            result["missing"].append(orchestrator)
        return result

    @staticmethod
    def _default_task_title(goal: str) -> str:
        compact = " ".join(goal.strip().split())
        if not compact:
            return "Supervised repo task"
        if len(compact) <= 72:
            return compact
        return f"{compact[:69]}..."

    @staticmethod
    def _default_worker_prompt(
        worker_actions: str,
        monitor_goal: str,
        *,
        gpu: str | None,
        env_vars: list[str],
    ) -> str:
        gpu_line = f"Use GPU {gpu} for model and evaluation work." if gpu is not None else ""
        env_line = f"Environment overrides: {', '.join(env_vars)}." if env_vars else ""
        return "\n".join(
            line
            for line in [
                "You are the implementation worker.",
                f"Required worker actions: {worker_actions or monitor_goal}",
                f"Final acceptance target: {monitor_goal}",
                gpu_line,
                env_line,
                "Read the repo AGENTS.md first.",
                "Edit code and environment configuration as needed.",
                "Do not stop at analysis. Run the relevant commands, collect evidence, and keep the task history current.",
            ]
            if line
        )

    @staticmethod
    def _default_monitor_prompt(
        worker_actions: str,
        monitor_goal: str,
        *,
        style: str,
        gpu: str | None,
        env_vars: list[str],
    ) -> str:
        gpu_line = f"The execution environment is pinned to GPU {gpu}." if gpu is not None else ""
        env_line = f"Environment overrides in scope: {', '.join(env_vars)}." if env_vars else ""
        if style == "micro":
            style_line = "Supervise closely and intervene quickly when progress stalls."
        else:
            style_line = (
                "Supervise at a high level. Focus on milestones, blockers, benchmark evidence, and final acceptance."
            )
        return "\n".join(
            line
            for line in [
                "You are the supervising monitor.",
                f"Worker was instructed to: {worker_actions or monitor_goal}",
                f"Monitor goal: {monitor_goal}",
                gpu_line,
                env_line,
                style_line,
                "Avoid micromanaging line-by-line edits. Prefer one high-impact correction per review cycle.",
                "Only mark the task done when the goal is clearly satisfied.",
            ]
            if line
        )

    @staticmethod
    def _tmux_session_exists(session_name: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
