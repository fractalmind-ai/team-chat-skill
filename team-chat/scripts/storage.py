"""Durable file-backed storage helpers for team-chat."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import fcntl

from protocol import parse_iso_utc, validate_identifier


DEFAULT_ACK_POLICY: dict[str, dict[str, int]] = {
    "default": {"ack_timeout_seconds": 60, "max_retries": 2},
    "decision_required": {"ack_timeout_seconds": 180, "max_retries": 3},
    "shutdown_request": {"ack_timeout_seconds": 180, "max_retries": 2},
}


class TeamStore:
    def __init__(self, base_dir: Path, team: str):
        self.base_dir = Path(base_dir)
        self.team = validate_identifier(team, field_name="team")
        self.team_dir = self.base_dir / "teams" / self.team
        self.inboxes_dir = self.team_dir / "inboxes"
        self.events_dir = self.team_dir / "events"
        self.tasks_dir = self.team_dir / "tasks"
        self.state_dir = self.team_dir / "state"
        self.dead_letter_dir = self.team_dir / "dead-letter"
        self.locks_dir = self.team_dir / "locks"
        self.config_path = self.team_dir / "config.json"
        self.team_meta_path = self.team_dir / "team.json"

        self.message_index_path = self.state_dir / "message-index.json"
        self.event_index_path = self.state_dir / "event-index.json"
        self.ack_index_path = self.state_dir / "ack-index.json"
        self.nudge_index_path = self.state_dir / "nudge-index.json"

    def ensure_layout(self) -> None:
        for directory in (
            self.team_dir,
            self.inboxes_dir,
            self.events_dir,
            self.tasks_dir,
            self.state_dir,
            self.dead_letter_dir,
            self.locks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, lock_name: str) -> Iterator[None]:
        self.ensure_layout()
        path = self.locks_dir / f"{lock_name}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return deepcopy(default)

    def write_json_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid4().hex}")
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temp_path.write_text(body, encoding="utf-8")
        os.replace(temp_path, path)

    def append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except Exception:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def load_ack_policy(self) -> dict[str, dict[str, int]]:
        config = self.read_json(self.config_path, {})
        policy = config.get("ack_policy") if isinstance(config, dict) else None
        merged = deepcopy(DEFAULT_ACK_POLICY)
        if isinstance(policy, dict):
            for key, value in policy.items():
                if not isinstance(value, dict):
                    continue
                timeout = value.get("ack_timeout_seconds")
                retries = value.get("max_retries")
                merged[key] = {
                    "ack_timeout_seconds": int(timeout) if isinstance(timeout, int) else merged.get(key, merged["default"])["ack_timeout_seconds"],
                    "max_retries": int(retries) if isinstance(retries, int) else merged.get(key, merged["default"])["max_retries"],
                }
        return merged

    def ack_policy_for_type(self, message_type: str) -> dict[str, int]:
        policy = self.load_ack_policy()
        default = policy["default"]
        specific = policy.get(message_type, {})
        return {
            "ack_timeout_seconds": int(specific.get("ack_timeout_seconds", default["ack_timeout_seconds"])),
            "max_retries": int(specific.get("max_retries", default["max_retries"])),
        }

    def _inbox_path(self, agent: str) -> Path:
        safe_agent = validate_identifier(agent, field_name="agent")
        return self.inboxes_dir / f"{safe_agent}.jsonl"

    def upsert_message(self, message: dict[str, Any]) -> bool:
        self.ensure_layout()
        message_id = str(message["id"])
        agent = str(message["to"])
        inbox_path = self._inbox_path(agent)

        with self.lock("messages"):
            index = self.read_json(self.message_index_path, {})
            if message_id in index:
                return False

            self.append_jsonl(inbox_path, message)
            index[message_id] = {
                "inbox": inbox_path.name,
                "created_at": message.get("created_at"),
                "to": agent,
            }
            self.write_json_atomic(self.message_index_path, index)
            return True

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        index = self.read_json(self.message_index_path, {})
        info = index.get(message_id)
        if not isinstance(info, dict):
            return None

        inbox_name = info.get("inbox")
        if not isinstance(inbox_name, str):
            return None
        records = self.read_jsonl(self.inboxes_dir / inbox_name)
        for record in records:
            if record.get("id") == message_id:
                return record
        return None

    def list_agents(self) -> list[str]:
        self.ensure_layout()
        agents = [path.stem for path in sorted(self.inboxes_dir.glob("*.jsonl"))]
        return agents

    def list_messages_for_agent(self, agent: str, *, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        messages = self.read_jsonl(self._inbox_path(agent))
        ack_index = self.read_json(self.ack_index_path, {})

        if unread_only:
            messages = [msg for msg in messages if str(msg.get("id")) not in ack_index]

        if limit > 0:
            messages = messages[-limit:]
        return messages

    def record_ack(self, message_id: str, *, agent: str, acked_at: str, delivery_id: str | None = None) -> bool:
        with self.lock("acks"):
            index = self.read_json(self.ack_index_path, {})
            if message_id in index:
                return False
            entry: dict[str, Any] = {
                "message_id": message_id,
                "agent": agent,
                "acked_at": acked_at,
            }
            if delivery_id:
                entry["delivery_id"] = delivery_id
            index[message_id] = entry
            self.write_json_atomic(self.ack_index_path, index)
            return True

    def get_ack(self, message_id: str) -> dict[str, Any] | None:
        index = self.read_json(self.ack_index_path, {})
        ack = index.get(message_id)
        return ack if isinstance(ack, dict) else None

    def append_event(self, event: dict[str, Any]) -> bool:
        self.ensure_layout()
        event_id = str(event["id"])
        created_at = str(event.get("created_at", ""))
        date_part = created_at[:10] if len(created_at) >= 10 else "unknown"
        event_path = self.events_dir / f"{date_part}.jsonl"

        with self.lock("events"):
            index = self.read_json(self.event_index_path, {})
            if event_id in index:
                return False

            self.append_jsonl(event_path, event)
            index[event_id] = {"file": event_path.name, "created_at": created_at}
            self.write_json_atomic(self.event_index_path, index)
            return True

    def iter_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in sorted(self.events_dir.glob("*.jsonl")):
            events.extend(self.read_jsonl(path))
        events.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
        return events

    def write_dead_letter(self, entry: dict[str, Any]) -> None:
        created_at = str(entry.get("created_at", ""))
        date_part = created_at[:10] if len(created_at) >= 10 else "unknown"
        path = self.dead_letter_dir / f"{date_part}.jsonl"
        with self.lock("dead-letter"):
            self.append_jsonl(path, entry)

    def list_dead_letters(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.dead_letter_dir.glob("*.jsonl")):
            records.extend(self.read_jsonl(path))
        records.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
        return records

    def write_task_snapshot(self, task_id: str, payload: dict[str, Any]) -> None:
        path = self.tasks_dir / f"{task_id}.json"
        self.write_json_atomic(path, payload)

    def read_task_snapshot(self, task_id: str) -> dict[str, Any] | None:
        path = self.tasks_dir / f"{task_id}.json"
        if not path.exists():
            return None
        snapshot = self.read_json(path, None)
        return snapshot if isinstance(snapshot, dict) else None

    def list_task_snapshots(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            payload = self.read_json(path, None)
            if isinstance(payload, dict):
                snapshots.append(payload)
        snapshots.sort(key=lambda item: (item.get("updated_at", ""), item.get("task_id", "")))
        return snapshots

    def check_and_record_cooldown(self, key: str, cooldown_seconds: int) -> int:
        if cooldown_seconds <= 0:
            return 0
        now = int(time.time())
        with self.lock("nudge-cooldown"):
            state = self.read_json(self.nudge_index_path, {})
            last_sent = state.get(key)
            if isinstance(last_sent, int):
                elapsed = now - last_sent
                if elapsed < cooldown_seconds:
                    return cooldown_seconds - elapsed
            state[key] = now
            self.write_json_atomic(self.nudge_index_path, state)
            return 0

    def unread_count(self, agent: str) -> int:
        return len(self.list_messages_for_agent(agent, unread_only=True, limit=0))

    def stale_unread_messages(self, older_than_seconds: int) -> list[dict[str, Any]]:
        stale: list[dict[str, Any]] = []
        if older_than_seconds <= 0:
            return stale
        now = time.time()
        for agent in self.list_agents():
            for message in self.list_messages_for_agent(agent, unread_only=True, limit=0):
                created_at = message.get("created_at")
                if not isinstance(created_at, str):
                    continue
                try:
                    age = now - parse_iso_utc(created_at).timestamp()
                except Exception:
                    continue
                if age >= older_than_seconds:
                    stale.append(message)
        stale.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
        return stale

    def replace_state_indexes(
        self,
        *,
        message_index: dict[str, Any],
        event_index: dict[str, Any],
        ack_index: dict[str, Any],
    ) -> None:
        with self.lock("state-rehydrate"):
            self.write_json_atomic(self.message_index_path, message_index)
            self.write_json_atomic(self.event_index_path, event_index)
            self.write_json_atomic(self.ack_index_path, ack_index)

    def replace_task_snapshots(self, snapshots: dict[str, dict[str, Any]]) -> None:
        self.ensure_layout()
        for existing in self.tasks_dir.glob("*.json"):
            if existing.stem not in snapshots:
                existing.unlink()
        for task_id, snapshot in snapshots.items():
            self.write_task_snapshot(task_id, snapshot)
