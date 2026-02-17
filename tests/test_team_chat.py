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

    def test_read_cursor_pagination(self) -> None:
        for i in range(1, 6):
            self.service.send(
                self.team,
                {
                    "id": f"msg_page_{i}",
                    "type": "handoff",
                    "from": "lead",
                    "to": "dev",
                    "payload": {"seq": i},
                },
            )

        page1 = self.service.read(self.team, agent="dev", unread_only=False, limit=2)
        self.assertEqual(["msg_page_4", "msg_page_5"], [m["id"] for m in page1["messages"]])
        self.assertEqual("msg_page_4", page1["next_cursor"])

        page2 = self.service.read(
            self.team,
            agent="dev",
            unread_only=False,
            limit=2,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(["msg_page_2", "msg_page_3"], [m["id"] for m in page2["messages"]])
        self.assertEqual("msg_page_2", page2["next_cursor"])

        page3 = self.service.read(
            self.team,
            agent="dev",
            unread_only=False,
            limit=2,
            cursor=page2["next_cursor"],
        )
        self.assertEqual(["msg_page_1"], [m["id"] for m in page3["messages"]])
        self.assertIsNone(page3["next_cursor"])

    def test_trace_cursor_pagination(self) -> None:
        for i in range(1, 6):
            self.service.send(
                self.team,
                {
                    "id": f"msg_trace_page_{i}",
                    "type": "handoff",
                    "from": "lead",
                    "to": "qa",
                    "trace_id": "trace_paginated",
                    "payload": {"seq": i},
                },
            )

        page1 = self.service.trace(self.team, trace_id="trace_paginated", limit=2)
        self.assertEqual(2, page1["count"])
        self.assertIsNotNone(page1["next_cursor"])

        page2 = self.service.trace(
            self.team,
            trace_id="trace_paginated",
            limit=2,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(2, page2["count"])
        self.assertIsNotNone(page2["next_cursor"])

        page3 = self.service.trace(
            self.team,
            trace_id="trace_paginated",
            limit=2,
            cursor=page2["next_cursor"],
        )
        self.assertGreaterEqual(page3["count"], 1)
        self.assertIsNone(page3["next_cursor"])

    def test_trace_cursor_pagination_no_duplicates_against_full_trace(self) -> None:
        for i in range(30):
            self.service.send(
                self.team,
                {
                    "id": f"msg_trace_reg_{i}",
                    "type": "handoff",
                    "from": "lead",
                    "to": "dev",
                    "trace_id": "trace_regression",
                    "payload": {"seq": i},
                },
            )

        full = self.service.trace(self.team, trace_id="trace_regression", limit=0)
        full_ids = [event["id"] for event in full["events"]]

        paged_ids: list[str] = []
        cursor = None
        while True:
            page = self.service.trace(
                self.team,
                trace_id="trace_regression",
                limit=7,
                cursor=cursor,
            )
            paged_ids.extend([event["id"] for event in page["events"]])
            cursor = page.get("next_cursor")
            if not cursor:
                break

        self.assertEqual(len(full_ids), len(paged_ids))
        self.assertEqual(len(set(full_ids)), len(set(paged_ids)))
        self.assertEqual(set(full_ids), set(paged_ids))

    def test_ack_fallback_works_without_index_offset(self) -> None:
        self.service.send(
            self.team,
            {
                "id": "msg_no_offset",
                "type": "task_assign",
                "from": "lead",
                "to": "dev",
                "payload": {"subject": "compat"},
            },
        )
        store = self.service.store(self.team)
        shard_path = store._index_shard_path(store.message_index_shards_dir, "msg_no_offset")
        index = store.read_json(shard_path, {})
        entry = index.get("msg_no_offset", {})
        if isinstance(entry, dict) and "offset" in entry:
            del entry["offset"]
        index["msg_no_offset"] = entry
        store.write_json_atomic(shard_path, index)

        ack = self.service.ack(self.team, agent="dev", message_id="msg_no_offset")
        self.assertEqual("acked", ack["status"])

    def test_get_message_falls_back_to_legacy_index_before_migration(self) -> None:
        store = self.service.store(self.team)
        inbox = store.inboxes_dir / "dev.jsonl"
        message = {
            "id": "msg_legacy_index",
            "type": "task_assign",
            "from": "lead",
            "to": "dev",
            "payload": {"subject": "legacy"},
            "created_at": "2026-02-17T00:00:00Z",
            "schema_version": 1,
            "priority": "normal",
        }
        store.append_jsonl(inbox, message)
        store.write_json_atomic(
            store.message_index_path,
            {
                "msg_legacy_index": {
                    "inbox": "dev.jsonl",
                    "created_at": message["created_at"],
                    "to": "dev",
                }
            },
        )

        loaded = store.get_message("msg_legacy_index")
        self.assertIsNotNone(loaded)
        self.assertEqual("msg_legacy_index", loaded["id"])

    def test_send_writes_sharded_indexes_on_hot_path(self) -> None:
        for i in range(60):
            self.service.send(
                self.team,
                {
                    "id": f"msg_hot_{i}",
                    "type": "task_update",
                    "from": "lead",
                    "to": "dev",
                    "task_id": f"task_{i % 5}",
                    "payload": {"status": "working", "i": i},
                },
            )

        store = self.service.store(self.team)
        msg_shards = list(store.message_index_shards_dir.glob("*.json"))
        event_shards = list(store.event_index_shards_dir.glob("*.json"))

        self.assertGreater(len(msg_shards), 1)
        self.assertGreater(len(event_shards), 1)
        self.assertFalse(store.message_index_path.exists())
        self.assertFalse(store.event_index_path.exists())

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

    def test_rejects_task_id_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.service.send(
                self.team,
                {
                    "id": "msg_bad_task_id",
                    "type": "task_assign",
                    "from": "lead",
                    "to": "dev",
                    "task_id": "../../escape",
                    "payload": {"subject": "bad"},
                },
            )

    def test_rehydrate_ignores_malformed_task_id_snapshot_update(self) -> None:
        store = self.service.store(self.team)
        inbox = store.inboxes_dir / "dev.jsonl"
        store.append_jsonl(
            inbox,
            {
                "id": "msg_malformed_task_id",
                "type": "task_assign",
                "from": "lead",
                "to": "dev",
                "task_id": "../../escape",
                "payload": {"subject": "malformed"},
                "created_at": "2026-02-16T00:00:00Z",
                "schema_version": 1,
                "priority": "normal",
            },
        )

        self.service.rehydrate(self.team)

        escaped = self.root / "teams" / "escape.json"
        self.assertFalse(escaped.exists())
        tasks = list((self.root / "teams" / self.team / "tasks").glob("*.json"))
        self.assertEqual([], tasks)


if __name__ == "__main__":
    unittest.main()
