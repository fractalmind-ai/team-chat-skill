from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI_PATH = Path(__file__).resolve().parents[1] / "team-chat" / "scripts" / "main.py"


def _run_cli(data_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), "--data-root", str(data_root), *args],
        capture_output=True,
        text=True,
    )


class TeamChatCliTests(unittest.TestCase):
    def test_missing_payload_file_returns_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,ops")
            self.assertEqual(0, init.returncode, init.stderr)

            missing_file = root / "no-such-payload.json"
            result = _run_cli(
                root,
                "send",
                "demo",
                "--from",
                "ops",
                "--to",
                "lead",
                "--type",
                "handoff",
                "--payload-file",
                str(missing_file),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("error: payload file not found:", result.stderr)
            self.assertIn(str(missing_file), result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_existing_value_error_path_stays_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,dev")
            self.assertEqual(0, init.returncode, init.stderr)

            result = _run_cli(
                root,
                "send",
                "demo",
                "--from",
                "../ops",
                "--to",
                "dev",
                "--type",
                "handoff",
                "--payload-json",
                "{}",
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("error:", result.stderr)
            self.assertIn("message.from", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
