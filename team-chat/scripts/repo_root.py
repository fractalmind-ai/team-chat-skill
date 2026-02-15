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
    repo_root_env = os.environ.get("REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).expanduser()

    cwd = Path.cwd()
    toplevel = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if toplevel:
        return Path(toplevel)
    return cwd
