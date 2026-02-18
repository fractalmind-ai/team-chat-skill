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
$CLI --json doctor check demo
$CLI read demo --agent dev --unread --limit 50 --json
# use returned next_cursor for older page
$CLI read demo --agent dev --unread --limit 50 --cursor <NEXT_CURSOR> --json
```

## Pagination / Cursor

- `read` supports `--limit` + `--cursor` (message id cursor, reads older messages).
- `trace` supports `--limit` + `--cursor` (event id cursor, reads older events).
- JSON output includes `next_cursor` when older records remain.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Identifier Safety

For write-path safety, `team`, `from`, `to`, and `agent` identifiers must match:

- `^[A-Za-z0-9._-]+$`
- No `/`, `\\`, or `..` traversal segments

## Task Snapshot Ordering / Conflict Rules

Task snapshots (`teams/<team>/tasks/*.json`) are updated with monotonic ordering guards:

- Ordering key: `(message.created_at, message.id)`
- Apply rule: apply only when incoming key is strictly newer than snapshot's last key
- Conflict behavior: stale/out-of-order updates are ignored (no state rollback)
- Tie-breaker: if timestamps are equal, lexicographically larger `message.id` wins

Snapshot metadata:

- `snapshot_version`: increments on every applied task message
- `last_message_id`
- `last_message_created_at`
- `snapshot_conflict_policy` (`created_at_then_message_id_monotonic`)

Compatibility / migration:

- Legacy snapshots without version metadata remain readable
- Metadata is added lazily on next applied update
- `rehydrate` rebuilds snapshots deterministically by chronological task message order

## Skill Doc

See `team-chat/SKILL.md` for full protocol and CLI reference.

## Storage and Filesystem Notes

- See `docs/filesystem-semantics.md` for locking model, atomicity boundaries, JSONL/index behavior, supported environments, and recovery/ops guidance.
