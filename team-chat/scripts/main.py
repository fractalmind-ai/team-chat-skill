#!/usr/bin/env python3
"""team-chat CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from protocol import MESSAGE_TYPES
from repo_root import get_repo_root
from service import TeamChatService, human_age_minutes, iso_to_local_string


def _parse_payload(args: argparse.Namespace) -> dict:
    if args.payload_json:
        return json.loads(args.payload_json)
    if args.payload_file:
        path = Path(args.payload_file)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"payload file not found: {path}") from exc
    return {}


def _print(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return

    status = data.get("status")
    if status:
        print(f"status: {status}")

    if "message" in data and isinstance(data["message"], dict):
        message = data["message"]
        print(f"message_id: {message.get('id')}")
        print(f"type: {message.get('type')}")
        print(f"from: {message.get('from')} -> {message.get('to')}")

    if "count" in data and "messages" in data:
        print(f"messages: {data['count']}")
        for message in data.get("messages", []):
            print(
                f"- {message.get('id')} [{message.get('type')}] "
                f"{message.get('from')} -> {message.get('to')}"
            )
        next_cursor = data.get("next_cursor")
        if isinstance(next_cursor, str) and next_cursor:
            print(f"next_cursor: {next_cursor}")


def cmd_init(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    members = [member.strip() for member in (args.members or "").split(",") if member.strip()]
    result = service.init_team(args.team, members=members)
    _print(result, args.json)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    payload = _parse_payload(args)
    envelope = {
        "id": args.message_id,
        "type": args.type,
        "from": args.sender,
        "to": args.recipient,
        "task_id": args.task_id,
        "trace_id": args.trace_id,
        "priority": args.priority,
        "payload": payload,
    }
    envelope = {key: value for key, value in envelope.items() if value is not None}

    result = service.send(
        args.team,
        envelope,
        require_ack=args.require_ack,
        ack_timeout_seconds=args.ack_timeout_seconds,
        max_retries=args.max_retries,
        cooldown_seconds=args.cooldown_seconds,
    )
    _print(result, args.json)
    return 0


def cmd_task_assign(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    payload = {
        "subject": args.subject,
        "details": args.details,
    }
    envelope = {
        "type": "task_assign",
        "from": args.sender,
        "to": args.recipient,
        "task_id": args.task_id,
        "trace_id": args.trace_id,
        "priority": args.priority,
        "payload": payload,
    }
    result = service.send(args.team, envelope, cooldown_seconds=args.cooldown_seconds)
    _print(result, args.json)
    return 0


def cmd_task_update(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    payload = {
        "status": args.status,
        "progress": args.progress,
        "eta": args.eta,
        "blocked": args.blocked,
        "note": args.note,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    envelope = {
        "type": "task_update",
        "from": args.sender,
        "to": args.recipient,
        "task_id": args.task_id,
        "trace_id": args.trace_id,
        "priority": args.priority,
        "payload": payload,
    }
    result = service.send(args.team, envelope, cooldown_seconds=args.cooldown_seconds)
    _print(result, args.json)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.read(
        args.team,
        agent=args.agent,
        unread_only=args.unread,
        limit=args.limit,
        cursor=args.cursor,
    )
    _print(result, args.json)
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.ack(args.team, agent=args.agent, message_id=args.message_id)
    _print(result, args.json)
    return 0 if result.get("status") in {"acked", "already_acked"} else 1


def cmd_status(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.status(args.team, stale_minutes=args.stale_minutes)
    if args.json:
        _print(result, True)
        return 0

    print(f"team: {result['team']}")
    print("members:")
    for member in result["members"]:
        unread = result["unread_counts"].get(member, 0)
        print(f"- {member}: unread={unread}")

    print(f"blocked_tasks: {len(result['blocked_tasks'])}")
    for task in result["blocked_tasks"]:
        print(f"- {task.get('task_id')} owner={task.get('owner')} status={task.get('status')}")

    print(f"stale_tasks: {len(result['stale_tasks'])}")
    for task in result["stale_tasks"]:
        age = human_age_minutes(task.get("updated_at", ""))
        print(
            f"- {task.get('task_id')} owner={task.get('owner')} "
            f"last_update={iso_to_local_string(task.get('updated_at', ''))} age_min={age}"
        )

    print(f"stale_messages: {len(result['stale_messages'])}")
    for msg in result["stale_messages"]:
        age = human_age_minutes(msg.get("created_at", ""))
        print(
            f"- {msg.get('id')} to={msg.get('to')} type={msg.get('type')} "
            f"created={iso_to_local_string(msg.get('created_at', ''))} age_min={age}"
        )

    diagnostics = result.get("malformed_jsonl", {})
    malformed_total = int(diagnostics.get("total", 0)) if isinstance(diagnostics, dict) else 0
    print(f"malformed_jsonl_total: {malformed_total}")
    if isinstance(diagnostics, dict):
        for item in diagnostics.get("files", []):
            if not isinstance(item, dict):
                continue
            print(
                f"- {item.get('path')}: count={item.get('count', 0)} "
                f"last_line={item.get('last_line')} last_reason={item.get('last_reason')}"
            )
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.trace(
        args.team,
        trace_id=args.trace_id,
        limit=args.limit,
        cursor=args.cursor,
    )
    if args.json:
        _print(result, True)
        return 0

    print(f"trace_id: {result['trace_id']}")
    print(f"events: {result['count']}")
    for event in result["events"]:
        print(
            f"- {event.get('created_at')} {event.get('kind')} "
            f"id={event.get('id')} task={event.get('task_id', '-') }"
        )
    if result.get("next_cursor"):
        print(f"next_cursor: {result['next_cursor']}")
    return 0


def cmd_rehydrate(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.rehydrate(args.team)
    _print({"status": "ok", **result}, args.json)
    return 0


def cmd_doctor_check(args: argparse.Namespace) -> int:
    service = TeamChatService(Path(args.data_root))
    result = service.doctor_check(args.team, sample_size=args.sample_size)
    if args.json:
        _print(result, True)
        return int(result.get("exit_code", 1))

    print(f"team: {result.get('team')}")
    print(f"overall_status: {result.get('overall_status')}")
    for check in result.get("checks", []):
        if not isinstance(check, dict):
            continue
        print(
            f"- {check.get('name')}: status={check.get('status')} "
            f"summary={check.get('summary')}"
        )

    recommendations = result.get("recommendations", [])
    if isinstance(recommendations, list) and recommendations:
        print("recommendations:")
        for recommendation in recommendations:
            print(f"- {recommendation}")

    return int(result.get("exit_code", 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="team-chat file-backed control-plane CLI")
    parser.add_argument(
        "--data-root",
        default=str(get_repo_root()),
        help="Repository root where teams/<team>/ state is stored",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize team state folders")
    init_parser.add_argument("team")
    init_parser.add_argument("--members", help="Comma-separated member ids", default="")
    init_parser.set_defaults(func=cmd_init)

    send_parser = subparsers.add_parser("send", help="Send protocol message")
    send_parser.add_argument("team")
    send_parser.add_argument("--message-id")
    send_parser.add_argument("--from", dest="sender", required=True)
    send_parser.add_argument("--to", dest="recipient", required=True)
    send_parser.add_argument("--type", required=True, choices=sorted(MESSAGE_TYPES))
    send_parser.add_argument("--task-id")
    send_parser.add_argument("--trace-id")
    send_parser.add_argument("--priority", default="normal")
    send_parser.add_argument("--payload-json")
    send_parser.add_argument("--payload-file")
    send_parser.add_argument("--require-ack", action="store_true")
    send_parser.add_argument("--ack-timeout-seconds", type=int)
    send_parser.add_argument("--max-retries", type=int)
    send_parser.add_argument("--cooldown-seconds", type=int, default=0)
    send_parser.set_defaults(func=cmd_send)

    assign_parser = subparsers.add_parser("task-assign", help="Convenience task_assign command")
    assign_parser.add_argument("team")
    assign_parser.add_argument("--from", dest="sender", required=True)
    assign_parser.add_argument("--to", dest="recipient", required=True)
    assign_parser.add_argument("--task-id", required=True)
    assign_parser.add_argument("--subject", required=True)
    assign_parser.add_argument("--details", default="")
    assign_parser.add_argument("--trace-id")
    assign_parser.add_argument("--priority", default="normal")
    assign_parser.add_argument("--cooldown-seconds", type=int, default=0)
    assign_parser.set_defaults(func=cmd_task_assign)

    update_parser = subparsers.add_parser("task-update", help="Convenience task_update command")
    update_parser.add_argument("team")
    update_parser.add_argument("--from", dest="sender", required=True)
    update_parser.add_argument("--to", dest="recipient", required=True)
    update_parser.add_argument("--task-id", required=True)
    update_parser.add_argument("--status")
    update_parser.add_argument("--progress")
    update_parser.add_argument("--eta")
    blocked_group = update_parser.add_mutually_exclusive_group()
    blocked_group.add_argument("--blocked", dest="blocked", action="store_const", const=True)
    blocked_group.add_argument("--unblocked", dest="blocked", action="store_const", const=False)
    update_parser.set_defaults(blocked=None)
    update_parser.add_argument("--note")
    update_parser.add_argument("--trace-id")
    update_parser.add_argument("--priority", default="normal")
    update_parser.add_argument("--cooldown-seconds", type=int, default=0)
    update_parser.set_defaults(func=cmd_task_update)

    read_parser = subparsers.add_parser("read", help="Read inbox messages")
    read_parser.add_argument("team")
    read_parser.add_argument("--agent", required=True)
    read_parser.add_argument("--unread", action="store_true")
    read_parser.add_argument("--limit", type=int, default=50)
    read_parser.add_argument("--cursor", help="Read messages older than this message id")
    read_parser.set_defaults(func=cmd_read)

    ack_parser = subparsers.add_parser("ack", help="Acknowledge message")
    ack_parser.add_argument("team")
    ack_parser.add_argument("--agent", required=True)
    ack_parser.add_argument("--message-id", required=True)
    ack_parser.set_defaults(func=cmd_ack)

    status_parser = subparsers.add_parser("status", help="Team-level unread/blocked/stale snapshot")
    status_parser.add_argument("team")
    status_parser.add_argument("--stale-minutes", type=int, default=90)
    status_parser.set_defaults(func=cmd_status)

    trace_parser = subparsers.add_parser("trace", help="Trace events by trace_id")
    trace_parser.add_argument("team")
    trace_parser.add_argument("--trace-id", required=True)
    trace_parser.add_argument("--limit", type=int, default=0, help="0 means no limit")
    trace_parser.add_argument("--cursor", help="Read events older than this event id")
    trace_parser.set_defaults(func=cmd_trace)

    rehydrate_parser = subparsers.add_parser("rehydrate", help="Rebuild indexes/snapshots from logs")
    rehydrate_parser.add_argument("team")
    rehydrate_parser.set_defaults(func=cmd_rehydrate)

    doctor_parser = subparsers.add_parser("doctor", help="Storage/index diagnostics")
    doctor_subparsers = doctor_parser.add_subparsers(dest="doctor_command", required=True)
    doctor_check_parser = doctor_subparsers.add_parser("check", help="Run health checks")
    doctor_check_parser.add_argument("team")
    doctor_check_parser.add_argument("--sample-size", type=int, default=100)
    doctor_check_parser.set_defaults(func=cmd_doctor_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
