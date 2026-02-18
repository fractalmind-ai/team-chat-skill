# File System Semantics and Operational Constraints

This document explains the on-disk contract used by `team-chat-skill`, what environments are supported, and how to operate and recover safely.

## Scope and Guarantees

- Storage backend is plain files under `teams/<team>/`.
- Message and event logs are append-only JSONL.
- Indexes and snapshots are derived state that can be rebuilt (`rehydrate`).
- Atomic replace is used for JSON state files (`write temp -> os.replace`).
- Locking uses POSIX `flock` lock files per team.

Non-goals:

- No distributed transaction across multiple files.
- No strict crash-consistency guarantee across "append log + update index" without `fsync`.
- No support for multi-host shared-storage semantics.

## Directory Layout

Per team directory:

```text
teams/<team>/
  inboxes/
    <agent>.jsonl                  # append-only message log per receiver
  events/
    YYYY-MM-DD.jsonl               # append-only event log by day
  tasks/
    <task_id>.json                 # current task snapshot
  state/
    ack-index.json                 # message_id -> ack metadata
    nudge-index.json               # cooldown state
    malformed-jsonl.json           # malformed diagnostics state
    message-index.json             # legacy message index (compat)
    event-index.json               # legacy event index (compat)
    message-index-shards/*.json    # active message index shards
    event-index-shards/*.json      # active event index shards
    message-index-shards/.migrated # marker for shard migration state
    event-index-shards/.migrated   # marker for shard migration state
  dead-letter/
    YYYY-MM-DD.jsonl               # failed deliveries / timeout outputs
  locks/
    *.lock                         # flock lock files
```

## Concurrency and Locking

Locking is per team and per lock file:

- `messages.lock`: message append + message-index update
- `events.lock`: event append + event-index update
- `acks.lock`: ack index updates
- `task-snapshots.lock`: task snapshot merge/write
- `state-rehydrate.lock`: index replacement during `rehydrate`
- `malformed-jsonl.lock`: malformed diagnostics updates
- `dead-letter.lock`: dead-letter appends
- `nudge-cooldown.lock`: cooldown state updates

Current behavior:

- Critical write paths use a single lock at a time.
- Lock scope is process-level mutual exclusion for cooperating processes on the same machine.
- Locks are isolated per team (`teams/<team>/locks/*.lock`), so teams do not block each other.

Lock-order guidance (for future changes):

- Keep single-lock critical sections whenever possible.
- If multi-lock logic is added, enforce one global order and never invert it.
- Suggested order: `messages -> events -> acks -> task-snapshots -> state-rehydrate -> malformed-jsonl`.

## Atomicity and Crash Windows

### JSON state files

`write_json_atomic()` writes to a temp file in the same directory and then uses `os.replace()`:

- Prevents partial-content reads of the target JSON file.
- Final rename is atomic on local POSIX filesystems.
- Existing file is replaced in one step.

### JSONL append files

`append_jsonl()` appends one serialized line plus newline:

- Append-only semantics for logical history.
- Not a multi-file transaction.
- No explicit `fsync`, so sudden power loss can still lose the latest buffered writes.

### Known partial-failure windows

- Message append succeeds but index update does not:
  - Message exists in inbox, index entry may be missing.
  - Fallback scanning and `rehydrate` can recover derived state.
- Index update succeeds but process crashes before other related writes:
  - Temporary inconsistencies can occur until next successful operation or `rehydrate`.
- Malformed/truncated JSONL tail after crash:
  - Reader skips malformed records and records diagnostics (not hard-fail).

## Index and JSONL Semantics

### Append-only logs

- `inboxes/*.jsonl` and `events/*.jsonl` are append-only.
- Ordering is file append order; some rebuild flows sort by `(created_at, id)` for determinism.

### Index usage and fallback

- Primary fast path uses sharded indexes in `state/*-index-shards/`.
- Legacy single-file indexes are read for compatibility before migration marker exists.
- Status/read paths may fallback to inbox scans when index data is absent.
- `status()` fast path uses message index + ack index, with compatibility fallback.

### Malformed JSONL handling

- Malformed/non-object lines are skipped.
- Diagnostics are persisted in `state/malformed-jsonl.json`.
- Dedupe key includes line hash (and line number when known), so repeated reads do not inflate counts.
- Set `TEAM_CHAT_WARN_MALFORMED=1` to emit one warning per new malformed fingerprint.

## Supported and Unsupported Runtime Environments

Recommended:

- Local Linux/macOS filesystem (for example ext4/xfs/apfs local disk).
- Single host where all writers/readers share the same local filesystem semantics.

Not recommended:

- NFS/SMB/network filesystems.
- Cloud sync folders (for example Dropbox/iCloud/OneDrive style replication).
- Any environment where `flock` semantics, rename atomicity, or close-to-open consistency are weak.

Platform notes:

- Linux/macOS: expected path (POSIX `fcntl.flock` available).
- Windows: currently unsupported (module imports `fcntl`; lock model is POSIX-specific).

## Operations Guidance

### Backups

- Back up the full `teams/<team>/` tree, not only `state/`.
- `inboxes/` and `events/` are source-of-truth history; indexes/snapshots are rebuildable.
- For consistent snapshots, quiesce writers or take filesystem snapshots at host/storage layer.

### Monitoring

- Watch `status` output for `malformed_jsonl_total`.
- Alert on growth of:
  - `dead-letter/*.jsonl`
  - `state/malformed-jsonl.json` totals
  - unexpected divergence between unread behavior and operator expectations

### Disk and retention

- JSONL logs grow unbounded by default.
- Plan retention/archival for old inbox and event files.
- Keep enough free space to allow temp-file + replace writes.

### Permissions

- Run with least privilege.
- Ensure process user can read/write under `teams/<team>/`.
- Restrict group/world write permissions unless intentionally shared.

## FAQ and Failure Modes

### Why is `status` slower than expected?

- If message index is missing or partially unavailable, status can fallback to inbox scanning.
- Run `rehydrate` to rebuild indexes and snapshots from logs.

### Why do I see malformed JSONL counters increasing?

- A log contains invalid JSON or non-object JSON lines.
- Records are skipped by design to keep service running.
- Inspect `state/malformed-jsonl.json` for file path, reason, and last seen location.

### Can I delete index files to fix corruption?

- Yes, indexes are derived state.
- Preferred recovery path: run `rehydrate` to rebuild from inbox/events logs.

### Can old snapshots or indexes still be read?

- Legacy index/snapshot formats are kept readable with compatibility fallbacks.
- New writes prefer sharded indexes and monotonic snapshot metadata.

### What if process crashes mid-write?

- JSON state files use atomic replace, so target files should not contain partial JSON payload.
- JSONL files may have a truncated tail line; malformed handling skips it and records diagnostics.
- Rebuild command (`rehydrate`) is the standard recovery tool for derived state consistency.

