#!/usr/bin/env python3
"""Scheduled notifier: nudge idle agents when they have unread team-chat messages.

Designed to be run by a single system cronjob (e.g. every 5 minutes).
- Polls `team-chat status --json` for each team under data-root/teams
- For each member with unread>0, checks agent-manager runtime state
- Nudges via agent-manager `send` only when agent is idle
- Persists per-(team,member) cooldown state to avoid spamming

This job must NOT ack messages itself; it only nudges agents.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Support running as a script (cron) without requiring `pip install -e .`.
try:
    from team_chat.scripts.service_state import dump_json_one_line, update_service_state
except ModuleNotFoundError:  # pragma: no cover
    _here = Path(__file__).resolve()
    _pkg_root = _here.parents[1]  # team-chat/
    if str(_pkg_root) not in sys.path:
        sys.path.insert(0, str(_pkg_root))
    from scripts.service_state import dump_json_one_line, update_service_state  # type: ignore


EMP_RE = re.compile(r"^(?:EMP_)?(\d{4})$")
EMP_DASH_RE = re.compile(r"^emp-(\d{4})$", re.IGNORECASE)


def _now_s() -> int:
    return int(time.time())


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def normalize_member_id(raw: str) -> str:
    raw = raw.strip()
    m = EMP_DASH_RE.match(raw)
    if m:
        return f"EMP_{m.group(1)}"
    m = EMP_RE.match(raw)
    if m:
        return f"EMP_{m.group(1)}"
    return raw


def run_json(cmd: list[str]) -> Any:
    # Keep errors short; cron logs should be readable.
    try:
        p = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        raise RuntimeError(f"failed to run: {cmd!r}: {e}")

    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        stdout = (p.stdout or "").strip()
        tail = (stderr or stdout)[:500]
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}: {tail}")

    out = (p.stdout or "").strip()
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid json from: {' '.join(cmd)}: {e}")


def run_text(cmd: list[str]) -> str:
    p = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        stdout = (p.stdout or "").strip()
        tail = (stderr or stdout)[:500]
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}: {tail}")
    return (p.stdout or "")


@dataclass
class AgentStatus:
    running: bool
    runtime_state: str


def parse_agent_manager_status(text: str) -> AgentStatus:
    running = False
    runtime_state = "unknown"

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Running:"):
            running = line.split(":", 1)[1].strip().lower().startswith("yes")
        elif line.startswith("Runtime state:"):
            runtime_state = line.split(":", 1)[1].strip().lower()

    return AgentStatus(running=running, runtime_state=runtime_state)


def is_agent_idle(agent_id: str, agent_manager_path: Path) -> bool:
    text = run_text([
        sys.executable,
        str(agent_manager_path),
        "status",
        agent_id,
    ])
    st = parse_agent_manager_status(text)
    return st.running and st.runtime_state == "idle"


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "members": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt state shouldn't break the whole job.
        return {"version": 1, "members": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a unique tmp name to tolerate overlapping cron runs.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def should_nudge(
    member_state: Dict[str, Any],
    unread_count: int,
    now_s: int,
    cooldown_s: int,
) -> bool:
    if unread_count <= 0:
        return False

    last_nudge_at = int(member_state.get("last_nudge_at", 0) or 0)
    last_unread_count = int(member_state.get("last_unread_count", 0) or 0)

    # If unread increased, allow immediate nudge even within cooldown.
    if unread_count > last_unread_count:
        return True

    return (now_s - last_nudge_at) >= cooldown_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data-root",
        default=str(Path(__file__).resolve().parents[5]),
        help="Repo root where teams/<team>/ state is stored",
    )
    ap.add_argument("--interval-minutes", type=int, default=5, help="Used for logs only")
    ap.add_argument("--cooldown-minutes", type=int, default=15, help="Per-(team,member) cooldown")
    ap.add_argument(
        "--teams",
        default="",
        help="Comma-separated team names to check (default: scan data-root/teams/*)",
    )
    ap.add_argument(
        "--state-dir",
        default="",
        help="When set, write cron-friendly state files (last_run/last_ok/fail_count) to this directory",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit a single-line JSON summary to stdout (cron/ops friendly)",
    )

    args = ap.parse_args()

    repo_root = Path(args.data_root).resolve()
    teams_dir = repo_root / "teams"
    if not teams_dir.exists():
        _eprint(f"error: teams dir not found: {teams_dir}")
        return 2

    # Resolve agent-manager script relative to the OpenClaw workspace layout.
    # This keeps cron invocation simple (just run this script anywhere).
    workspace_root = repo_root
    agent_manager_path = workspace_root / ".agent" / "skills" / "agent-manager" / "scripts" / "main.py"
    if not agent_manager_path.exists():
        _eprint(f"error: agent-manager not found: {agent_manager_path}")
        return 2

    # team-chat is vendored under the workspace projects/ tree.
    team_chat_main = repo_root / "projects" / "fractalmind-ai" / "team-chat-skill" / "team-chat" / "scripts" / "main.py"
    if not team_chat_main.exists():
        _eprint(f"error: team-chat main not found: {team_chat_main}")
        return 2

    if args.teams.strip():
        teams = [t.strip() for t in args.teams.split(",") if t.strip()]
    else:
        teams = sorted([p.name for p in teams_dir.iterdir() if p.is_dir()])

    now_s = _now_s()
    cooldown_s = int(args.cooldown_minutes) * 60

    nudged: list[Tuple[str, str, int]] = []
    errors: list[str] = []

    for team in teams:
        state_path = teams_dir / team / "state" / "notifier_state.json"
        state = load_state(state_path)
        state.setdefault("version", 1)
        members_state: Dict[str, Any] = state.setdefault("members", {})

        try:
            status = run_json([
                sys.executable,
                str(team_chat_main),
                "--data-root",
                str(repo_root),
                "--json",
                "status",
                team,
            ])
        except Exception as e:
            errors.append(f"{team}: status failed: {e}")
            continue

        unread_counts = status.get("unread_counts") if isinstance(status, dict) else None
        if not isinstance(unread_counts, dict):
            errors.append(f"{team}: missing unread_counts in status")
            continue

        changed = False

        for raw_member_id, raw_count in unread_counts.items():
            member_id = normalize_member_id(str(raw_member_id))
            try:
                unread_count = int(raw_count)
            except Exception:
                continue

            ms = members_state.setdefault(member_id, {})

            if not should_nudge(ms, unread_count, now_s, cooldown_s):
                ms["last_unread_count"] = unread_count
                changed = True
                continue

            try:
                if not is_agent_idle(member_id, agent_manager_path):
                    ms["last_unread_count"] = unread_count
                    changed = True
                    continue
            except Exception as e:
                errors.append(f"{team}:{member_id}: agent status failed: {e}")
                continue

            msg = (
                f"Nudge (every {args.interval_minutes}m): you have {unread_count} unread team-chat message(s) "
                f"in team '{team}'. Please run team-chat read --unread, ack what you processed, then proceed."
            )

            try:
                run_text([
                    sys.executable,
                    str(agent_manager_path),
                    "send",
                    member_id,
                    msg,
                ])
            except Exception as e:
                errors.append(f"{team}:{member_id}: send failed: {e}")
                continue

            ms["last_nudge_at"] = now_s
            ms["last_unread_count"] = unread_count
            nudged.append((team, member_id, unread_count))
            changed = True

        if changed:
            save_state(state_path, state)

    ok = len(errors) == 0

    # Optional: emit state files for cron/healthcheck without wrapper scripts.
    state_result = None
    if args.state_dir.strip():
        try:
            state_result = update_service_state(
                Path(args.state_dir),
                ok=ok,
                error=("; ".join(errors[:3]) if errors else None),
            )
        except Exception as e:
            # If state update fails, treat as a hard error for monitoring.
            errors.append(f"state update failed: {e}")
            ok = False

    if args.json:
        payload: Dict[str, Any] = {
            "ok": ok,
            "nudged_count": len(nudged),
            "teams_scanned": len(teams),
            "members_nudged": [{"team": t, "member": m, "unread": c} for t, m, c in nudged],
        }
        if errors:
            payload["errors"] = errors
        if state_result is not None:
            payload["state"] = asdict(state_result)
        sys.stdout.write(dump_json_one_line(payload))
    else:
        # Human-readable summary for cron logs.
        if nudged:
            print(f"nudged={len(nudged)} " + " ".join([f"{t}:{m}({c})" for t, m, c in nudged]))
        else:
            print("nudged=0")

        for e in errors:
            _eprint("warn: " + e)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
