from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path

CLI_PATH = Path(__file__).resolve().parents[1] / "team-chat" / "scripts" / "main.py"


def _run_cli(
    data_root: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(CLI_PATH), "--data-root", str(data_root), *args],
        capture_output=True,
        text=True,
        env=env,
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

    def test_status_shows_malformed_counter_and_optional_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,dev")
            self.assertEqual(0, init.returncode, init.stderr)

            malformed = root / "teams" / "demo" / "inboxes" / "lead.jsonl"
            malformed.write_text("{\"id\":\"bad\"\n", encoding="utf-8")

            result = _run_cli(
                root,
                "status",
                "demo",
                env_overrides={"TEAM_CHAT_WARN_MALFORMED": "1"},
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("warning: malformed jsonl skipped", result.stderr)
            self.assertIn("malformed_jsonl_total: 1", result.stdout)
            self.assertIn("teams/demo/inboxes/lead.jsonl", result.stdout)

    def test_doctor_check_json_schema_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,dev")
            self.assertEqual(0, init.returncode, init.stderr)

            send = _run_cli(
                root,
                "--json",
                "send",
                "demo",
                "--from",
                "lead",
                "--to",
                "dev",
                "--type",
                "handoff",
                "--payload-json",
                "{}",
            )
            self.assertEqual(0, send.returncode, send.stderr)

            result = _run_cli(root, "--json", "doctor", "check", "demo")
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)

            self.assertEqual("demo", payload["team"])
            self.assertIn(payload["overall_status"], {"healthy", "warn", "unhealthy"})
            self.assertIsInstance(payload.get("generated_at"), str)
            self.assertIsInstance(payload.get("exit_code"), int)
            self.assertIsInstance(payload.get("checks"), list)
            self.assertIsInstance(payload.get("stats"), dict)
            self.assertIsInstance(payload.get("recommendations"), list)

            check_names = {check.get("name") for check in payload["checks"] if isinstance(check, dict)}
            self.assertEqual(
                {
                    "index_integrity",
                    "malformed_jsonl",
                    "snapshot_monotonicity",
                    "index_inbox_sample_consistency",
                    "ack_index_consistency",
                },
                check_names,
            )
            for check in payload["checks"]:
                self.assertIsInstance(check.get("name"), str)
                self.assertIn(check.get("status"), {"healthy", "warn", "unhealthy"})
                self.assertIsInstance(check.get("summary"), str)
                self.assertIsInstance(check.get("details"), dict)

    def test_doctor_check_unhealthy_when_malformed_jsonl_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,dev")
            self.assertEqual(0, init.returncode, init.stderr)

            malformed = root / "teams" / "demo" / "inboxes" / "lead.jsonl"
            malformed.write_text("{\"id\":\"bad\"\n", encoding="utf-8")

            result = _run_cli(root, "--json", "doctor", "check", "demo")
            self.assertEqual(2, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("unhealthy", payload["overall_status"])

            malformed_check = next(
                (check for check in payload["checks"] if check.get("name") == "malformed_jsonl"),
                None,
            )
            self.assertIsNotNone(malformed_check)
            assert malformed_check is not None
            self.assertEqual("unhealthy", malformed_check["status"])
            self.assertGreater(int(malformed_check["details"].get("total", 0)), 0)

    def test_doctor_check_unhealthy_when_message_index_shard_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = _run_cli(root, "init", "demo", "--members", "lead,dev")
            self.assertEqual(0, init.returncode, init.stderr)

            send = _run_cli(
                root,
                "--json",
                "send",
                "demo",
                "--from",
                "lead",
                "--to",
                "dev",
                "--type",
                "handoff",
                "--payload-json",
                "{}",
            )
            self.assertEqual(0, send.returncode, send.stderr)
            message_id = json.loads(send.stdout)["message"]["id"]

            digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:2]
            shard_path = root / "teams" / "demo" / "state" / "message-index-shards" / f"{digest}.json"
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            shard.pop(message_id, None)
            shard_path.write_text(json.dumps(shard, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = _run_cli(root, "--json", "doctor", "check", "demo")
            self.assertEqual(2, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("unhealthy", payload["overall_status"])

            index_check = next(
                (check for check in payload["checks"] if check.get("name") == "index_integrity"),
                None,
            )
            self.assertIsNotNone(index_check)
            assert index_check is not None
            self.assertEqual("unhealthy", index_check["status"])
            self.assertGreater(int(index_check["details"].get("missing_index_entries", 0)), 0)


if __name__ == "__main__":
    unittest.main()
