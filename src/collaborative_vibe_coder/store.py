from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl


TASK_STATUSES = {"open", "in_progress", "blocked", "done", "cancelled"}
TASK_PRIORITIES = {"low", "medium", "high", "urgent"}
DEFAULT_META = {"next_task_number": 1}
ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))"
)


class CollabError(RuntimeError):
    """Raised when the collaboration state is invalid or a command cannot proceed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_terminal_text(text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char for char in cleaned if char in "\n\t" or ord(char) >= 32)


class CollabStore:
    """Simple file-backed store shared by multiple agent terminals."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.collab_dir = self.root / ".collab"
        self.meta_path = self.collab_dir / "meta.json"
        self.lock_path = self.collab_dir / ".lock"
        self.agents_dir = self.collab_dir / "agents"
        self.tasks_dir = self.collab_dir / "tasks"
        self.messages_dir = self.collab_dir / "messages"
        self.heartbeats_dir = self.collab_dir / "heartbeats"
        self.logs_dir = self.collab_dir / "logs"
        self.runtime_dir = self.collab_dir / "runtime"
        self.sessions_dir = self.runtime_dir / "sessions"
        self.scratchpads_dir = self.runtime_dir / "scratchpads"
        self.events_path = self.logs_dir / "events.jsonl"

    def init(self, force: bool = False) -> dict[str, Any]:
        self.collab_dir.mkdir(parents=True, exist_ok=True)
        with self._lock():
            for directory in (
                self.agents_dir,
                self.tasks_dir,
                self.messages_dir,
                self.heartbeats_dir,
                self.logs_dir,
                self.runtime_dir,
                self.sessions_dir,
                self.scratchpads_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            if force or not self.meta_path.exists():
                self._write_json(self.meta_path, dict(DEFAULT_META))
            if force or not self.events_path.exists():
                self.events_path.write_text("", encoding="utf-8")
        return {
            "root": str(self.root),
            "collab_dir": str(self.collab_dir),
            "initialized_at": utc_now(),
        }

    def register_agent(
        self,
        *,
        name: str,
        kind: str,
        role: str,
        purpose: str = "",
        capabilities: list[str] | None = None,
        status: str = "idle",
    ) -> dict[str, Any]:
        capabilities = sorted(set(capabilities or []))
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            agent_path = self._agent_path(name)
            agent = self._load_json(agent_path, default=None)
            if agent is None:
                agent = {
                    "name": name,
                    "kind": kind,
                    "role": role,
                    "registered_at": now,
                }
            agent.update(
                {
                    "purpose": purpose,
                    "capabilities": capabilities,
                    "status": status,
                    "updated_at": now,
                    "last_seen_at": now,
                }
            )
            self._write_json(agent_path, agent)
            self._append_event(
                event_type="agent.registered",
                actor=name,
                payload={"kind": kind, "role": role, "status": status},
            )
            return agent

    def list_agents(self, *, role: str | None = None) -> list[dict[str, Any]]:
        self._ensure_initialized()
        agents = [self._load_json(path) for path in sorted(self.agents_dir.glob("*.json"))]
        if role:
            agents = [agent for agent in agents if agent["role"] == role]
        return sorted(agents, key=lambda item: item["name"])

    def heartbeat(self, *, agent_name: str, status: str, note: str = "") -> dict[str, Any]:
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            agent = self._require_agent(agent_name)
            agent["status"] = status
            agent["updated_at"] = now
            agent["last_seen_at"] = now
            self._write_json(self._agent_path(agent_name), agent)
            heartbeat = {"agent": agent_name, "status": status, "note": note, "at": now}
            self._write_json(self._heartbeat_path(agent_name), heartbeat)
            self._append_event(
                event_type="agent.heartbeat",
                actor=agent_name,
                payload={"status": status, "note": note},
            )
            return heartbeat

    def create_task(
        self,
        *,
        title: str,
        description: str,
        created_by: str,
        priority: str = "medium",
        labels: list[str] | None = None,
        role_hint: str = "worker",
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        if priority not in TASK_PRIORITIES:
            raise CollabError(f"Unsupported priority: {priority}")
        labels = sorted(set(labels or []))
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            self._require_agent(created_by)
            if assigned_to:
                self._require_agent(assigned_to)
            task_id = self._reserve_task_id()
            task = {
                "id": task_id,
                "title": title,
                "description": description,
                "status": "open",
                "priority": priority,
                "labels": labels,
                "role_hint": role_hint,
                "created_by": created_by,
                "assigned_to": assigned_to,
                "claimed_by": None,
                "created_at": now,
                "updated_at": now,
                "history": [
                    {
                        "at": now,
                        "agent": created_by,
                        "action": "created",
                        "summary": description or title,
                    }
                ],
            }
            self._write_json(self._task_path(task_id), task)
            self._append_event(
                event_type="task.created",
                actor=created_by,
                payload={"task_id": task_id, "title": title, "assigned_to": assigned_to},
            )
            return task

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        claimed_by: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()
        tasks = [self._load_json(path) for path in sorted(self.tasks_dir.glob("*.json"))]
        if status:
            tasks = [task for task in tasks if task["status"] == status]
        if assigned_to:
            tasks = [task for task in tasks if task.get("assigned_to") == assigned_to]
        if claimed_by:
            tasks = [task for task in tasks if task.get("claimed_by") == claimed_by]
        return sorted(tasks, key=lambda item: item["id"])

    def claim_task(self, *, task_id: str, agent_name: str, summary: str = "") -> dict[str, Any]:
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            self._require_agent(agent_name)
            task = self._require_task(task_id)
            current_owner = task.get("claimed_by")
            if current_owner and current_owner != agent_name:
                raise CollabError(f"{task_id} is already claimed by {current_owner}")
            if task["status"] in {"done", "cancelled"}:
                raise CollabError(f"{task_id} is already {task['status']}")
            task["claimed_by"] = agent_name
            task["assigned_to"] = task.get("assigned_to") or agent_name
            task["status"] = "in_progress"
            task["updated_at"] = now
            task["history"].append(
                {
                    "at": now,
                    "agent": agent_name,
                    "action": "claimed",
                    "summary": summary or "Task claimed",
                }
            )
            self._write_json(self._task_path(task_id), task)
            self._append_event(
                event_type="task.claimed",
                actor=agent_name,
                payload={"task_id": task_id, "summary": summary},
            )
            return task

    def update_task(
        self,
        *,
        task_id: str,
        agent_name: str,
        status: str | None = None,
        summary: str = "",
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        if status and status not in TASK_STATUSES:
            raise CollabError(f"Unsupported status: {status}")
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            self._require_agent(agent_name)
            task = self._require_task(task_id)
            if assigned_to:
                self._require_agent(assigned_to)
            if not status and not summary and assigned_to is None:
                raise CollabError("task update requires at least one change")
            if status:
                task["status"] = status
            if assigned_to is not None:
                task["assigned_to"] = assigned_to
            task["updated_at"] = now
            task["history"].append(
                {
                    "at": now,
                    "agent": agent_name,
                    "action": "updated",
                    "summary": summary or status or "Task updated",
                }
            )
            self._write_json(self._task_path(task_id), task)
            self._append_event(
                event_type="task.updated",
                actor=agent_name,
                payload={
                    "task_id": task_id,
                    "status": task["status"],
                    "assigned_to": task.get("assigned_to"),
                    "summary": summary,
                },
            )
            return task

    def send_message(
        self,
        *,
        from_agent: str,
        to_agent: str,
        subject: str,
        body: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock():
            self._ensure_initialized()
            self._require_agent(from_agent)
            self._require_agent(to_agent)
            if task_id:
                self._require_task(task_id)
            message = {
                "id": f"MSG-{uuid.uuid4().hex[:8]}",
                "from_agent": from_agent,
                "to_agent": to_agent,
                "subject": subject,
                "body": body,
                "task_id": task_id,
                "created_at": now,
                "read_at": None,
            }
            self._write_json(self.messages_dir / f"{message['id']}.json", message)
            self._append_event(
                event_type="message.sent",
                actor=from_agent,
                payload={"message_id": message["id"], "to_agent": to_agent, "task_id": task_id},
            )
            return message

    def inbox(
        self,
        *,
        agent_name: str,
        unread_only: bool = False,
        mark_read: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()
        with self._lock():
            self._require_agent(agent_name)
            messages: list[dict[str, Any]] = []
            read_count = 0
            read_at = utc_now() if mark_read else None
            for path in sorted(self.messages_dir.glob("*.json")):
                message = self._load_json(path)
                if message["to_agent"] != agent_name:
                    continue
                if unread_only and message["read_at"] is not None:
                    continue
                if mark_read and message["read_at"] is None:
                    message["read_at"] = read_at
                    self._write_json(path, message)
                    read_count += 1
                messages.append(message)
            if read_count:
                self._append_event(
                    event_type="message.read",
                    actor=agent_name,
                    payload={"count": read_count},
                )
            return sorted(messages, key=lambda item: item["created_at"])

    def board(self) -> dict[str, Any]:
        self._ensure_initialized()
        tasks = self.list_tasks()
        agents = self.list_agents()
        unread_counts = Counter()
        for message_path in self.messages_dir.glob("*.json"):
            message = self._load_json(message_path)
            if message["read_at"] is None:
                unread_counts[message["to_agent"]] += 1
        return {
            "root": str(self.root),
            "generated_at": utc_now(),
            "task_counts": dict(Counter(task["status"] for task in tasks)),
            "tasks": tasks,
            "agents": [
                {
                    **agent,
                    "unread_messages": unread_counts.get(agent["name"], 0),
                }
                for agent in agents
            ],
        }

    def events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_initialized()
        if not self.events_path.exists():
            return []
        with self.events_path.open("r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        return records[-limit:]

    def save_session(self, *, agent_name: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock():
            self._ensure_initialized()
            current = self._load_json(self._session_path(agent_name), default={})
            current.update(data)
            self._write_json(self._session_path(agent_name), current)
            return current

    def get_session(self, *, agent_name: str) -> dict[str, Any] | None:
        self._ensure_initialized()
        return self._load_json(self._session_path(agent_name), default=None)

    def list_sessions(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        sessions = [self._load_json(path) for path in sorted(self.sessions_dir.glob("*.json"))]
        return sorted(sessions, key=lambda item: item["agent_name"])

    def scratchpad_path(self, *, agent_name: str) -> Path:
        return self.scratchpads_dir / f"{agent_name}.log"

    def append_scratchpad(
        self,
        *,
        agent_name: str,
        channel: str,
        text: str,
    ) -> Path:
        with self._lock():
            self._ensure_initialized()
            path = self.scratchpad_path(agent_name=agent_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {utc_now()} {channel} ===\n")
                handle.write(clean_terminal_text(text).rstrip())
                handle.write("\n")
            return path

    def read_scratchpad(self, *, agent_name: str) -> str:
        self._ensure_initialized()
        path = self.scratchpad_path(agent_name=agent_name)
        if not path.exists():
            raise CollabError(f"No scratchpad exists for agent: {agent_name}")
        return clean_terminal_text(path.read_text(encoding="utf-8"))

    def _reserve_task_id(self) -> str:
        meta = self._load_json(self.meta_path, default=dict(DEFAULT_META))
        next_task_number = int(meta.get("next_task_number", 1))
        meta["next_task_number"] = next_task_number + 1
        self._write_json(self.meta_path, meta)
        return f"TASK-{next_task_number:03d}"

    def _require_agent(self, name: str) -> dict[str, Any]:
        agent = self._load_json(self._agent_path(name), default=None)
        if agent is None:
            raise CollabError(f"Unknown agent: {name}")
        return agent

    def _require_task(self, task_id: str) -> dict[str, Any]:
        task = self._load_json(self._task_path(task_id), default=None)
        if task is None:
            raise CollabError(f"Unknown task: {task_id}")
        return task

    def _agent_path(self, name: str) -> Path:
        return self.agents_dir / f"{name}.json"

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _heartbeat_path(self, name: str) -> Path:
        return self.heartbeats_dir / f"{name}.json"

    def _session_path(self, name: str) -> Path:
        return self.sessions_dir / f"{name}.json"

    @contextmanager
    def _lock(self) -> Any:
        self.collab_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _ensure_initialized(self) -> None:
        if not self.meta_path.exists():
            raise CollabError("Repository is not initialized. Run `vibe-collab init` first.")

    def _load_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            temp_name = handle.name
        os.replace(temp_name, path)

    def _append_event(self, *, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "at": utc_now(),
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
