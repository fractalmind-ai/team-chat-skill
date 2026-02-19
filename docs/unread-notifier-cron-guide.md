# unread_notifier: Cron + Healthcheck Integration Guide

This guide shows how to run `team-chat/scripts/unread_notifier.py` in production with stable paths, logs, and health checks.

Goal: a new operator should reach a working setup in under 5 minutes.

## Supported / Recommended / Not Recommended

### Supported

- Linux host with system `cron` or `systemd` timers.
- Local disk storage for workspace + `teams/` data.
- One notifier instance per workspace.

### Recommended

- Cron every 5 minutes.
- `--cooldown-minutes 15` (default) unless your team needs faster nudges.
- Use `--state-dir` and `--json` for machine-readable monitoring.
- Use absolute paths for script, data root, logs, and state directory.

### Not Recommended

- Relative paths in cron entries.
- Multiple overlapping cron entries running the same notifier job.
- NFS/cloud-sync folders for live state (`teams/`, state files, lock files).

## Exit Code Contract

`unread_notifier.py` exit codes:

- `0`: success
- `1`: runtime/operational error (partial team failures, command failures, state update failure)
- `2`: configuration/bootstrap error (missing required dirs/scripts)

## Quick Setup (Cron)

Assume workspace root is `/home/elliot245/work-assistant`.

1. Prepare directories:

```bash
mkdir -p /home/elliot245/work-assistant/logs
mkdir -p /home/elliot245/work-assistant/.state/team-chat
```

2. Add crontab entry (`crontab -e`):

```cron
*/5 * * * * /usr/bin/python3 /home/elliot245/work-assistant/projects/fractalmind-ai/team-chat-skill/team-chat/scripts/unread_notifier.py --data-root /home/elliot245/work-assistant --interval-minutes 5 --cooldown-minutes 15 --state-dir /home/elliot245/work-assistant/.state/team-chat --json >> /home/elliot245/work-assistant/logs/team-chat-unread-notifier.log 2>&1
```

Notes:

- Keep all paths absolute.
- `--interval-minutes` is informational in nudge text/logging.
- `--cooldown-minutes` controls per-(team,member) nudge suppression.

## Logging + logrotate

Example logrotate config (`/etc/logrotate.d/team-chat-unread-notifier`):

```conf
/home/elliot245/work-assistant/logs/team-chat-unread-notifier.log {
  daily
  rotate 7
  compress
  missingok
  notifempty
  copytruncate
}
```

Why `copytruncate`:

- Simple for cron append-style logs without service restart hooks.

## Healthcheck Patterns

### Simplest (log-based)

- Alert if recent log lines contain `warn:` repeatedly.
- Alert if log file has not updated for more than 2 cron intervals.

Example:

```bash
tail -n 200 /home/elliot245/work-assistant/logs/team-chat-unread-notifier.log | grep -c "warn:"
```

### Better (state-dir based, recommended)

With `--state-dir`, notifier writes:

- `unread_notifier.last_run`
- `unread_notifier.last_ok` (only on success)
- `unread_notifier.fail_count` (consecutive failures)

Suggested alert policy:

- critical if `fail_count >= 3`
- warning if `now - last_ok > 15 minutes` (for a 5-minute cron)

Example check script:

```bash
#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="/home/elliot245/work-assistant/.state/team-chat"
FAIL_COUNT_FILE="$STATE_DIR/unread_notifier.fail_count"
LAST_OK_FILE="$STATE_DIR/unread_notifier.last_ok"

now=$(date +%s)
fail_count=$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0)
last_ok=$(cat "$LAST_OK_FILE" 2>/dev/null || echo 0)

if [ "${fail_count:-0}" -ge 3 ]; then
  echo "CRITICAL: unread_notifier fail_count=$fail_count"
  exit 2
fi

if [ "${last_ok:-0}" -le 0 ] || [ $((now - last_ok)) -gt 900 ]; then
  echo "WARNING: unread_notifier last_ok is stale"
  exit 1
fi

echo "OK: unread_notifier healthy"
exit 0
```

## OpenClaw / Heartbeat Integration

Recommended heartbeat checks (every 10-30 minutes):

1. Read `fail_count` and `last_ok` from `--state-dir`.
2. Alert only when thresholds are breached (`fail_count >= 3` or stale `last_ok`).
3. Include latest log tail in alert payload for quick triage.

This avoids wrapper scripts that reimplement state accounting.

## Optional: systemd Timer Example

Unit file (`/etc/systemd/system/team-chat-unread-notifier.service`):

```ini
[Unit]
Description=team-chat unread notifier

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/elliot245/work-assistant/projects/fractalmind-ai/team-chat-skill/team-chat/scripts/unread_notifier.py --data-root /home/elliot245/work-assistant --interval-minutes 5 --cooldown-minutes 15 --state-dir /home/elliot245/work-assistant/.state/team-chat --json
WorkingDirectory=/home/elliot245/work-assistant
StandardOutput=append:/home/elliot245/work-assistant/logs/team-chat-unread-notifier.log
StandardError=append:/home/elliot245/work-assistant/logs/team-chat-unread-notifier.log
```

Timer file (`/etc/systemd/system/team-chat-unread-notifier.timer`):

```ini
[Unit]
Description=Run team-chat unread notifier every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now team-chat-unread-notifier.timer
```

## Common Pitfalls

- Using relative `--data-root` in cron (breaks when cron cwd differs).
- Running duplicate cron entries (double nudges).
- Forgetting log rotation (unbounded log growth).
- Assuming shared/network filesystem locking behaves like local disk.
