"""Repository root helpers for team-chat skill."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def _run_git(cwd: Path, args: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        )
        value = (result.stdout or "").strip()
        return value or None
    except Exception:
        return None


def get_repo_root() -> Path:
    # Prefer an explicit env override for cron/agents.
    repo_root_env = os.environ.get("REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).expanduser()

    # In OpenClaw, the team-chat storage lives under the workspace repo root
    # (e.g. /home/<user>/work-assistant/teams/<team>/...). team-chat itself is
    # vendored under projects/, so the git toplevel of the current cwd would be
    # wrong when invoked from inside the skill repo.
    claw_workspace = os.environ.get("CLAW_WORKSPACE")
    if claw_workspace:
        return Path(claw_workspace).expanduser()

    cwd = Path.cwd().resolve()
    # Heuristic: if we're running from within .../projects/<org>/<repo>/..., hop
    # back to the OpenClaw workspace root.
    parts = list(cwd.parts)
    try:
        i = parts.index("projects")
    except ValueError:
        i = -1
    if i > 0:
        return Path(*parts[:i])

    toplevel = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if toplevel:
        return Path(toplevel)
    return cwd
