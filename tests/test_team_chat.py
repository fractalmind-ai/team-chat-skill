from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "team-chat" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from service import TeamChatService  # noqa: E402


class TeamChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = TeamChatService(self.root)
        self.team = "demo"
        self.service.init_team(self.team, members=["lead", "dev", "qa"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_send_read_ack_flow(self) -> None:
        result = self.service.send(
            self.team,
            {
                "id": "msg_flow_1",
                "type": "task_assign",
                "from": "lead",
                "to": "dev",
                "task_id": "task_1",
                "trace_id": "trace_1",
                "payload": {"subject": "Build endpoint"},
            },
        )
        self.assertEqual("sent", result["status"])

        inbox = self.service.read(self.team, agent="dev", unread_only=True, limit=20)
        self.assertEqual(1, inbox["count"])
        message_id = inbox["messages"][0]["id"]

        ack = self.service.ack(self.team, agent="dev", message_id=message_id)
        self.assertEqual("acked", ack["status"])

        unread_after = self.service.read(self.team, agent="dev", unread_only=True, limit=20)
        self.assertEqual(0, unread_after["count"])

    def test_duplicate_message_id_is_idempotent(self) -> None:
        envelope = {
            "id": "msg_duplicate_1",
            "type": "idle_notification",
            "from": "dev",
            "to": "lead",
            "payload": {"state": "idle"},
        }
        first = self.service.send(self.team, envelope)
        second = self.service.send(self.team, envelope)

        self.assertEqual("sent", first["status"])
        self.assertEqual("duplicate", second["status"])

        inbox = self.service.read(self.team, agent="lead", unread_only=False, limit=20)
        self.assertEqual(1, inbox["count"])

    def test_task_snapshot_updates_with_task_update(self) -> None:
        self.service.send(
            self.team,
            {
                "id": "msg_task_assign_1",
                "type": "task_assign",
                "from": "lead",
                "to": "dev",
                "task_id": "task_alpha",
                "trace_id": "trace_alpha",
                "payload": {"subject": "Implement core"},
            },
        )

        self.service.send(
            self.team,
            {
                "id": "msg_task_update_1",
                "type": "task_update",
                "from": "dev",
                "to": "lead",
                "task_id": "task_alpha",
                "trace_id": "trace_alpha",
                "payload": {"status": "blocked", "blocked": True, "eta": "2h"},
            },
        )

        status = self.service.status(self.team, stale_minutes=90)
        blocked_ids = [task.get("task_id") for task in status["blocked_tasks"]]
        self.assertIn("task_alpha", blocked_ids)

    def test_ack_timeout_retries_to_dead_letter(self) -> None:
        result = self.service.send(
            self.team,
            {
                "id": "msg_ack_timeout_1",
                "type": "decision_required",
                "from": "lead",
                "to": "qa",
                "payload": {"question": "approve release?"},
            },
            require_ack=True,
            ack_timeout_seconds=1,
            max_retries=1,
        )

        self.assertEqual("dead_letter", result["status"])
        store = self.service.store(self.team)
        dlq = store.list_dead_letters()
        self.assertEqual(1, len(dlq))
        self.assertEqual("msg_ack_timeout_1", dlq[0]["message_id"])

    def test_cooldown_suppresses_spam_message(self) -> None:
        envelope = {
            "id": "msg_cooldown_1",
            "type": "idle_notification",
            "from": "dev",
            "to": "lead",
            "payload": {"state": "idle"},
        }
        first = self.service.send(self.team, envelope, cooldown_seconds=120)
        second = self.service.send(
            self.team,
            {
                "id": "msg_cooldown_2",
                "type": "idle_notification",
                "from": "dev",
                "to": "lead",
                "payload": {"state": "idle"},
            },
            cooldown_seconds=120,
        )

        self.assertEqual("sent", first["status"])
        self.assertEqual("suppressed", second["status"])
        inbox = self.service.read(self.team, agent="lead", unread_only=False, limit=20)
        self.assertEqual(1, inbox["count"])

    def test_trace_filters_events(self) -> None:
        self.service.send(
            self.team,
            {
                "id": "msg_trace_a",
                "type": "handoff",
                "from": "lead",
                "to": "qa",
                "trace_id": "trace_a",
                "payload": {"note": "handoff"},
            },
        )
        self.service.send(
            self.team,
            {
                "id": "msg_trace_b",
                "type": "handoff",
                "from": "lead",
                "to": "dev",
                "trace_id": "trace_b",
                "payload": {"note": "handoff"},
            },
        )

        trace = self.service.trace(self.team, trace_id="trace_a")
        self.assertGreaterEqual(trace["count"], 1)
        for event in trace["events"]:
            payload_message = event.get("payload", {}).get("message", {})
            if payload_message:
                self.assertEqual("trace_a", payload_message.get("trace_id"))

    def test_rejects_team_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.service.init_team("../escape", members=["lead"])

    def test_rejects_agent_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.service.read(self.team, agent="../escape", unread_only=False, limit=10)

    def test_rejects_sender_or_recipient_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.service.send(
                self.team,
                {
                    "id": "msg_bad_1",
                    "type": "handoff",
                    "from": "../lead",
                    "to": "dev",
                    "payload": {"note": "bad"},
                },
            )
        with self.assertRaises(ValueError):
            self.service.send(
                self.team,
                {
                    "id": "msg_bad_2",
                    "type": "handoff",
                    "from": "lead",
                    "to": "../../dev",
                    "payload": {"note": "bad"},
                },
            )


if __name__ == "__main__":
    unittest.main()
