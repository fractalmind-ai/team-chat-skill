#!/usr/bin/env python3
"""Small helpers for long-lived services / cron jobs."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ServiceStateResult:
    ok: bool
    fail_count: int
    last_run: int
    last_ok: Optional[int]
    error: Optional[str]


def _now_s() -> int:
    return int(time.time())


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def update_service_state(state_dir: Path, ok: bool, error: Optional[str] = None) -> ServiceStateResult:
    """Update state files for cron-friendly health checks."""

    now_s = _now_s()
    state_dir = state_dir.resolve()

    fail_count_path = state_dir / "unread_notifier.fail_count"
    last_run_path = state_dir / "unread_notifier.last_run"
    last_ok_path = state_dir / "unread_notifier.last_ok"

    prev_fail = read_int(fail_count_path) or 0
    fail_count = 0 if ok else (prev_fail + 1)

    write_text_atomic(last_run_path, f"{now_s}\n")
    write_text_atomic(fail_count_path, f"{fail_count}\n")

    last_ok: Optional[int] = None
    if ok:
        last_ok = now_s
        write_text_atomic(last_ok_path, f"{now_s}\n")

    return ServiceStateResult(ok=ok, fail_count=fail_count, last_run=now_s, last_ok=last_ok, error=error)


def dump_json_one_line(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n"
