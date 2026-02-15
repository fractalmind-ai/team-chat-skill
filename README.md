# team-chat-skill

File-backed Team Chat Protocol skill for multi-agent collaboration.

## What it delivers (Issue #1)

- Append-only per-agent inboxes (`teams/<team>/inboxes/*.jsonl`)
- Append-only daily event logs (`teams/<team>/events/YYYY-MM-DD.jsonl`)
- Task state snapshots (`teams/<team>/tasks/*.json`)
- Protocol commands for send/read/ack plus `task_assign`/`task_update`
- Safety: idempotency, atomic writes, retry/dead-letter, rehydrate, cooldown suppression
- Observability: `status` and `trace`

## Quick Start

```bash
CLI="python3 team-chat/scripts/main.py"

$CLI init demo --members lead,dev,qa
$CLI task-assign demo --from lead --to dev --task-id task_1 --subject "Build feature" --trace-id tr_1
$CLI read demo --agent dev --unread
$CLI ack demo --agent dev --message-id <MESSAGE_ID>
$CLI status demo
$CLI trace demo --trace-id tr_1
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Skill Doc

See `team-chat/SKILL.md` for full protocol and CLI reference.
