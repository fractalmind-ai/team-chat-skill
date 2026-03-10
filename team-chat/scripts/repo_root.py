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


def _git_root(cwd: Path) -> Optional[Path]:
    toplevel = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if not toplevel:
        return None
    try:
        return Path(toplevel).resolve()
    except Exception:
        return Path(toplevel)


def _openclaw_workspace_from_path(path: Path) -> Optional[Path]:
    """Detect explicit OpenClaw layout: .../work-assistant/projects/<org>/<repo>/..."""

    parts = list(path.resolve().parts)
    for idx, part in enumerate(parts):
        if part != "work-assistant":
            continue
        # Require ".../work-assistant/projects/<org>/<repo>/..."
        if idx + 3 < len(parts) and parts[idx + 1] == "projects":
            return Path(*parts[: idx + 1])
    return None


def _walk_up_for_data_root(start: Path) -> Optional[Path]:
    """Walk up from *start* looking for a directory that looks like a data root.

    A directory qualifies when it contains at least one of:
    - a ``teams/`` subdirectory
    - an ``AGENTS.md`` file
    - a ``.agent/`` directory
    """
    current = start.resolve()
    # Guard against symlink loops by capping iterations at a generous depth.
    for _ in range(256):
        if (current / "teams").is_dir():
            return current
        if (current / "AGENTS.md").is_file():
            return current
        if (current / ".agent").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def detect_data_root() -> Path:
    """Auto-detect the data root directory.

    Resolution order:
    1. ``TEAM_CHAT_DATA_ROOT`` environment variable (highest priority).
    2. Walk up from cwd looking for ``teams/``, ``AGENTS.md``, or ``.agent/``.
    3. Fall back to :func:`get_repo_root` (git / OpenClaw heuristics).
    """
    env_data_root = os.environ.get("TEAM_CHAT_DATA_ROOT")
    if env_data_root:
        return Path(env_data_root).expanduser()

    walked = _walk_up_for_data_root(Path.cwd())
    if walked is not None:
        return walked

    return get_repo_root()


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

    # Prefer real git roots first; this avoids false positives for paths that
    # merely contain a "projects" segment.
    git_root = _git_root(cwd)
    if git_root is not None:
        return git_root

    # Only then apply OpenClaw-specific layout heuristic.
    openclaw_workspace = _openclaw_workspace_from_path(cwd)
    if openclaw_workspace is not None:
        return openclaw_workspace

    return cwd
