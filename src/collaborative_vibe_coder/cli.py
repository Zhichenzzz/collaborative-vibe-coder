from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from collaborative_vibe_coder.monitor import MonitorManager
from collaborative_vibe_coder.session import SessionManager
from collaborative_vibe_coder.store import CollabError, CollabStore, TASK_PRIORITIES, TASK_STATUSES
from collaborative_vibe_coder.supervise import SupervisionManager, default_interval_for_style


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe-collab", description="Coordinate multiple coding agents in one repo.")
    parser.add_argument("--root", default=".", help="Repository root. Defaults to the current working tree.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize the repo-local collaboration store.")
    init_parser.add_argument("--force", action="store_true", help="Reset metadata files if they already exist.")

    agent_parser = subparsers.add_parser("agent", help="Manage agent registration.")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    register_parser = agent_subparsers.add_parser("register", help="Register or update an agent.")
    register_parser.add_argument("--name", required=True)
    register_parser.add_argument("--kind", required=True)
    register_parser.add_argument("--role", required=True)
    register_parser.add_argument("--purpose", default="")
    register_parser.add_argument("--capability", action="append", default=[], dest="capabilities")
    register_parser.add_argument("--status", default="idle")

    list_agents_parser = agent_subparsers.add_parser("list", help="List registered agents.")
    list_agents_parser.add_argument("--role")

    heartbeat_parser = subparsers.add_parser("heartbeat", help="Publish a worker heartbeat.")
    heartbeat_parser.add_argument("--agent", required=True, dest="agent_name")
    heartbeat_parser.add_argument("--status", required=True)
    heartbeat_parser.add_argument("--note", default="")

    task_parser = subparsers.add_parser("task", help="Manage tasks.")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)

    create_task_parser = task_subparsers.add_parser("create", help="Create a new task.")
    create_task_parser.add_argument("--title", required=True)
    create_task_parser.add_argument("--description", default="")
    create_task_parser.add_argument("--created-by", required=True)
    create_task_parser.add_argument("--priority", default="medium", choices=sorted(TASK_PRIORITIES))
    create_task_parser.add_argument("--label", action="append", default=[], dest="labels")
    create_task_parser.add_argument("--role-hint", default="worker")
    create_task_parser.add_argument("--assign-to")

    list_task_parser = task_subparsers.add_parser("list", help="List tasks.")
    list_task_parser.add_argument("--status", choices=sorted(TASK_STATUSES))
    list_task_parser.add_argument("--assigned-to")
    list_task_parser.add_argument("--claimed-by")

    claim_task_parser = task_subparsers.add_parser("claim", help="Claim a task for an agent.")
    claim_task_parser.add_argument("--task", required=True, dest="task_id")
    claim_task_parser.add_argument("--agent", required=True, dest="agent_name")
    claim_task_parser.add_argument("--summary", default="")

    update_task_parser = task_subparsers.add_parser("update", help="Update a task status or summary.")
    update_task_parser.add_argument("--task", required=True, dest="task_id")
    update_task_parser.add_argument("--agent", required=True, dest="agent_name")
    update_task_parser.add_argument("--status", choices=sorted(TASK_STATUSES))
    update_task_parser.add_argument("--summary", default="")
    update_task_parser.add_argument("--assign-to")

    message_parser = subparsers.add_parser("message", help="Send or inspect direct messages.")
    message_subparsers = message_parser.add_subparsers(dest="message_command", required=True)

    send_message_parser = message_subparsers.add_parser("send", help="Send a direct message.")
    send_message_parser.add_argument("--from-agent", required=True)
    send_message_parser.add_argument("--to-agent", required=True)
    send_message_parser.add_argument("--subject", required=True)
    send_message_parser.add_argument("--body", required=True)
    send_message_parser.add_argument("--task")

    inbox_parser = message_subparsers.add_parser("inbox", help="Read messages for an agent.")
    inbox_parser.add_argument("--agent", required=True, dest="agent_name")
    inbox_parser.add_argument("--unread-only", action="store_true")
    inbox_parser.add_argument("--mark-read", action="store_true")

    subparsers.add_parser("board", help="Show the repo-wide coordination board.")

    events_parser = subparsers.add_parser("events", help="Show recent coordination events.")
    events_parser.add_argument("--limit", type=int, default=20)

    session_parser = subparsers.add_parser("session", help="Launch and manage live tmux-backed agent sessions.")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)

    launch_session_parser = session_subparsers.add_parser("launch", help="Launch a Codex or Claude session in tmux.")
    launch_session_parser.add_argument("--name", required=True)
    launch_session_parser.add_argument("--kind", required=True, choices=["codex", "claude"])
    launch_session_parser.add_argument("--role", required=True)
    launch_session_parser.add_argument("--purpose", default="")
    launch_session_parser.add_argument("--capability", action="append", default=[], dest="capabilities")
    launch_session_parser.add_argument("--task")
    launch_session_parser.add_argument("--model")
    launch_session_parser.add_argument("--tmux-session")
    launch_session_parser.add_argument("--extra-prompt", default="")
    launch_session_parser.add_argument("--command", dest="command_override")
    launch_session_parser.add_argument("--watch-worker", action="append", default=[], dest="watch_workers")
    launch_session_parser.add_argument("--goal", default="")
    launch_session_parser.add_argument("--interval-seconds", "--check-interval-seconds", dest="interval_seconds", type=int)
    launch_session_parser.add_argument("--env", action="append", default=[], dest="env_vars")
    launch_session_parser.add_argument("--full-access", action="store_true")
    launch_session_parser.add_argument("--search", action="store_true", dest="enable_search")
    launch_session_parser.add_argument("--monitor-style", choices=["macro", "micro"], default="macro")
    launch_session_parser.add_argument("--no-scratchpad", action="store_true")

    session_subparsers.add_parser("list", help="List launched tmux-backed agent sessions.")

    attach_session_parser = session_subparsers.add_parser("attach", help="Attach your terminal to an agent tmux session.")
    attach_session_parser.add_argument("--agent", required=True, dest="agent_name")

    logs_session_parser = session_subparsers.add_parser("logs", help="Capture recent output from an agent tmux session.")
    logs_session_parser.add_argument("--agent", required=True, dest="agent_name")
    logs_session_parser.add_argument("--lines", type=int, default=80)

    send_session_parser = session_subparsers.add_parser("send", help="Send a prompt directly into an agent tmux session.")
    send_session_parser.add_argument("--agent", required=True, dest="agent_name")
    send_session_parser.add_argument("--text", required=True)
    send_session_parser.add_argument("--no-enter", action="store_true")

    scratchpad_session_parser = session_subparsers.add_parser("scratchpad", help="Show the scratchpad log for an agent.")
    scratchpad_session_parser.add_argument("--agent", required=True, dest="agent_name")
    scratchpad_session_parser.add_argument("--lines", type=int, default=120)

    stop_session_parser = session_subparsers.add_parser("stop", help="Stop an agent tmux session.")
    stop_session_parser.add_argument("--agent", required=True, dest="agent_name")

    monitor_parser = subparsers.add_parser("monitor", help="Drive a monitor agent that supervises workers.")
    monitor_subparsers = monitor_parser.add_subparsers(dest="monitor_command", required=True)

    tick_parser = monitor_subparsers.add_parser("tick", help="Send one supervision prompt to a monitor agent.")
    tick_parser.add_argument("--monitor", required=True, dest="monitor_agent")
    tick_parser.add_argument("--worker", action="append", default=[], dest="worker_agents")
    tick_parser.add_argument("--task")
    tick_parser.add_argument("--goal", default="")
    tick_parser.add_argument("--worker-log-lines", type=int, default=60)

    run_parser = monitor_subparsers.add_parser("run", help="Run supervision ticks until the task is done or max ticks is reached.")
    run_parser.add_argument("--monitor", required=True, dest="monitor_agent")
    run_parser.add_argument("--worker", action="append", default=[], dest="worker_agents")
    run_parser.add_argument("--task")
    run_parser.add_argument("--goal", default="")
    run_parser.add_argument("--interval-seconds", "--check-interval-seconds", dest="interval_seconds", type=int)
    run_parser.add_argument("--max-ticks", type=int)
    run_parser.add_argument("--worker-log-lines", type=int, default=60)

    supervise_parser = subparsers.add_parser("supervise", help="One-command worker + monitor orchestration.")
    supervise_subparsers = supervise_parser.add_subparsers(dest="supervise_command", required=True)

    supervise_start = supervise_subparsers.add_parser("start", help="Start a worker, a monitor, and an optional monitor loop.")
    supervise_start.add_argument("--monitor-goal", dest="monitor_goal")
    supervise_start.add_argument("--goal", dest="monitor_goal_compat")
    supervise_start.add_argument("--worker-actions", default="")
    supervise_start.add_argument("--task-title", default="")
    supervise_start.add_argument("--task")
    supervise_start.add_argument("--requester-name", default="human-owner")
    supervise_start.add_argument("--worker-name", default="codex-worker-1")
    supervise_start.add_argument("--worker-kind", choices=["codex", "claude"], default="codex")
    supervise_start.add_argument("--monitor-name", default="codex-monitor")
    supervise_start.add_argument("--monitor-kind", choices=["codex", "claude"], default="codex")
    supervise_start.add_argument("--gpu")
    supervise_start.add_argument("--env", action="append", default=[], dest="env_vars")
    supervise_start.add_argument("--interval-seconds", "--check-interval-seconds", dest="interval_seconds", type=int)
    supervise_start.add_argument("--monitor-style", choices=["macro", "micro"], default="macro")
    supervise_start.add_argument("--worker-prompt", default="")
    supervise_start.add_argument("--monitor-prompt", default="")
    supervise_start.add_argument("--no-full-access", action="store_true")
    supervise_start.add_argument("--no-search", action="store_true")
    supervise_start.add_argument("--no-scratchpad", action="store_true")
    supervise_start.add_argument("--no-loop", action="store_true")
    supervise_start.add_argument("--orchestrator-session")
    supervise_start.add_argument("--worker-command")
    supervise_start.add_argument("--monitor-command")
    supervise_start.add_argument("--max-ticks", type=int)

    supervise_stop = supervise_subparsers.add_parser("stop", help="Stop the default worker, monitor, and orchestrator.")
    supervise_stop.add_argument("--worker-name", default="codex-worker-1")
    supervise_stop.add_argument("--monitor-name", default="codex-monitor")
    supervise_stop.add_argument("--orchestrator-session")

    return parser


