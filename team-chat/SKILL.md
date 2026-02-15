---
name: team-chat
description: File-backed team collaboration control plane. Use when teams need append-only inbox/event messaging, task state snapshots, acknowledgements, retry/dead-letter safety, and operator observability via status/trace.
license: MIT
allowed-tools: [Read, Write, Edit, Bash]
---

# Team Chat

`team-chat` provides a local-first, append-only collaboration protocol for multi-agent teams.

## Command Path Baseline

```bash
CLI="python3 team-chat/scripts/main.py"
```

## Quick Start

```bash
# Initialize a team workspace
$CLI init demo --members lead,dev,qa

# Assign a task
$CLI task-assign demo \
  --from lead --to dev \
  --task-id task_001 \
  --subject "Implement API" \
  --details "Add POST /v1/profile" \
  --trace-id tr_001

# Read unread inbox
$CLI read demo --agent dev --unread

# Ack message
$CLI ack demo --agent dev --message-id msg_abc123

# Publish progress update
$CLI task-update demo \
  --from dev --to lead \
  --task-id task_001 \
  --status blocked \
  --note "Need schema decision" \
  --trace-id tr_001

# Operator observability
$CLI status demo
$CLI trace demo --trace-id tr_001

# Rebuild indexes/snapshots from append-only logs
$CLI rehydrate demo
```

## Data Layout

For team `demo`, state is stored under `teams/demo/`:

- `inboxes/<agent>.jsonl`: append-only message envelopes
- `events/YYYY-MM-DD.jsonl`: immutable event log
- `tasks/<task_id>.json`: current task snapshot
- `dead-letter/YYYY-MM-DD.jsonl`: exhausted-delivery records
- `state/*.json`: dedupe indexes + ack/cooldown state

## Protocol Fields

Message envelope (schema v1):

- Required: `id`, `type`, `from`, `to`, `payload`, `created_at`, `schema_version`
- Optional: `task_id`, `trace_id`, `priority`

Supported message types:

- `task_assign`
- `task_update`
- `idle_notification`
- `handoff`
- `decision_required`
- `shutdown_request`
- `shutdown_approved`
- `agent_wakeup_required`
- `agent_shutdown_required`
- `agent_started`
- `agent_stopped`
- `agent_error`
- `agent_timeout`

## Safety Features

- Idempotency: duplicate `message.id` and `event.id` are ignored.
- Atomic writes: lock + atomic replace for mutable index/snapshot files.
- Timeout + retry + dead-letter: `send --require-ack` applies per-type policy.
- Rehydrate: rebuild task/index state from append-only logs.
- Nudge cooldown: `send --cooldown-seconds` suppresses spam repeats.
- Path safety: `team`, `from`, `to`, and `agent` must match `^[A-Za-z0-9._-]+$` and cannot contain path separators/traversal tokens.
