# File System Semantics and Operational Constraints

This document defines the runtime contract for file-backed storage in `team-chat-skill`.
It is written for operators running cron jobs and long-lived automation.

## Supported / Not Supported / Recommended

### Supported

- Linux and macOS hosts using a local POSIX filesystem.
- Single-host deployment where all writers/readers share one local path.
- Multiple processes on the same host coordinating via `flock` lock files.

### Not Supported

- Windows runtime for storage locking (`fcntl`/`flock` is POSIX-only in this codebase).
- Cross-host correctness guarantees on shared/network filesystems.
- Distributed transaction semantics across inbox/events/index/snapshot files.

### Recommended

- Local SSD/NVMe disk for `teams/<team>/` data.
- One service account with explicit read/write permissions to the data root.
- Host-level snapshots/backups for durability and rollback.
- Periodic `rehydrate` in maintenance windows when diagnosing index drift.

## 1) Storage Semantics and Path/Permission Constraints

Data root layout per team:

```text
teams/<team>/
  inboxes/<agent>.jsonl
  events/YYYY-MM-DD.jsonl
  tasks/<task_id>.json
  state/
    ack-index.json
    nudge-index.json
    malformed-jsonl.json
    message-index.json          # legacy
    event-index.json            # legacy
    message-index-shards/*.json
    event-index-shards/*.json
    message-index-shards/.migrated
    event-index-shards/.migrated
  dead-letter/YYYY-MM-DD.jsonl
  locks/*.lock
```

Path safety rules in protocol validation:

- `team`, `agent`, `from`, `to`, `task_id` must match `^[A-Za-z0-9._-]+$`.
- Path separators and traversal tokens are rejected.

Permission assumptions:

- Process user must have read/write/execute on `teams/<team>/` and children.
- Locks require write access to `teams/<team>/locks/`.
- Atomic replace requires write access to both target directory and temp file path.

Practical recommendation:

- Set owner-only write permissions for production (for example, `0750` dir + service account group policy).
- Avoid world-writable data roots.

## 2) Concurrency Model (`flock`) and Boundaries

Locking model:

- Lock files are per-team, per-resource (for example `messages.lock`, `events.lock`, `acks.lock`).
- Writers acquire an exclusive `flock` around critical sections.
- Team A and Team B do not block each other because lock paths differ.

What is guaranteed:

- Same-host cooperating processes that use these lock files serialize correctly.
- Lock lifetime is tied to process/file descriptor lifetime.

What is not guaranteed:

- Cross-host mutual exclusion on NFS/SMB is not guaranteed in all environments.
- Non-cooperating writers that bypass lock files can corrupt assumptions.

Why NFS can fail here (concrete):

- Some NFS deployments have weak/disabled lock daemons or split-brain lock behavior under network partitions.
- Host A may believe it holds `messages.lock` while Host B proceeds after stale lock state.
- Result: concurrent append/index updates can diverge.

## 3) Atomic Write Semantics (`tmp + fsync + os.replace`)

Current implementation behavior:

- JSON state files are written to a temp file, then `os.replace(temp, target)`.
- This protects readers from seeing partially written target JSON files.
- JSONL logs use append writes and are not transactional with index updates.

Durability nuance:

- Current code does **not** call `fsync` on file and parent directory before/after replace.
- Therefore crash/power-loss durability is best-effort, not fully crash-safe durability.

Operator-facing semantics:

- Atomic visibility: mostly yes for target JSON replacement on local POSIX filesystems.
- Crash durability: not guaranteed for the very latest write without explicit `fsync`.
- Cross-file atomicity (for example inbox append + index update): not guaranteed.

Failure window examples:

- Inbox append succeeds, index write fails: message exists but lookup may fallback/lag.
- Index write succeeds, subsequent step fails: temporary inconsistency until retry/rehydrate.
- Crash during append: trailing malformed JSONL line can appear and is skipped with diagnostics.

## 4) Shards, Retention, Cleanup, and `unlink` Risks

Index sharding:

- Message/event indexes are stored in shard JSON files.
- Legacy single-file indexes are compatibility-only; shard markers (`.migrated`) indicate shard mode.

Cleanup/compaction behavior today:

- No automatic inbox/event log compaction.
- Rehydrate/replace flows can remove old shard files (`unlink`) before writing rebuilt shards.

Operational risks:

- Interrupting a manual cleanup script mid-run can leave partial shard sets.
- Deleting inbox/event logs without coordinated index/snapshot rebuild can create dangling references.

Safe cleanup strategy:

1. Stop writers (or schedule maintenance window).
2. Backup full `teams/<team>/`.
3. Run cleanup/retention action.
4. Run `rehydrate` to rebuild derived state.
5. Run `doctor check` and verify status before resuming traffic.

Retention guidance:

- Treat `inboxes/` and `events/` as source-of-truth history.
- Treat `state/*index*` and `tasks/*.json` as rebuildable derived state.

## 5) Deployment Guidance and Minimum Baseline

Recommended deployment profile:

- Local filesystem only (ext4/xfs/apfs class local disk).
- One host per writable data root.
- Cron or supervisor launches with stable service user.

Not recommended deployment profile:

- NFS/SMB mounted data roots.
- Dropbox/OneDrive/iCloud synced folders.
- Multi-host active-active writes to the same `teams/` tree.

Why cloud sync folders break assumptions (concrete):

- Sync tools can upload temp files and merge/rename asynchronously.
- Conflict copies can appear (`file (conflicted copy).json`) outside protocol expectations.
- `flock` does not coordinate through remote sync engines.

Minimum practical baseline:

- 1 vCPU, 1 GB RAM, local SSD, stable clock (NTP), and enough disk headroom for append-only logs.
- Alerts for malformed growth, dead-letter growth, and disk utilization.

## 6) Troubleshooting and Recovery

### Symptom: `doctor check` reports index inconsistency

Likely causes:

- Partial index rewrite.
- Manual deletion of shard files.
- Crash between append and index update.

Recovery:

1. Backup current `teams/<team>/`.
2. Run `rehydrate`.
3. Re-run `doctor check` and confirm healthy/warn state is expected.

### Symptom: malformed JSONL counter keeps increasing

Likely causes:

- External script writing non-JSON or truncated lines.
- Storage corruption in tail lines after abrupt restart.

Recovery:

1. Inspect `state/malformed-jsonl.json` for path/reason.
2. Repair producer or rotate affected log file during maintenance.
3. Rehydrate if derived state drift is suspected.

### Symptom: permission denied errors in cron

Likely causes:

- Service user mismatch.
- Directory ownership/mode drift after deployment changes.

Recovery:

1. Verify service user and group.
2. Fix ownership/modes on `teams/` tree.
3. Re-run command with `--json` and verify exit code/summary.

### Symptom: stale health despite process running

Likely causes:

- Cron executes but fails before writing state output.
- Shared storage latency causing inconsistent reads.

Recovery:

1. Run command manually with `--json`.
2. Verify local disk path and remove shared-sync mount from write path.
3. Add watchdog based on `last_ok` freshness and failure counters.

## Quick Ops Checklist

- Use local disk, not NFS/cloud-sync.
- Keep one writer domain per team data root.
- Monitor malformed/dead-letter/disk growth.
- Backup first, then cleanup.
- Use `rehydrate` + `doctor check` as the standard recovery loop.
