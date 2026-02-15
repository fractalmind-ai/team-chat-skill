"""Protocol utilities for team-chat envelope/event validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1

MESSAGE_TYPES = {
    "task_assign",
    "task_update",
    "idle_notification",
    "handoff",
    "decision_required",
    "shutdown_request",
    "shutdown_approved",
    "agent_wakeup_required",
    "agent_shutdown_required",
    "agent_started",
    "agent_stopped",
    "agent_error",
    "agent_timeout",
}

PRIORITIES = {"low", "normal", "high", "critical"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def normalize_message(payload: dict[str, Any]) -> dict[str, Any]:
    message = dict(payload)
    message.setdefault("schema_version", SCHEMA_VERSION)
    message.setdefault("id", _id("msg"))
    message.setdefault("created_at", utc_now_iso())
    message.setdefault("priority", "normal")
    message.setdefault("payload", {})

    validate_message(message)
    return message


def validate_message(message: dict[str, Any]) -> None:
    required = ["id", "type", "from", "to", "payload", "created_at", "schema_version"]
    missing = [field for field in required if field not in message]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    if message["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version: {message['schema_version']}")

    if not isinstance(message["id"], str) or not message["id"].strip():
        raise ValueError("message.id must be a non-empty string")

    msg_type = message.get("type")
    if msg_type not in MESSAGE_TYPES:
        supported = ", ".join(sorted(MESSAGE_TYPES))
        raise ValueError(f"Unsupported message type: {msg_type}. Supported: {supported}")

    for endpoint in ("from", "to"):
        value = message.get(endpoint)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"message.{endpoint} must be a non-empty string")

    if not isinstance(message.get("payload"), dict):
        raise ValueError("message.payload must be an object")

    priority = message.get("priority", "normal")
    if priority not in PRIORITIES:
        raise ValueError(f"Unsupported priority: {priority}")

    created_at = message.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("message.created_at must be a string")
    parse_iso_utc(created_at)


def new_event(
    *,
    kind: str,
    team: str,
    payload: dict[str, Any],
    trace_id: str | None = None,
    task_id: str | None = None,
    event_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    event = {
        "id": event_id or _id("evt"),
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "team": team,
        "payload": payload,
        "created_at": created_at or utc_now_iso(),
    }
    if trace_id:
        event["trace_id"] = trace_id
    if task_id:
        event["task_id"] = task_id
    return event


def sort_key_by_created_at(record: dict[str, Any]) -> tuple[datetime, str]:
    created_at = record.get("created_at", "1970-01-01T00:00:00Z")
    try:
        dt = parse_iso_utc(created_at)
    except Exception:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return dt, str(record.get("id", ""))
