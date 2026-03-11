from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collaborative_vibe_coder.cli import main, render
from collaborative_vibe_coder.session import build_agent_command
from collaborative_vibe_coder.supervise import default_interval_for_style
from collaborative_vibe_coder.store import CollabStore, clean_terminal_text


class CollaborationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--root", str(self.root), *args])
        return code, stdout.getvalue().strip(), stderr.getvalue().strip()

    def test_full_monitor_worker_flow(self) -> None:
        code, _, _ = self.run_cli("init")
        self.assertEqual(code, 0)

        code, _, _ = self.run_cli(
            "agent",
            "register",
            "--name",
            "codex-monitor",
            "--kind",
            "codex",
            "--role",
            "monitor",
            "--purpose",
            "dispatch work",
        )
        self.assertEqual(code, 0)

        code, _, _ = self.run_cli(
            "agent",
            "register",
            "--name",
            "claude-worker-1",
            "--kind",
            "claude",
            "--role",
            "worker",
            "--capability",
            "backend",
            "--capability",
            "tests",
        )
        self.assertEqual(code, 0)

        code, output, _ = self.run_cli(
            "--json",
            "task",
            "create",
            "--created-by",
            "codex-monitor",
            "--title",
            "Build collaboration board",
            "--description",
            "Set up shared repo-local state",
            "--priority",
            "high",
            "--assign-to",
            "claude-worker-1",
        )
        self.assertEqual(code, 0)
        task = json.loads(output)
        self.assertEqual(task["id"], "TASK-001")

        code, _, _ = self.run_cli(
            "task",
            "claim",
            "--task",
            "TASK-001",
            "--agent",
            "claude-worker-1",
            "--summary",
            "Starting work",
        )
        self.assertEqual(code, 0)

        code, _, _ = self.run_cli(
            "heartbeat",
            "--agent",
            "claude-worker-1",
            "--status",
            "active",
            "--note",
            "Working on task",
        )
        self.assertEqual(code, 0)

        code, _, _ = self.run_cli(
            "message",
            "send",
            "--from-agent",
            "codex-monitor",
            "--to-agent",
            "claude-worker-1",
            "--subject",
            "Need milestone update",
            "--body",
            "Send a note before changing the task state.",
            "--task",
            "TASK-001",
        )
        self.assertEqual(code, 0)

        code, output, _ = self.run_cli("--json", "board")
        self.assertEqual(code, 0)
        board = json.loads(output)
        self.assertEqual(board["task_counts"]["in_progress"], 1)
        worker = next(agent for agent in board["agents"] if agent["name"] == "claude-worker-1")
        self.assertEqual(worker["unread_messages"], 1)

    def test_claim_conflict_is_rejected(self) -> None:
        store = CollabStore(self.root)
        store.init()
        store.register_agent(name="codex-monitor", kind="codex", role="monitor")
        store.register_agent(name="codex-worker-1", kind="codex", role="worker")
        store.register_agent(name="claude-worker-1", kind="claude", role="worker")
        task = store.create_task(
            title="Parallel ownership test",
            description="Only one worker should own the task",
            created_by="codex-monitor",
        )
        store.claim_task(task_id=task["id"], agent_name="codex-worker-1")

        code, _, stderr = self.run_cli(
            "task",
            "claim",
            "--task",
            task["id"],
            "--agent",
            "claude-worker-1",
        )
        self.assertEqual(code, 1)
        self.assertIn("already claimed", stderr)

    def test_marking_inbox_as_read_updates_messages(self) -> None:
        store = CollabStore(self.root)
        store.init()
        store.register_agent(name="codex-monitor", kind="codex", role="monitor")
        store.register_agent(name="claude-worker-1", kind="claude", role="worker")
        store.send_message(
            from_agent="codex-monitor",
            to_agent="claude-worker-1",
            subject="Hello",
            body="Please pick up the task.",
        )

        code, output, _ = self.run_cli("--json", "message", "inbox", "--agent", "claude-worker-1", "--mark-read")
        self.assertEqual(code, 0)
        inbox = json.loads(output)
        self.assertEqual(len(inbox), 1)
        self.assertIsNotNone(inbox[0]["read_at"])

    def test_session_launch_list_logs_and_stop(self) -> None:
        session_name = f"test-vibe-{uuid.uuid4().hex[:8]}"
        agent_name = f"shell-worker-{uuid.uuid4().hex[:6]}"
        try:
            code, _, _ = self.run_cli("init")
            self.assertEqual(code, 0)

            code, output, stderr = self.run_cli(
                "--json",
                "session",
                "launch",
                "--name",
                agent_name,
                "--kind",
                "codex",
                "--role",
                "worker",
                "--tmux-session",
                session_name,
                "--command",
                "python3 -c \"import time; print('session-ready', flush=True); time.sleep(30)\"",
            )
            self.assertEqual(code, 0, msg=stderr)
            launch_payload = json.loads(output)
            self.assertEqual(launch_payload["tmux_session"], session_name)

            code, output, _ = self.run_cli("--json", "session", "list")
            self.assertEqual(code, 0)
            sessions = json.loads(output)
            record = next(item for item in sessions if item["agent_name"] == agent_name)
            self.assertTrue(record["tmux_active"])

            code, output, _ = self.run_cli("--json", "session", "logs", "--agent", agent_name, "--lines", "20")
            self.assertEqual(code, 0)
            logs = json.loads(output)
            self.assertIn("session-ready", logs["output"])

            code, output, _ = self.run_cli("--json", "session", "stop", "--agent", agent_name)
            self.assertEqual(code, 0)
            stopped = json.loads(output)
            self.assertFalse(stopped["tmux_active"])
        finally:
            self.run_cli("session", "stop", "--agent", agent_name)

    def test_session_send_and_monitor_tick(self) -> None:
        worker_session = f"worker-{uuid.uuid4().hex[:8]}"
        monitor_session = f"monitor-{uuid.uuid4().hex[:8]}"
        worker_agent = f"worker-{uuid.uuid4().hex[:6]}"
        monitor_agent = f"monitor-{uuid.uuid4().hex[:6]}"
        try:
            code, _, _ = self.run_cli("init")
            self.assertEqual(code, 0)

            code, _, _ = self.run_cli(
                "agent",
                "register",
                "--name",
                "human-owner",
                "--kind",
                "human",
                "--role",
                "requester",
            )
            self.assertEqual(code, 0)

            code, output, _ = self.run_cli(
                "--json",
                "task",
                "create",
                "--created-by",
                "human-owner",
                "--title",
                "Implement feature A",
                "--description",
                "Feature A must satisfy the requirement.",
            )
            self.assertEqual(code, 0)
            task = json.loads(output)

            code, _, _ = self.run_cli(
                "session",
                "launch",
                "--name",
                worker_agent,
                "--kind",
                "codex",
                "--role",
                "worker",
                "--tmux-session",
                worker_session,
                "--command",
                "cat",
            )
            self.assertEqual(code, 0)

            code, _, _ = self.run_cli(
                "session",
                "launch",
                "--name",
                monitor_agent,
                "--kind",
                "codex",
                "--role",
                "monitor",
                "--tmux-session",
                monitor_session,
                "--watch-worker",
                worker_agent,
                "--task",
                task["id"],
                "--goal",
                "Feature A must satisfy the requirement.",
                "--command",
                "cat",
            )
            self.assertEqual(code, 0)

            code, output, _ = self.run_cli(
                "--json",
                "session",
                "send",
                "--agent",
                worker_agent,
                "--text",
                "Please update the API shape.",
            )
            self.assertEqual(code, 0)
            send_payload = json.loads(output)
            self.assertIn("Please update the API shape.", send_payload["text"])

            code, output, _ = self.run_cli("--json", "session", "logs", "--agent", worker_agent, "--lines", "20")
            self.assertEqual(code, 0)
            worker_logs = json.loads(output)
            self.assertIn("Please update the API shape.", worker_logs["output"])

            code, output, _ = self.run_cli("--json", "session", "scratchpad", "--agent", worker_agent, "--lines", "80")
            self.assertEqual(code, 0)
            worker_scratchpad = json.loads(output)
            self.assertIn("Please update the API shape.", worker_scratchpad["output"])
            self.assertTrue(worker_scratchpad["scratchpad_path"].endswith(f"{worker_agent}.log"))

            code, output, _ = self.run_cli(
                "--json",
                "monitor",
                "tick",
                "--monitor",
                monitor_agent,
                "--worker",
                worker_agent,
                "--task",
                task["id"],
                "--goal",
                "Feature A must satisfy the requirement.",
                "--worker-log-lines",
                "20",
            )
            self.assertEqual(code, 0)
            tick_payload = json.loads(output)
            self.assertFalse(tick_payload["completed"])
            self.assertIn("Feature A must satisfy the requirement.", tick_payload["prompt_preview"])

            code, output, _ = self.run_cli("--json", "session", "logs", "--agent", monitor_agent, "--lines", "80")
            self.assertEqual(code, 0)
            monitor_logs = json.loads(output)
            self.assertIn(worker_agent, monitor_logs["output"])
            self.assertIn("WORKER LOGS", monitor_logs["output"])

            code, output, _ = self.run_cli("--json", "session", "scratchpad", "--agent", monitor_agent, "--lines", "120")
            self.assertEqual(code, 0)
            monitor_scratchpad = json.loads(output)
            self.assertIn("WORKER LOGS", monitor_scratchpad["output"])
            self.assertIn(worker_agent, monitor_scratchpad["output"])
            self.assertTrue(monitor_scratchpad["scratchpad_path"].endswith(f"{monitor_agent}.log"))
        finally:
            self.run_cli("session", "stop", "--agent", worker_agent)
            self.run_cli("session", "stop", "--agent", monitor_agent)

    def test_build_agent_command_supports_full_access_search_and_env(self) -> None:
        command = build_agent_command(
            kind="codex",
            root=self.root,
            prompt="hello",
            model="gpt-5",
            env_vars=["CUDA_VISIBLE_DEVICES=6", "PYTHONUNBUFFERED=1"],
            full_access=True,
            enable_search=True,
        )
        self.assertEqual(command[:3], ["env", "CUDA_VISIBLE_DEVICES=6", "PYTHONUNBUFFERED=1"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--search", command)
        self.assertIn("hello", command)

    def test_terminal_text_and_json_render_keep_chinese_readable(self) -> None:
        dirty = "\x1b[39;49m你好\x1b[0m\r\nworld"
        self.assertEqual(clean_terminal_text(dirty), "你好\nworld")
        rendered = render({"message": "你好"}, as_json=True)
        self.assertIn("你好", rendered)
        self.assertNotIn("\\u4f60", rendered)

    def test_supervise_start_accepts_check_interval_alias(self) -> None:
        worker_name = f"codex-worker-{uuid.uuid4().hex[:8]}"
        monitor_name = f"codex-monitor-{uuid.uuid4().hex[:8]}"
        orchestrator_session = f"orchestrator-{uuid.uuid4().hex[:8]}"
        try:
            code, output, stderr = self.run_cli(
                "--json",
                "supervise",
                "start",
                "--worker-actions",
                "Run the benchmark and fix issues.",
                "--monitor-goal",
                "Reach the benchmark target.",
                "--worker-command",
                "cat",
                "--monitor-command",
                "cat",
                "--check-interval-seconds",
                "123",
                "--worker-name",
                worker_name,
                "--monitor-name",
                monitor_name,
                "--orchestrator-session",
                orchestrator_session,
                "--no-loop",
            )
            self.assertEqual(code, 0, msg=stderr)
            payload = json.loads(output)
            self.assertEqual(payload["monitor_style"], "macro")
            self.assertEqual(payload["interval_seconds"], 123)
            self.assertEqual(payload["task_id"], "TASK-001")
            self.assertEqual(payload["worker_actions"], "Run the benchmark and fix issues.")
            self.assertEqual(payload["monitor_goal"], "Reach the benchmark target.")
            self.assertTrue(payload["scratchpad"])
            self.assertTrue(payload["worker_scratchpad_path"].endswith(f"{worker_name}.log"))
            self.assertTrue(payload["monitor_scratchpad_path"].endswith(f"{monitor_name}.log"))

            code, output, _ = self.run_cli("--json", "session", "list")
            self.assertEqual(code, 0)
            sessions = json.loads(output)
            names = {item["agent_name"] for item in sessions}
            self.assertIn(worker_name, names)
            self.assertIn(monitor_name, names)
        finally:
            self.run_cli(
                "supervise",
                "stop",
                "--worker-name",
                worker_name,
                "--monitor-name",
                monitor_name,
                "--orchestrator-session",
                orchestrator_session,
            )

    def test_supervise_start_defaults_to_macro_interval(self) -> None:
        code, output, stderr = self.run_cli(
            "--json",
            "supervise",
            "start",
            "--worker-actions",
            "Run the benchmark and fix issues.",
            "--monitor-goal",
            "Reach the benchmark target.",
            "--worker-command",
            "cat",
            "--monitor-command",
            "cat",
            "--worker-name",
            "codex-worker-default",
            "--monitor-name",
            "codex-monitor-default",
            "--orchestrator-session",
            "orchestrator-default-test",
            "--no-loop",
        )
        try:
            self.assertEqual(code, 0, msg=stderr)
            payload = json.loads(output)
            self.assertEqual(payload["monitor_style"], "macro")
            self.assertEqual(payload["interval_seconds"], default_interval_for_style("macro"))
        finally:
            self.run_cli(
                "supervise",
                "stop",
                "--worker-name",
                "codex-worker-default",
                "--monitor-name",
                "codex-monitor-default",
                "--orchestrator-session",
                "orchestrator-default-test",
            )


if __name__ == "__main__":
    unittest.main()
