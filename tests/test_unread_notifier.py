from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

NOTIFIER_SRC = Path(__file__).resolve().parents[1] / "team-chat" / "scripts" / "unread_notifier.py"
SERVICE_STATE_SRC = Path(__file__).resolve().parents[1] / "team-chat" / "scripts" / "service_state.py"
REPO_ROOT_SRC = Path(__file__).resolve().parents[1] / "team-chat" / "scripts" / "repo_root.py"


class UnreadNotifierCliTests(unittest.TestCase):
    def test_help_does_not_crash_in_shallow_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts_dir = Path(tmp) / "x" / "team-chat" / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(NOTIFIER_SRC, scripts_dir / "unread_notifier.py")
            shutil.copy2(SERVICE_STATE_SRC, scripts_dir / "service_state.py")
            shutil.copy2(REPO_ROOT_SRC, scripts_dir / "repo_root.py")

            result = subprocess.run(
                [sys.executable, str(scripts_dir / "unread_notifier.py"), "--help"],
                capture_output=True,
                text=True,
                cwd=tmp,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("usage:", result.stdout.lower())
            self.assertNotIn("traceback", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
