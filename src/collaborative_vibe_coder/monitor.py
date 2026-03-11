from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from collaborative_vibe_coder.session import SessionManager
from collaborative_vibe_coder.store import CollabError, CollabStore, utc_now
from collaborative_vibe_coder.supervise import default_interval_for_style


class MonitorManager:
    """Drive a higher-level monitor agent that supervises one or more workers."""

    def __init__(self, store: CollabStore, session_manager: SessionManager) -> None:
        self.store = store
        self.session_manager = session_manager

    def tick(
        self,
        *,
        monitor_agent: str,
        worker_agents: list[str] | None = None,
        task_id: str | None = None,
        goal: str = "",
        interval_seconds: int | None = None,
        worker_log_lines: int = 60,
    ) -> dict[str, Any]:
        monitor_session = self.store.get_session(agent_name=monitor_agent)
        if not monitor_session:
            raise CollabError(f"Unknown monitor session: {monitor_agent}")

        configured_workers = monitor_session.get("watch_workers", [])
        worker_agents = sorted(set(worker_agents or configured_workers))
        task_id = task_id or monitor_session.get("task_id")
        goal = goal or monitor_session.get("goal", "")
        monitor_style = monitor_session.get("monitor_style", "macro")
        interval_seconds = interval_seconds or monitor_session.get("interval_seconds")

        task = None
        if task_id:
            task = self.store._require_task(task_id)
            if task["status"] == "done":
                return {
                    "monitor_agent": monitor_agent,
                    "completed": True,
                    "reason": "task_already_done",
                    "task_id": task_id,
                }

        prompt = self._build_monitor_prompt(
            monitor_agent=monitor_agent,
            worker_agents=worker_agents,
            task=task,
            goal=goal,
            worker_log_lines=worker_log_lines,
            interval_seconds=interval_seconds,
            monitor_style=monitor_style,
        )
        delivery = self.session_manager.send(agent_name=monitor_agent, text=prompt)
        self.store.heartbeat(
            agent_name=monitor_agent,
            status="monitoring",
            note=f"monitor tick for {', '.join(worker_agents) or 'workers'}",
        )
        self.store._append_event(
            event_type="monitor.tick",
            actor=monitor_agent,
            payload={
                "workers": worker_agents,
                "task_id": task_id,
                "goal": goal,
            },
        )
        return {
            "monitor_agent": monitor_agent,
            "completed": False,
            "task_id": task_id,
            "workers": worker_agents,
            "goal": goal,
            "interval_seconds": interval_seconds,
            "monitor_style": monitor_style,
            "sent_at": delivery["sent_at"],
            "prompt_preview": prompt[:800],
        }

    def run(
        self,
        *,
        monitor_agent: str,
        worker_agents: list[str] | None = None,
        task_id: str | None = None,
        goal: str = "",
        interval_seconds: int | None = None,
        max_ticks: int | None = None,
        worker_log_lines: int = 60,
    ) -> dict[str, Any]:
        monitor_session = self.store.get_session(agent_name=monitor_agent)
        if not monitor_session:
            raise CollabError(f"Unknown monitor session: {monitor_agent}")
        monitor_style = monitor_session.get("monitor_style", "macro")
        interval_seconds = (
            interval_seconds
            or monitor_session.get("interval_seconds")
            or default_interval_for_style(monitor_style)
        )
        if interval_seconds < 0:
            raise CollabError("interval_seconds must be >= 0")
        ticks = 0
        started_at = utc_now()
        last_result: dict[str, Any] | None = None
        while True:
            last_result = self.tick(
                monitor_agent=monitor_agent,
                worker_agents=worker_agents,
                task_id=task_id,
                goal=goal,
                interval_seconds=interval_seconds,
                worker_log_lines=worker_log_lines,
            )
            ticks += 1
            if last_result.get("completed"):
                stopped_reason = last_result["reason"]
                break
            if max_ticks is not None and ticks >= max_ticks:
                stopped_reason = "max_ticks_reached"
                break
            time.sleep(interval_seconds)
        return {
            "monitor_agent": monitor_agent,
            "ticks": ticks,
            "started_at": started_at,
            "finished_at": utc_now(),
            "stopped_reason": stopped_reason,
            "last_result": last_result,
        }

    def _build_monitor_prompt(
        self,
        *,
        monitor_agent: str,
        worker_agents: list[str],
        task: dict[str, Any] | None,
        goal: str,
        worker_log_lines: int,
        interval_seconds: int | None,
        monitor_style: str,
    ) -> str:
        board = self.store.board()
        events = self.store.events(limit=12)
        sessions = self.session_manager.list_sessions()
        repo_snapshot = get_repo_snapshot(self.store.root)
        worker_logs = {}
        for worker in worker_agents:
            try:
                worker_logs[worker] = self.session_manager.logs(
                    agent_name=worker,
                    lines=worker_log_lines,
                )["output"]
            except CollabError as exc:
                worker_logs[worker] = f"<unavailable: {exc}>"

        instructions = [
            f"You are {monitor_agent}, the supervising agent for this repository.",
            f"Workers under supervision: {', '.join(worker_agents) if worker_agents else 'none declared'}.",
            f"Supervision style: {monitor_style}.",
            f"Expected review interval: {interval_seconds or 'manual'} seconds.",
            "This prompt is a scheduled supervision check. Do not ignore it or defer silently.",
            f"Goal to enforce: {goal or 'No explicit goal provided. Infer it from the task and task history.'}",
            "Review the requirement, task state, repo state, recent events, and worker logs below.",
            "Decide whether the worker is on track, blocked, missing requirements, or finished.",
        ]
        if monitor_style == "macro":
            instructions.extend(
                [
                    "Stay high-level. Do not nitpick implementation details unless they block the goal.",
                    "Focus on milestone completion, benchmark validity, major blockers, and whether the worker's next direction is correct.",
                    "If you need to push a worker, use: PYTHONPATH=src python3 -m collaborative_vibe_coder session send --agent <worker> --text \"<next milestone or correction>\"",
                    "If you need to push a worker, prefer one concise high-impact instruction for the next milestone.",
                    "If the task is complete, mark it done with collaborative_vibe_coder task update and stop pushing.",
                    "Every review tick must end with one of these outcomes: send a worker instruction, mark the task done, or update your heartbeat with a concrete reason for holding steady until the next check.",
                ]
            )
        else:
            instructions.extend(
                [
                    "If you need to push a worker, send a direct instruction with:",
                    "PYTHONPATH=src python3 -m collaborative_vibe_coder session send --agent <worker> --text \"<next action>\"",
                    "If the task is complete, mark it done with collaborative_vibe_coder task update and stop pushing.",
                    "If the worker is blocked, tell it the missing step explicitly instead of giving vague feedback.",
                    "Every review tick must end with one of these outcomes: send a worker instruction, mark the task done, or update your heartbeat with a concrete reason for holding steady until the next check.",
                ]
            )
        instructions.extend(
            [
            "",
            "TASK SNAPSHOT:",
            json.dumps(task, indent=2, sort_keys=True, ensure_ascii=False) if task else "<no task selected>",
            "",
            "BOARD SNAPSHOT:",
            json.dumps(board, indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "SESSION SNAPSHOT:",
            json.dumps(sessions, indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "REPO SNAPSHOT:",
            json.dumps(repo_snapshot, indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "RECENT EVENTS:",
            json.dumps(events, indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "WORKER LOGS:",
            json.dumps(worker_logs, indent=2, sort_keys=True, ensure_ascii=False),
            ]
        )
        return "\n".join(instructions)


def get_repo_snapshot(root: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"root": str(root), "generated_at": utc_now(), "git": None}
    inside_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if inside_git.returncode != 0:
        return snapshot
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    diff_stat = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    snapshot["git"] = {
        "status_short": status.stdout.strip(),
        "diff_stat": diff_stat.stdout.strip(),
    }
    return snapshot