def render(value: Any, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "No records found."
        if value and "event_type" in value[0]:
            return _render_events(value)
        if value and "tmux_session" in value[0]:
            return _render_sessions(value)
        if value and "to_agent" in value[0]:
            return _render_messages(value)
        if value and "priority" in value[0]:
            return _render_tasks(value)
        if value and "role" in value[0]:
            return _render_agents(value)
    if isinstance(value, dict):
        if "task_counts" in value and "agents" in value:
            return _render_board(value)
        if "scratchpad_path" in value and "output" in value:
            return _render_scratchpad(value)
        if "worker_name" in value and "monitor_name" in value and "task_id" in value:
            return _render_supervision_result(value)
        if "sent_at" in value and "tmux_session" in value and "text" in value:
            return _render_session_send(value)
        if "output" in value and "tmux_session" in value:
            return _render_session_logs(value)
        if "tmux_session" in value and "agent_name" in value:
            return _render_sessions([value])
        if "to_agent" in value:
            return _render_messages([value])
        if "priority" in value:
            return _render_tasks([value])
        if "role" in value:
            return _render_agents([value])
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _render_agents(agents: list[dict[str, Any]]) -> str:
    lines = []
    for agent in agents:
        capabilities = ",".join(agent.get("capabilities", [])) or "-"
        lines.append(
            f"{agent['name']} [{agent['kind']}/{agent['role']}] "
            f"status={agent.get('status', '-')}"
        )
        lines.append(
            f"  last_seen={agent.get('last_seen_at', '-')} capabilities={capabilities}"
        )
        if agent.get("purpose"):
            lines.append(f"  purpose={agent['purpose']}")
        if "unread_messages" in agent:
            lines.append(f"  unread_messages={agent['unread_messages']}")
    return "\n".join(lines)


def _render_tasks(tasks: list[dict[str, Any]]) -> str:
    lines = []
    for task in tasks:
        labels = ",".join(task.get("labels", [])) or "-"
        history = task.get("history", [])
        last_summary = history[-1]["summary"] if history else "-"
        lines.append(
            f"{task['id']} [{task['status']}/{task['priority']}] {task['title']}"
        )
        lines.append(
            "  "
            f"assigned_to={task.get('assigned_to') or '-'} "
            f"claimed_by={task.get('claimed_by') or '-'} labels={labels}"
        )
        lines.append(f"  last_update={task['updated_at']} summary={last_summary}")
    return "\n".join(lines)


def _render_messages(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        task_fragment = f" task={message['task_id']}" if message.get("task_id") else ""
        read_state = "read" if message.get("read_at") else "unread"
        lines.append(
            f"{message['id']} {message['from_agent']} -> {message['to_agent']}{task_fragment} [{read_state}]"
        )
        lines.append(f"  subject={message['subject']}")
        lines.append(f"  body={message['body']}")
    return "\n".join(lines)


def _render_events(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        lines.append(f"{event['at']} {event['actor']} {event['event_type']}")
        if event.get("payload"):
            lines.append(f"  payload={json.dumps(event['payload'], sort_keys=True, ensure_ascii=False)}")
    return "\n".join(lines)


def _render_board(board: dict[str, Any]) -> str:
    task_counts = board.get("task_counts", {})
    summary = ", ".join(f"{key}={task_counts[key]}" for key in sorted(task_counts)) or "no tasks"
    lines = [f"Board @ {board['root']}", f"Task counts: {summary}", "", "Agents:"]
    if board["agents"]:
        lines.append(_render_agents(board["agents"]))
    else:
        lines.append("No records found.")
    lines.extend(["", "Tasks:"])
    if board["tasks"]:
        lines.append(_render_tasks(board["tasks"]))
    else:
        lines.append("No records found.")
    return "\n".join(lines)


def _render_sessions(sessions: list[dict[str, Any]]) -> str:
    lines = []
    for session in sessions:
        task_fragment = f" task={session['task_id']}" if session.get("task_id") else ""
        lines.append(
            f"{session['agent_name']} [{session['kind']}/{session['role']}] "
            f"tmux={session['tmux_session']} status={session.get('status', '-')}"
            f" active={session.get('tmux_active', False)}{task_fragment}"
        )
        lines.append(f"  attach={session.get('attach_command', '-')}")
        if session.get("scratchpad_path"):
            lines.append(f"  scratchpad={session['scratchpad_path']}")
        if session.get("launch_command"):
            lines.append(f"  launch={session['launch_command']}")
    return "\n".join(lines)


def _render_session_logs(payload: dict[str, Any]) -> str:
    header = (
        f"{payload['agent_name']} tmux={payload['tmux_session']} "
        f"captured_at={payload['captured_at']}"
    )
    if payload.get("output"):
        return f"{header}\n{payload['output']}"
    return f"{header}\n<no output>"


def _render_session_send(payload: dict[str, Any]) -> str:
    return (
        f"{payload['agent_name']} tmux={payload['tmux_session']} sent_at={payload['sent_at']}\n"
        f"{payload['text']}"
    )


def _render_supervision_result(payload: dict[str, Any]) -> str:
    lines = [
        f"root={payload['root']}",
        f"task={payload['task_id']} title={payload.get('task_title', '-')}",
        f"worker_actions={payload.get('worker_actions', '-')}",
        f"monitor_goal={payload.get('monitor_goal', '-')}",
        f"worker={payload['worker_name']} tmux={payload.get('worker_tmux', '-')}",
        f"monitor={payload['monitor_name']} tmux={payload.get('monitor_tmux', '-')}",
        f"monitor_style={payload.get('monitor_style', '-')} interval_seconds={payload.get('interval_seconds', '-')}",
    ]
    if payload.get("worker_scratchpad_path"):
        lines.append(f"worker_scratchpad={payload['worker_scratchpad_path']}")
    if payload.get("monitor_scratchpad_path"):
        lines.append(f"monitor_scratchpad={payload['monitor_scratchpad_path']}")
    if payload.get("orchestrator_session"):
        lines.append(f"orchestrator={payload['orchestrator_session']}")
    return "\n".join(lines)


def _render_scratchpad(payload: dict[str, Any]) -> str:
    header = f"{payload['agent_name']} scratchpad={payload['scratchpad_path']} captured_at={payload['captured_at']}"
    if payload.get("output"):
        return f"{header}\n{payload['output']}"
    return f"{header}\n<empty>"


def execute(args: argparse.Namespace) -> Any:
    store = CollabStore(Path(args.root))
    session_manager = SessionManager(store)
    monitor_manager = MonitorManager(store, session_manager)
    supervision_manager = SupervisionManager(store, session_manager)
    if args.command == "init":
        return store.init(force=args.force)
    if args.command == "agent":
        if args.agent_command == "register":
            return store.register_agent(
                name=args.name,
                kind=args.kind,
                role=args.role,
                purpose=args.purpose,
                capabilities=args.capabilities,
                status=args.status,
            )
        if args.agent_command == "list":
            return store.list_agents(role=args.role)
    if args.command == "heartbeat":
        return store.heartbeat(
            agent_name=args.agent_name,
            status=args.status,
            note=args.note,
        )
    if args.command == "task":
        if args.task_command == "create":
            return store.create_task(
                title=args.title,
                description=args.description,
                created_by=args.created_by,
                priority=args.priority,
                labels=args.labels,
                role_hint=args.role_hint,
                assigned_to=args.assign_to,
            )
        if args.task_command == "list":
            return store.list_tasks(
                status=args.status,
                assigned_to=args.assigned_to,
                claimed_by=args.claimed_by,
            )
        if args.task_command == "claim":
            return store.claim_task(
                task_id=args.task_id,
                agent_name=args.agent_name,
                summary=args.summary,
            )
        if args.task_command == "update":
            return store.update_task(
                task_id=args.task_id,
                agent_name=args.agent_name,
                status=args.status,
                summary=args.summary,
                assigned_to=args.assign_to,
            )
    if args.command == "message":
        if args.message_command == "send":
            return store.send_message(
                from_agent=args.from_agent,
                to_agent=args.to_agent,
                subject=args.subject,
                body=args.body,
                task_id=args.task,
            )
        if args.message_command == "inbox":
            return store.inbox(
                agent_name=args.agent_name,
                unread_only=args.unread_only,
                mark_read=args.mark_read,
            )
    if args.command == "board":
        return store.board()
    if args.command == "events":
        return store.events(limit=args.limit)
    if args.command == "session":
        if args.session_command == "launch":
            return session_manager.launch(
                agent_name=args.name,
                kind=args.kind,
                role=args.role,
                purpose=args.purpose,
                capabilities=args.capabilities,
                task_id=args.task,
                model=args.model,
                tmux_session=args.tmux_session,
                extra_prompt=args.extra_prompt,
                command_override=args.command_override,
                watch_workers=args.watch_workers,
                goal=args.goal,
                interval_seconds=args.interval_seconds,
                env_vars=args.env_vars,
                full_access=args.full_access,
                enable_search=args.enable_search,
                monitor_style=args.monitor_style,
                scratchpad=not args.no_scratchpad,
            )
        if args.session_command == "list":
            return session_manager.list_sessions()
        if args.session_command == "logs":
            return session_manager.logs(agent_name=args.agent_name, lines=args.lines)
        if args.session_command == "scratchpad":
            return session_manager.scratchpad(agent_name=args.agent_name, lines=args.lines)
        if args.session_command == "send":
            return session_manager.send(
                agent_name=args.agent_name,
                text=args.text,
                press_enter=not args.no_enter,
            )
        if args.session_command == "stop":
            return session_manager.stop(agent_name=args.agent_name)
        if args.session_command == "attach":
            return {"exit_code": session_manager.attach(agent_name=args.agent_name)}
    if args.command == "monitor":
        if args.monitor_command == "tick":
            return monitor_manager.tick(
                monitor_agent=args.monitor_agent,
                worker_agents=args.worker_agents,
                task_id=args.task,
                goal=args.goal,
                interval_seconds=None,
                worker_log_lines=args.worker_log_lines,
            )
        if args.monitor_command == "run":
            return monitor_manager.run(
                monitor_agent=args.monitor_agent,
                worker_agents=args.worker_agents,
                task_id=args.task,
                goal=args.goal,
                interval_seconds=args.interval_seconds,
                max_ticks=args.max_ticks,
                worker_log_lines=args.worker_log_lines,
            )
    if args.command == "supervise":
        if args.supervise_command == "start":
            monitor_goal = args.monitor_goal or args.monitor_goal_compat
            if not monitor_goal:
                raise CollabError("supervise start requires --monitor-goal")
            interval_seconds = args.interval_seconds or default_interval_for_style(args.monitor_style)
            return supervision_manager.start(
                monitor_goal=monitor_goal,
                worker_actions=args.worker_actions,
                task_title=args.task_title,
                task_id=args.task,
                requester_name=args.requester_name,
                worker_name=args.worker_name,
                worker_kind=args.worker_kind,
                monitor_name=args.monitor_name,
                monitor_kind=args.monitor_kind,
                gpu=args.gpu,
                env_vars=args.env_vars,
                interval_seconds=interval_seconds,
                monitor_style=args.monitor_style,
                worker_prompt=args.worker_prompt,
                monitor_prompt=args.monitor_prompt,
                full_access=not args.no_full_access,
                enable_search=not args.no_search,
                scratchpad=not args.no_scratchpad,
                no_loop=args.no_loop,
                orchestrator_session=args.orchestrator_session,
                worker_command=args.worker_command,
                monitor_command=args.monitor_command,
                max_ticks=args.max_ticks,
            )
        if args.supervise_command == "stop":
            return supervision_manager.stop(
                worker_name=args.worker_name,
                monitor_name=args.monitor_name,
                orchestrator_session=args.orchestrator_session,
            )
    raise CollabError("Unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except CollabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render(result, as_json=args.json))
    return 0
