"""Business logic for team-chat protocol operations."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from protocol import (
    new_event,
    normalize_message,
    parse_iso_utc,
    sort_key_by_created_at,
    utc_now_iso,
    validate_identifier,
)
from storage import TeamStore


def _dlq_id() -> str:
    return f"dlq_{uuid4().hex[:12]}"


class TeamChatService:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self._stores: dict[str, TeamStore] = {}

    def store(self, team: str) -> TeamStore:
        safe_team = validate_identifier(team, field_name="team")
        store = self._stores.get(safe_team)
        if store is None:
            store = TeamStore(self.repo_root, safe_team)
            self._stores[safe_team] = store
        return store

    def init_team(self, team: str, members: list[str] | None = None) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()

        members = members or []
        for member in members:
            validate_identifier(member, field_name="member")
            (store.inboxes_dir / f"{member}.jsonl").touch(exist_ok=True)

        if not store.team_meta_path.exists():
            store.write_json_atomic(
                store.team_meta_path,
                {
                    "team": team,
                    "members": members,
                    "schema_version": 1,
                    "created_at": utc_now_iso(),
                },
            )
        return {"status": "ok", "team": team, "members": members}

    def send(
        self,
        team: str,
        envelope: dict[str, Any],
        *,
        require_ack: bool = False,
        ack_timeout_seconds: int | None = None,
        max_retries: int | None = None,
        cooldown_seconds: int = 0,
    ) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()

        message = normalize_message(envelope)
        trace_id = message.get("trace_id")
        task_id = message.get("task_id")

        cooldown_key = f"{message['to']}::{task_id or '-'}::{message['type']}"
        remaining = store.check_and_record_cooldown(cooldown_key, cooldown_seconds)
        if remaining > 0:
            event = new_event(
                kind="message_suppressed",
                team=team,
                trace_id=trace_id,
                task_id=task_id,
                payload={
                    "message_id": message["id"],
                    "reason": "cooldown",
                    "cooldown_remaining_seconds": remaining,
                    "to": message["to"],
                    "type": message["type"],
                },
            )
            store.append_event(event)
            return {
                "status": "suppressed",
                "reason": "cooldown",
                "cooldown_remaining_seconds": remaining,
                "message": message,
            }

        inserted = store.upsert_message(message)
        send_kind = "message_sent" if inserted else "message_duplicate"
        send_event = new_event(
            kind=send_kind,
            team=team,
            trace_id=trace_id,
            task_id=task_id,
            payload={"message": message},
        )
        store.append_event(send_event)

        if inserted:
            self._update_task_snapshot_from_message(store, message)

        if not require_ack:
            return {
                "status": "sent" if inserted else "duplicate",
                "message": message,
                "event": send_event,
            }

        policy = store.ack_policy_for_type(message["type"])
        timeout = int(ack_timeout_seconds if ack_timeout_seconds is not None else policy["ack_timeout_seconds"])
        retries = int(max_retries if max_retries is not None else policy["max_retries"])

        for attempt in range(1, retries + 2):
            ack = self._wait_for_ack(store, message_id=message["id"], timeout_seconds=timeout)
            if ack:
                ack_event = new_event(
                    kind="delivery_acked",
                    team=team,
                    trace_id=trace_id,
                    task_id=task_id,
                    payload={
                        "message_id": message["id"],
                        "attempt": attempt,
                        "acked_at": ack.get("acked_at"),
                        "agent": ack.get("agent"),
                    },
                )
                store.append_event(ack_event)
                return {
                    "status": "acked",
                    "message": message,
                    "attempt": attempt,
                    "ack": ack,
                }

            if attempt <= retries:
                retry_event = new_event(
                    kind="delivery_retry",
                    team=team,
                    trace_id=trace_id,
                    task_id=task_id,
                    payload={
                        "message_id": message["id"],
                        "attempt": attempt,
                        "timeout_seconds": timeout,
                    },
                )
                store.append_event(retry_event)

        dead_letter = {
            "id": _dlq_id(),
            "schema_version": 1,
            "team": team,
            "message_id": message["id"],
            "task_id": task_id,
            "trace_id": trace_id,
            "reason": "ack_timeout",
            "attempts": retries + 1,
            "created_at": utc_now_iso(),
            "message": message,
        }
        store.write_dead_letter(dead_letter)

        dlq_event = new_event(
            kind="delivery_dead_letter",
            team=team,
            trace_id=trace_id,
            task_id=task_id,
            payload={
                "dead_letter_id": dead_letter["id"],
                "message_id": message["id"],
                "attempts": retries + 1,
            },
        )
        store.append_event(dlq_event)

        return {
            "status": "dead_letter",
            "message": message,
            "dead_letter": dead_letter,
        }

    def read(
        self,
        team: str,
        *,
        agent: str,
        unread_only: bool,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()

        safe_agent = validate_identifier(agent, field_name="agent")
        messages, next_cursor = store.list_messages_window_for_agent(
            safe_agent,
            unread_only=unread_only,
            limit=limit,
            cursor=cursor,
        )
        read_event = new_event(
            kind="inbox_read",
            team=team,
            payload={
                "agent": safe_agent,
                "count": len(messages),
                "unread_only": unread_only,
                "cursor": cursor,
                "next_cursor": next_cursor,
            },
        )
        store.append_event(read_event)

        return {
            "team": team,
            "agent": safe_agent,
            "messages": messages,
            "count": len(messages),
            "next_cursor": next_cursor,
        }

    def ack(self, team: str, *, agent: str, message_id: str) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()
        safe_agent = validate_identifier(agent, field_name="agent")

        message = store.get_message(message_id)
        if not message:
            reject_event = new_event(
                kind="ack_rejected",
                team=team,
                payload={"agent": safe_agent, "message_id": message_id, "reason": "message_not_found"},
            )
            store.append_event(reject_event)
            return {"status": "not_found", "message_id": message_id}

        if message.get("to") != safe_agent:
            reject_event = new_event(
                kind="ack_rejected",
                team=team,
                trace_id=message.get("trace_id"),
                task_id=message.get("task_id"),
                payload={"agent": safe_agent, "message_id": message_id, "reason": "wrong_recipient"},
            )
            store.append_event(reject_event)
            return {"status": "wrong_recipient", "message_id": message_id, "expected": message.get("to")}

        created = store.record_ack(
            message_id,
            agent=safe_agent,
            acked_at=utc_now_iso(),
            delivery_id=message.get("delivery_id"),
        )

        kind = "message_acked" if created else "message_ack_duplicate"
        ack_event = new_event(
            kind=kind,
            team=team,
            trace_id=message.get("trace_id"),
            task_id=message.get("task_id"),
            payload={"agent": safe_agent, "message_id": message_id},
        )
        store.append_event(ack_event)

        return {
            "status": "acked" if created else "already_acked",
            "message_id": message_id,
            "agent": safe_agent,
        }

    def status(self, team: str, *, stale_minutes: int = 90) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()

        agents = store.list_agents()
        unread_counts = {agent: store.unread_count(agent) for agent in agents}
        snapshots = store.list_task_snapshots()

        blocked_tasks = [
            task
            for task in snapshots
            if str(task.get("status", "")).lower() == "blocked"
            or bool(task.get("blocked", False))
        ]

        stale_seconds = stale_minutes * 60
        now_ts = time.time()

        stale_tasks: list[dict[str, Any]] = []
        for task in snapshots:
            updated_at = task.get("updated_at")
            if not isinstance(updated_at, str):
                continue
            try:
                age = now_ts - parse_iso_utc(updated_at).timestamp()
            except Exception:
                continue
            if age >= stale_seconds:
                stale_tasks.append(task)

        stale_messages = store.stale_unread_messages(stale_seconds)

        return {
            "team": team,
            "members": agents,
            "unread_counts": unread_counts,
            "blocked_tasks": blocked_tasks,
            "stale_tasks": stale_tasks,
            "stale_messages": stale_messages,
            "task_count": len(snapshots),
        }

    def trace(
        self,
        team: str,
        *,
        trace_id: str,
        limit: int = 0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        store = self.store(team)
        clamped_limit = max(0, int(limit))

        if clamped_limit <= 0:
            events = store.iter_events()
            matched = [event for event in events if self._event_matches_trace(event, trace_id)]
            matched.sort(key=sort_key_by_created_at)
            return {
                "team": team,
                "trace_id": trace_id,
                "events": matched,
                "count": len(matched),
                "next_cursor": None,
            }

        started = cursor is None
        cursor_found = cursor is None
        collected: list[dict[str, Any]] = []
        target = clamped_limit + 1

        for event in store.iter_events_reverse():
            event_id = event.get("id")
            if not isinstance(event_id, str):
                continue

            if not started:
                if event_id == cursor:
                    started = True
                    cursor_found = True
                continue

            if not self._event_matches_trace(event, trace_id):
                continue

            collected.append(event)
            if len(collected) >= target:
                break

        if cursor is not None and not cursor_found:
            return {
                "team": team,
                "trace_id": trace_id,
                "events": [],
                "count": 0,
                "next_cursor": None,
            }

        page_reverse = collected[:clamped_limit]
        has_more = len(collected) > clamped_limit
        page = list(reversed(page_reverse))

        next_cursor = None
        if has_more and page:
            oldest_id = page[0].get("id")
            if isinstance(oldest_id, str):
                next_cursor = oldest_id
        return {
            "team": team,
            "trace_id": trace_id,
            "events": page,
            "count": len(page),
            "next_cursor": next_cursor,
        }

    def rehydrate(self, team: str) -> dict[str, Any]:
        store = self.store(team)
        store.ensure_layout()

        message_index: dict[str, Any] = {}
        ack_index: dict[str, Any] = {}
        event_index: dict[str, Any] = {}
        task_state: dict[str, dict[str, Any]] = {}

        for inbox in sorted(store.inboxes_dir.glob("*.jsonl")):
            for message in store.read_jsonl(inbox):
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    continue
                message_index[message_id] = {
                    "inbox": inbox.name,
                    "created_at": message.get("created_at"),
                    "to": message.get("to"),
                }
                self._update_task_snapshot_from_message_raw(task_state, message)

        for event_file in sorted(store.events_dir.glob("*.jsonl")):
            for event in store.read_jsonl(event_file):
                event_id = event.get("id")
                if isinstance(event_id, str) and event_id:
                    event_index[event_id] = {
                        "file": event_file.name,
                        "created_at": event.get("created_at"),
                    }
                if event.get("kind") == "message_acked":
                    payload = event.get("payload", {})
                    message_id = payload.get("message_id")
                    agent = payload.get("agent")
                    if isinstance(message_id, str) and isinstance(agent, str):
                        ack_index[message_id] = {
                            "message_id": message_id,
                            "agent": agent,
                            "acked_at": event.get("created_at"),
                        }

        store.replace_state_indexes(
            message_index=message_index,
            event_index=event_index,
            ack_index=ack_index,
        )
        store.replace_task_snapshots(task_state)

        result = {
            "team": team,
            "message_count": len(message_index),
            "event_count": len(event_index),
            "ack_count": len(ack_index),
            "task_count": len(task_state),
        }

        event = new_event(kind="rehydrate_completed", team=team, payload=result)
        store.append_event(event)
        return result

    def _wait_for_ack(self, store: TeamStore, *, message_id: str, timeout_seconds: int) -> dict[str, Any] | None:
        timeout_seconds = max(1, int(timeout_seconds))
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            ack = store.get_ack(message_id)
            if ack:
                return ack
            time.sleep(1)
        return store.get_ack(message_id)

    def _update_task_snapshot_from_message(self, store: TeamStore, message: dict[str, Any]) -> None:
        snapshots: dict[str, dict[str, Any]] = {}
        self._update_task_snapshot_from_message_raw(snapshots, message)
        for task_id, snapshot in snapshots.items():
            existing = store.read_task_snapshot(task_id) or {}
            merged = dict(existing)
            merged.update(snapshot)
            merged["updated_at"] = utc_now_iso()
            store.write_task_snapshot(task_id, merged)

    def _update_task_snapshot_from_message_raw(
        self,
        snapshots: dict[str, dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        msg_type = message.get("type")
        task_id = message.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        try:
            task_id = validate_identifier(task_id, field_name="task_id")
        except ValueError:
            # Keep rehydrate resilient to malformed historical data.
            return

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        created_at = message.get("created_at") or utc_now_iso()

        current = snapshots.get(task_id, {})
        snapshot = dict(current)
        snapshot["task_id"] = task_id
        snapshot.setdefault("created_at", created_at)
        snapshot["updated_at"] = created_at
        snapshot.setdefault("owner", message.get("to"))
        snapshot.setdefault("trace_id", message.get("trace_id"))

        if msg_type == "task_assign":
            snapshot["status"] = "assigned"
            snapshot["owner"] = message.get("to")
            snapshot["assigned_by"] = message.get("from")
            snapshot["subject"] = payload.get("subject")
            snapshot["details"] = payload.get("details")
        elif msg_type == "task_update":
            status = payload.get("status")
            if isinstance(status, str) and status:
                snapshot["status"] = status
            progress = payload.get("progress")
            if progress is not None:
                snapshot["progress"] = progress
            eta = payload.get("eta")
            if eta is not None:
                snapshot["eta"] = eta
            blocked = payload.get("blocked")
            if blocked is not None:
                snapshot["blocked"] = bool(blocked)
            note = payload.get("note")
            if note is not None:
                snapshot["note"] = note
            snapshot["last_update_from"] = message.get("from")

        snapshots[task_id] = snapshot

    def _event_matches_trace(self, event: dict[str, Any], trace_id: str) -> bool:
        return (
            event.get("trace_id") == trace_id
            or event.get("payload", {}).get("trace_id") == trace_id
            or event.get("payload", {}).get("message", {}).get("trace_id") == trace_id
        )


def iso_to_local_string(value: str) -> str:
    try:
        dt = parse_iso_utc(value).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return value


def human_age_minutes(value: str) -> int:
    try:
        age = datetime.now(timezone.utc) - parse_iso_utc(value)
        return max(0, int(age.total_seconds() // 60))
    except Exception:
        return -1
