import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay_jobs as jobs


@unittest.skipIf(os.name == "nt", "Требуется POSIX shell.")
class JobLogShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.state = Path(self.tmp.name) / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["XDG_STATE_HOME"] = str(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def run_shell(self, command: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", command],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def status(self, name: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = self.run_shell(jobs.build_job_status_command(name))
        return result, jobs.parse_job_status(result.stdout)

    def wait_state(self, name: str, expected: str, timeout: float = 4) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        last: dict[str, object] | None = None
        while time.monotonic() < deadline:
            result, status = self.status(name)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            last = status
            if status["state"] == expected:
                return status
            time.sleep(0.05)
        self.fail(f"job {name} не перешёл в {expected}: {last}")

    def test_empty_readable_log_has_zero_size(self):
        start = self.run_shell(jobs.build_job_start_command("empty-log", "exit 0"))
        self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
        status = self.wait_state("empty-log", "succeeded")
        self.assertEqual(status["log_size"], 0)

    def test_unreadable_log_size_is_unknown_and_tail_fails(self):
        start = self.run_shell(jobs.build_job_start_command("unreadable-log", "printf 'known-data\\n'"))
        self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
        status = self.wait_state("unreadable-log", "succeeded")
        self.assertEqual(status["log_size"], 11)

        jobdir = self.state / "ssh_relay" / "jobs" / "unreadable-log"
        log_path = jobdir / "log"
        self.assertEqual(log_path.read_bytes(), b"known-data\n")
        log_path.chmod(0)
        try:
            if os.access(log_path, os.R_OK):
                self.skipTest("Текущий пользователь может читать chmod 000; тест отказа доступа неприменим.")

            status_result, unreadable_status = self.status("unreadable-log")
            self.assertEqual(status_result.returncode, 0, status_result.stderr + status_result.stdout)
            self.assertIsNone(unreadable_status["log_size"])

            tail = self.run_shell(jobs.build_job_tail_command("unreadable-log"))
            self.assertEqual(tail.returncode, 22, tail.stderr + tail.stdout)
            self.assertEqual(tail.stdout, "tail_error=log_unreadable\n")
            self.assertEqual(jobs.classify_job_command_failure(tail.returncode, tail.stdout), "log_unreadable")
            self.assertEqual(list(jobdir.glob(".tail-status.*")), [])
        finally:
            log_path.chmod(0o600)

        restored = self.run_shell(jobs.build_job_tail_command("unreadable-log"))
        self.assertEqual(restored.returncode, 0, restored.stderr + restored.stdout)
        self.assertEqual(restored.stdout, "known-data\n")
        self.assertEqual(list(jobdir.glob(".tail-status.*")), [])


class JobLogCliTests(unittest.TestCase):
    def setUp(self):
        import ssh_relay

        self.relay = ssh_relay

    def test_unknown_log_size_is_printed_as_unknown(self):
        output = StringIO()
        with redirect_stdout(output):
            self.relay.print_job_status(
                {
                    "job": "probe",
                    "state": "succeeded",
                    "pid": 123,
                    "elapsed": 5,
                    "exit_code": 0,
                    "log_size": None,
                    "log_age": 5,
                }
            )
        self.assertIn("Размер журнала: неизвестен\n", output.getvalue())

    def test_tail_unreadable_has_clear_service_error(self):
        message = self.relay._service_error(
            {
                "ok": True,
                "stdout": "tail_error=log_unreadable\n",
                "stderr": "",
                "exit_code": 22,
            }
        )
        self.assertEqual(message, "Журнал job существует, но недоступен для чтения.")

    def test_successful_tail_payload_is_not_control_error(self):
        message = self.relay._service_error(
            {
                "ok": True,
                "stdout": "start_error=active_exists\ntail_error=log_unreadable\n",
                "stderr": "",
                "exit_code": 0,
            }
        )
        self.assertIsNone(message)

    def test_tail_error_code_ignores_control_like_log_lines(self):
        reason = jobs.classify_job_command_failure(
            22,
            "start_error=active_exists\nstop_error=still_running\ntail_error=log_read_failed\n",
        )
        self.assertEqual(reason, "log_read_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
