from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from collaborative_vibe_coder.store import CollabError, CollabStore, utc_now


class SessionManager:
    """Launch and inspect live tmux-backed agent sessions."""

    def __init__(self, store: CollabStore) -> None:
        self.store = store

    def launch(
        self,
        *,
        agent_name: str,
        kind: str,
        role: str,
        purpose: str = "",
        capabilities: list[str] | None = None,
        task_id: str | None = None,
        model: str | None = None,
        tmux_session: str | None = None,
        extra_prompt: str = "",
        command_override: str | None = None,
        watch_workers: list[str] | None = None,
        goal: str = "",
        interval_seconds: int | None = None,
        env_vars: list[str] | None = None,
        full_access: bool = False,
        enable_search: bool = False,
        monitor_style: str = "macro",
        scratchpad: bool = True,
    ) -> dict[str, Any]:
        if not self.store.meta_path.exists():
            self.store.init()
        tmux_session = tmux_session or self._default_tmux_session(agent_name)
        if self._tmux_session_exists(tmux_session):
            raise CollabError(f"tmux session already exists: {tmux_session}")

        self.store.register_agent(
            name=agent_name,
            kind=kind,
            role=role,
            purpose=purpose,
            capabilities=capabilities or [],
            status="starting",
        )
        self.store.heartbeat(agent_name=agent_name, status="starting", note="tmux session launching")

        if task_id:
            self.store._require_task(task_id)

        prompt = build_bootstrap_prompt(
            root=self.store.root,
            agent_name=agent_name,
            kind=kind,
            role=role,
            task_id=task_id,
            extra_prompt=extra_prompt,
            watch_workers=watch_workers or [],
            goal=goal,
            monitor_style=monitor_style,
        )
        command = command_override or shlex.join(
            build_agent_command(
                kind=kind,
                root=self.store.root,
                prompt=prompt,
                model=model,
                env_vars=env_vars or [],
                full_access=full_access,
                enable_search=enable_search,
            )
        )

        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                tmux_session,
                "-c",
                str(self.store.root),
                command,
            ],
            check=True,
        )

        scratchpad_path: str | None = None
        if scratchpad:
            scratchpad_path = str(self.store.scratchpad_path(agent_name=agent_name))
            self.store.append_scratchpad(
                agent_name=agent_name,
                channel="session.launch",
                text="\n".join(
                    [
                        f"agent_name={agent_name}",
                        f"kind={kind}",
                        f"role={role}",
                        f"tmux_session={tmux_session}",
                        f"task_id={task_id or '-'}",
                        f"monitor_style={monitor_style}",
                        f"launch_command={command}",
                        "",
                        "bootstrap_prompt:",
                        prompt,
                    ]
                ),
            )
            self._enable_scratchpad_capture(tmux_session=tmux_session, scratchpad_path=Path(scratchpad_path))
            time.sleep(0.05)
            initial_snapshot = self._capture_pane_text(tmux_session=tmux_session, lines=120).strip()
            if initial_snapshot:
                self.store.append_scratchpad(
                    agent_name=agent_name,
                    channel="session.initial_pane",
                    text=initial_snapshot,
                )

        launched_at = utc_now()
        record = self.store.save_session(
            agent_name=agent_name,
            data={
                "agent_name": agent_name,
                "kind": kind,
                "role": role,
                "purpose": purpose,
                "capabilities": sorted(set(capabilities or [])),
                "task_id": task_id,
                "model": model,
                "tmux_session": tmux_session,
                "launch_command": command,
                "attach_command": f"tmux attach-session -t {tmux_session}",
                "watch_workers": sorted(set(watch_workers or [])),
                "goal": goal,
                "interval_seconds": interval_seconds,
                "env_vars": list(env_vars or []),
                "full_access": full_access,
                "enable_search": enable_search,
                "monitor_style": monitor_style,
                "scratchpad": scratchpad,
                "scratchpad_path": scratchpad_path,
                "status": "running",
                "launched_at": launched_at,
                "updated_at": launched_at,
            },
        )
        record = self.store.save_session(
            agent_name=agent_name,
            data={"tmux_active": True},
        )
        self.store._append_event(
            event_type="session.launched",
            actor=agent_name,
            payload={"tmux_session": tmux_session, "task_id": task_id, "kind": kind},
        )
        return record

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = self.store.list_sessions()
        enriched = []
        for session in sessions:
            tmux_session = session.get("tmux_session")
            active = bool(tmux_session) and self._tmux_session_exists(tmux_session)
            if session.get("tmux_active") != active:
                session = self.store.save_session(
                    agent_name=session["agent_name"],
                    data={
                        "tmux_active": active,
                        "status": "running" if active else "stopped",
                        "updated_at": utc_now(),
                    },
                )
            enriched.append(session)
        return enriched

    def logs(self, *, agent_name: str, lines: int = 80) -> dict[str, Any]:
        session = self.store.get_session(agent_name=agent_name)
        if not session:
            raise CollabError(f"Unknown session for agent: {agent_name}")
        tmux_session = session["tmux_session"]
        if not self._tmux_session_exists(tmux_session):
            raise CollabError(f"tmux session is not active: {tmux_session}")
        return {
            "agent_name": agent_name,
            "tmux_session": tmux_session,
            "captured_at": utc_now(),
            "output": self._capture_pane_text(tmux_session=tmux_session, lines=lines).rstrip(),
        }

    def send(self, *, agent_name: str, text: str, press_enter: bool = True) -> dict[str, Any]:
        session = self.store.get_session(agent_name=agent_name)
        if not session:
            raise CollabError(f"Unknown session for agent: {agent_name}")
        tmux_session = session["tmux_session"]
        if not self._tmux_session_exists(tmux_session):
            raise CollabError(f"tmux session is not active: {tmux_session}")
        lines = text.splitlines() or [text]
        for index, line in enumerate(lines):
            if line:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_session, "-l", line],
                    check=True,
                )
            if press_enter or index < len(lines) - 1:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_session, "Enter"],
                    check=True,
                )
        payload = {
            "agent_name": agent_name,
            "tmux_session": tmux_session,
            "sent_at": utc_now(),
            "text": text,
            "press_enter": press_enter,
        }
        if session.get("scratchpad"):
            self.store.append_scratchpad(
                agent_name=agent_name,
                channel="session.send",
                text=text,
            )
        self.store._append_event(
            event_type="session.prompted",
            actor=agent_name,
            payload={"chars": len(text), "press_enter": press_enter},
        )
        return payload

    def scratchpad(self, *, agent_name: str, lines: int = 120) -> dict[str, Any]:
        session = self.store.get_session(agent_name=agent_name)
        if not session:
            raise CollabError(f"Unknown session for agent: {agent_name}")
        scratchpad_path = session.get("scratchpad_path")
        if not scratchpad_path:
            raise CollabError(f"Scratchpad is disabled for agent: {agent_name}")
        content = self.store.read_scratchpad(agent_name=agent_name)
        tail = "\n".join(content.splitlines()[-lines:])
        return {
            "agent_name": agent_name,
            "scratchpad_path": scratchpad_path,
            "captured_at": utc_now(),
            "output": tail,
        }

    def stop(self, *, agent_name: str) -> dict[str, Any]:
        session = self.store.get_session(agent_name=agent_name)
        if not session:
            raise CollabError(f"Unknown session for agent: {agent_name}")
        tmux_session = session["tmux_session"]
        if self._tmux_session_exists(tmux_session):
            subprocess.run(["tmux", "kill-session", "-t", tmux_session], check=True)
        now = utc_now()
        updated = self.store.save_session(
            agent_name=agent_name,
            data={
                "tmux_active": False,
                "status": "stopped",
                "updated_at": now,
                "stopped_at": now,
            },
        )
        self.store.heartbeat(agent_name=agent_name, status="stopped", note="tmux session stopped")
        self.store._append_event(
            event_type="session.stopped",
            actor=agent_name,
            payload={"tmux_session": tmux_session},
        )
        return updated

    def attach(self, *, agent_name: str) -> int:
        session = self.store.get_session(agent_name=agent_name)
        if not session:
            raise CollabError(f"Unknown session for agent: {agent_name}")
        tmux_session = session["tmux_session"]
        if not self._tmux_session_exists(tmux_session):
            raise CollabError(f"tmux session is not active: {tmux_session}")
        return subprocess.run(["tmux", "attach-session", "-t", tmux_session], check=False).returncode

    @staticmethod
    def _default_tmux_session(agent_name: str) -> str:
        return f"vibe-{agent_name}"

    @staticmethod
    def _tmux_session_exists(tmux_session: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", tmux_session],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    @staticmethod
    def _capture_pane_text(*, tmux_session: str, lines: int) -> str:
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-t",
                tmux_session,
                "-S",
                f"-{lines}",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout

    @staticmethod
    def _enable_scratchpad_capture(*, tmux_session: str, scratchpad_path: Path) -> None:
        pipe_command = f"cat >> {shlex.quote(str(scratchpad_path))}"
        subprocess.run(
            ["tmux", "pipe-pane", "-o", "-t", tmux_session, pipe_command],
            check=True,
        )


def build_bootstrap_prompt(
    *,
    root: Path,
    agent_name: str,
    kind: str,
    role: str,
    task_id: str | None,
    extra_prompt: str = "",
    watch_workers: list[str] | None = None,
    goal: str = "",
    monitor_style: str = "macro",
) -> str:
    watch_workers = watch_workers or []
    commands = [
        "PYTHONPATH=src python3 -m collaborative_vibe_coder board",
        f"PYTHONPATH=src python3 -m collaborative_vibe_coder message inbox --agent {agent_name} --mark-read",
        f"PYTHONPATH=src python3 -m collaborative_vibe_coder heartbeat --agent {agent_name} --status active --note \"starting work block\"",
    ]
    if task_id:
        commands.append(f"PYTHONPATH=src python3 -m collaborative_vibe_coder task claim --task {task_id} --agent {agent_name}")
    task_line = f"Primary task: {task_id}." if task_id else "No primary task was preassigned."
    extra_line = f"Extra instruction: {extra_prompt}" if extra_prompt else ""
    monitor_lines = []
    if role == "monitor":
        monitor_lines.append(f"Supervision style: {monitor_style}.")
        if watch_workers:
            monitor_lines.append(f"You supervise these workers: {', '.join(watch_workers)}.")
        if goal:
            monitor_lines.append(f"Success target: {goal}")
        if monitor_style == "macro":
            monitor_lines.append(
                "Stay high-level. Focus on milestones, blockers, benchmark evidence, and whether the repo is converging toward the goal."
            )
            monitor_lines.append(
                "Do not micromanage code details every cycle. Push only high-leverage corrections or missing major requirements."
            )
        monitor_lines.append(
            "When you need to nudge a worker, use `PYTHONPATH=src python3 -m collaborative_vibe_coder session send --agent <worker> --text \"...\"`."
        )
        monitor_lines.append(
            "Compare the requirement, repo state, and worker output. Keep pushing workers until the goal is met, then mark the task done."
        )
    return "\n".join(
        line
        for line in [
            f"You are {agent_name}, a {kind} agent acting as {role} in repo {root}.",
            f"Read {root / 'AGENTS.md'} first and follow the repo-local collaboration contract.",
            task_line,
            *monitor_lines,
            "Start by running these commands:",
            *[f"- {command}" for command in commands],
            "While working, keep task state, messages, and heartbeats updated through collaborative_vibe_coder.",
            "If you finish or get blocked, write a concise task update before stopping.",
            extra_line,
        ]
        if line
    )


def build_agent_command(
    *,
    kind: str,
    root: Path,
    prompt: str,
    model: str | None = None,
    env_vars: list[str] | None = None,
    full_access: bool = False,
    enable_search: bool = False,
) -> list[str]:
    normalized_kind = kind.lower()
    env_vars = env_vars or []
    command: list[str] = []
    if env_vars:
        command.extend(["env", *env_vars])
    if normalized_kind == "codex":
        command.extend(
            [
            "codex",
            "--no-alt-screen",
            "-C",
            str(root),
            ]
        )
        if full_access:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["-a", "never", "-s", "workspace-write"])
        if enable_search:
            command.append("--search")
        if model:
            command.extend(["-m", model])
        command.append(prompt)
        return command
    if normalized_kind == "claude":
        command.extend(
            [
            "claude",
            "--add-dir",
            str(root),
            "--dangerously-skip-permissions",
            ]
        )
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command
    raise CollabError(f"Unsupported live launcher kind: {kind}")
