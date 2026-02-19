from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "team-chat" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from repo_root import get_repo_root  # noqa: E402


class RepoRootTests(unittest.TestCase):
    def test_projects_path_git_repo_returns_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "projects" / "acme" / "team-chat-skill"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            cwd_before = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("REPO_ROOT", None)
                    os.environ.pop("CLAW_WORKSPACE", None)
                    resolved = get_repo_root()
            finally:
                os.chdir(cwd_before)

            self.assertEqual(repo.resolve(), resolved)


if __name__ == "__main__":
    unittest.main()
