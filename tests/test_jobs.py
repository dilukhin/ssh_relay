import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssh_relay_jobs as jobs


class JobShellTests(unittest.TestCase):
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

    def run_shell(self, command, timeout=10):
        return subprocess.run(
            ["sh", "-c", command],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def status(self, name):
        result = self.run_shell(jobs.build_job_status_command(name))
        return result, jobs.parse_job_status(result.stdout)

    def wait_state(self, name, expected, timeout=4):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            result, status = self.status(name)
            last = (result, status)
            if status["state"] == expected:
                return result, status
            time.sleep(0.05)
        self.fail(f"job {name} не перешёл в {expected}: {last}")

    def test_start_running_and_duplicate_is_rejected(self):
        start = self.run_shell(jobs.build_job_start_command("slow", "sleep 1"))
        self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
        status = jobs.parse_job_status(start.stdout)
        self.assertEqual(status["job"], "slow")
        self.assertEqual(status["state"], "running")
        self.assertIsInstance(status["pid"], int)

        second = self.run_shell(jobs.build_job_start_command("slow", "printf duplicate"))
        self.assertEqual(second.returncode, 17)
        self.assertEqual(jobs.classify_job_command_failure(second.returncode, second.stdout), "job_active_exists")

    def test_start_publishes_complete_process_identity(self):
        for index in range(20):
            name = f"identity{index}"
            start = self.run_shell(jobs.build_job_start_command(name, "sleep 0.2"))
            self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
            status = jobs.parse_job_status(start.stdout)
            self.assertNotEqual(status["state"], "unknown", start.stdout)
            self.assertIsInstance(status["pid"], int)
            jobdir = self.state / "ssh_relay" / "jobs" / name
            self.assertTrue((jobdir / "start_ticks").read_text().strip().isdigit())
            self.wait_state(name, "succeeded")

    def test_exit_zero_becomes_succeeded_and_state_persists(self):
        start = self.run_shell(jobs.build_job_start_command("ok", "printf 'done\\n'"))
        self.assertEqual(start.returncode, 0)
        _, status = self.wait_state("ok", "succeeded")
        self.assertEqual(status["exit_code"], 0)
        time.sleep(0.05)
        again_result, again = self.status("ok")
        self.assertEqual(again_result.returncode, 0)
        self.assertEqual(again["state"], "succeeded")
        self.assertEqual(again["exit_code"], 0)

    def test_nonzero_becomes_failed(self):
        start = self.run_shell(jobs.build_job_start_command("bad", "printf 'oops\\n'; exit 7"))
        self.assertEqual(start.returncode, 0)
        _, status = self.wait_state("bad", "failed")
        self.assertEqual(status["exit_code"], 7)

    def test_tail_is_limited_and_preserves_progress_lines(self):
        command = "printf '[ 69%%] Building A\\n[ 71%%] Building B\\n[ 73%%] Building C\\n'"
        start = self.run_shell(jobs.build_job_start_command("tailjob", command))
        self.assertEqual(start.returncode, 0)
        self.wait_state("tailjob", "succeeded")
        tail = self.run_shell(jobs.build_job_tail_command("tailjob", lines=2, max_bytes=1024))
        self.assertEqual(tail.returncode, 0)
        self.assertEqual(tail.stdout, "[ 71%] Building B\n[ 73%] Building C\n")

    def test_wait_timeout_does_not_stop_job(self):
        start = self.run_shell(jobs.build_job_start_command("waitjob", "sleep 1"))
        self.assertEqual(start.returncode, 0)

        def fetch_status():
            result, status = self.status("waitjob")
            self.assertEqual(result.returncode, 0)
            return status

        status, timed_out = jobs.wait_for_terminal_status(
            fetch_status,
            poll_interval=0.05,
            timeout=0.15,
        )
        self.assertTrue(timed_out)
        self.assertEqual(status["state"], "running")
        _, after = self.status("waitjob")
        self.assertEqual(after["state"], "running")
        self.wait_state("waitjob", "succeeded")

    def test_stop_soft_terminates_process_group(self):
        start = self.run_shell(jobs.build_job_start_command("stopjob", "sleep 30"))
        self.assertEqual(start.returncode, 0)
        stop = self.run_shell(jobs.build_job_stop_command("stopjob", force=False, grace_seconds=2), timeout=5)
        self.assertEqual(stop.returncode, 0, stop.stderr + stop.stdout)
        parsed = jobs.parse_job_status(stop.stdout)
        self.assertEqual(parsed["state"], "failed")
        self.assertIn(parsed["exit_code"], {143, 128 + 15})

    def test_force_is_separate_step_when_child_ignores_term(self):
        command = "trap '' TERM; while :; do sleep 1; done"
        start = self.run_shell(jobs.build_job_start_command("forcejob", command))
        self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
        soft = self.run_shell(
            jobs.build_job_stop_command("forcejob", force=False, grace_seconds=0.2),
            timeout=3,
        )
        self.assertEqual(soft.returncode, 21, soft.stderr + soft.stdout)
        self.assertEqual(jobs.classify_job_command_failure(soft.returncode, soft.stdout), "still_running")
        forced = self.run_shell(
            jobs.build_job_stop_command("forcejob", force=True, grace_seconds=0.2),
            timeout=3,
        )
        self.assertEqual(forced.returncode, 0, forced.stderr + forced.stdout)
        parsed = jobs.parse_job_status(forced.stdout)
        self.assertEqual(parsed["state"], "failed")
        self.assertEqual(parsed["exit_code"], 137)

    def test_state_files_do_not_store_full_command(self):
        secret_marker = "sensitive-command-marker-42"
        start = self.run_shell(jobs.build_job_start_command("nostore", f"sleep 1 # {secret_marker}"))
        self.assertEqual(start.returncode, 0, start.stderr + start.stdout)
        jobdir = self.state / "ssh_relay" / "jobs" / "nostore"
        for item in jobdir.iterdir():
            if item.is_file():
                self.assertNotIn(secret_marker, item.read_text(errors="replace"))
        self.wait_state("nostore", "succeeded")

    def test_unknown_job_is_reported(self):
        result = self.run_shell(jobs.build_job_status_command("missing"))
        self.assertEqual(result.returncode, 44)
        parsed = jobs.parse_job_status(result.stdout)
        self.assertTrue(parsed.get("not_found"))
        self.assertEqual(parsed["state"], "unknown")

    def test_live_pid_with_wrong_start_time_is_unknown_and_stop_is_refused(self):
        jobdir = self.state / "ssh_relay" / "jobs" / "reused"
        jobdir.mkdir(parents=True)
        (jobdir / "pid").write_text(f"{os.getpid()}\n")
        (jobdir / "start_ticks").write_text("1\n")
        (jobdir / "started_epoch").write_text(f"{int(time.time())}\n")
        (jobdir / "log").write_text("")
        status_result, status = self.status("reused")
        self.assertEqual(status_result.returncode, 0)
        self.assertEqual(status["state"], "unknown")
        stopped = self.run_shell(jobs.build_job_stop_command("reused", force=True, grace_seconds=0))
        self.assertEqual(stopped.returncode, 20)
        self.assertEqual(jobs.classify_job_command_failure(stopped.returncode, stopped.stdout), "identity_mismatch")

    def test_job_command_size_is_bounded(self):
        with self.assertRaises(ValueError):
            jobs.build_job_start_command("large", "x" * (jobs.MAX_JOB_COMMAND_BYTES + 1))

    def test_names_and_arguments_are_validated_and_escaped(self):
        for bad in ("", ".", "..", "a/b", "a b", "x;rm", "я"):
            with self.assertRaises(ValueError):
                jobs.validate_job_name(bad)
        payload = "printf '%s\\n' 'a b; $HOME; $(uname)'"
        start = self.run_shell(jobs.build_job_start_command("quoted", payload))
        self.assertEqual(start.returncode, 0)
        self.wait_state("quoted", "succeeded")
        tail = self.run_shell(jobs.build_job_tail_command("quoted"))
        self.assertEqual(tail.stdout, "a b; $HOME; $(uname)\n")

    def test_list_contains_terminal_and_running_jobs(self):
        self.assertEqual(self.run_shell(jobs.build_job_start_command("one", "exit 0")).returncode, 0)
        self.assertEqual(self.run_shell(jobs.build_job_start_command("two", "sleep 1")).returncode, 0)
        self.wait_state("one", "succeeded")
        listing = self.run_shell(jobs.build_job_list_command())
        self.assertEqual(listing.returncode, 0)
        parsed = {item["job"]: item for item in jobs.parse_job_list(listing.stdout)}
        self.assertEqual(parsed["one"]["state"], "succeeded")
        self.assertEqual(parsed["two"]["state"], "running")

    def test_completed_name_can_be_started_again_but_unknown_state_is_not_overwritten(self):
        self.assertEqual(self.run_shell(jobs.build_job_start_command("repeat", "exit 0")).returncode, 0)
        self.wait_state("repeat", "succeeded")
        restarted = self.run_shell(jobs.build_job_start_command("repeat", "sleep 1"))
        self.assertEqual(restarted.returncode, 0)

        jobdir = self.state / "ssh_relay" / "jobs" / "unknown"
        jobdir.mkdir(parents=True)
        (jobdir / "pid").write_text("999999\n")
        (jobdir / "start_ticks").write_text("1\n")
        blocked = self.run_shell(jobs.build_job_start_command("unknown", "printf should-not-run"))
        self.assertEqual(blocked.returncode, 18)
        self.assertEqual(jobs.classify_job_command_failure(blocked.returncode, blocked.stdout), "job_unknown_existing")
        self.assertFalse((jobdir / "log").exists())


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        import ssh_relay
        self.relay = ssh_relay

    def test_exec_parser_and_exit_code_are_preserved(self):
        from unittest import mock

        args = self.relay.build_parser().parse_args(["exec", "--name", "v3", "exit 7"])
        self.assertIs(args.handler, self.relay.exec_cmd)
        self.assertEqual(args.remote_command, "exit 7")
        session = {
            "name": "v3",
            "command_timeout": 120,
            "reconnect_wait": 30,
            "auth_token": "test",
            "daemon_port": 12345,
        }
        with mock.patch.object(self.relay._core, "read_session", return_value=session), mock.patch.object(
            self.relay._core,
            "request_daemon",
            return_value={"ok": True, "stdout": "", "stderr": "", "exit_code": 7},
        ) as request:
            self.assertEqual(self.relay.exec_cmd(args), 7)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1], "exec")

    def test_job_parser_routes_all_subcommands(self):
        cases = {
            "start": (["--job", "build", "sleep 1"], self.relay.job_start_cmd),
            "status": (["--job", "build"], self.relay.job_status_cmd),
            "tail": (["--job", "build"], self.relay.job_tail_cmd),
            "wait": (["--job", "build"], self.relay.job_wait_cmd),
            "stop": (["--job", "build"], self.relay.job_stop_cmd),
            "list": ([], self.relay.job_list_cmd),
        }
        for subcommand, (tail, handler) in cases.items():
            with self.subTest(subcommand=subcommand):
                args = self.relay.build_parser().parse_args(["job", subcommand, "--name", "v3", *tail])
                self.assertIs(args.handler, handler)

    def test_job_start_transport_unknown_is_not_retried_or_session_deleted(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from unittest import mock

        args = self.relay.build_parser().parse_args(
            ["job", "start", "--name", "v3", "--job", "build", "sleep 30"]
        )
        session = {
            "name": "v3",
            "command_timeout": 120,
            "reconnect_wait": 30,
            "auth_token": "test",
            "daemon_port": 12345,
        }
        stderr = StringIO()
        with mock.patch.object(self.relay, "read_session", return_value=session), mock.patch.object(
            self.relay,
            "request_daemon",
            side_effect=self.relay.DaemonUnavailableError("transport lost"),
        ) as request, mock.patch.object(self.relay, "remove_session_file") as remove, redirect_stderr(stderr):
            self.assertEqual(self.relay.job_start_cmd(args), 1)
        self.assertEqual(request.call_count, 1)
        remove.assert_not_called()
        self.assertIn("Не повторяйте запуск автоматически", stderr.getvalue())


    def test_job_control_reuses_short_exec_without_risky_receipt(self):
        from unittest import mock

        session = {"auth_token": "test", "daemon_port": 12345}
        with mock.patch.object(
            self.relay,
            "request_daemon",
            return_value={"ok": True, "stdout": "", "stderr": "", "exit_code": 0},
        ) as request:
            result = self.relay._run_job_control(session, "printf ok", response_timeout=7)
        self.assertTrue(result["ok"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], "exec")
        self.assertFalse(request.call_args.kwargs["risky"])
        self.assertEqual(request.call_args.kwargs["response_timeout"], 7)

    def test_daemon_wrapper_preserves_external_script_for_detach(self):
        from unittest import mock

        original_file = self.relay._core.__file__
        observed = {}

        def fake_daemon(_args):
            observed["file"] = self.relay._core.__file__
            observed["version"] = self.relay._core.__version__
            return 0

        with mock.patch.object(self.relay._core, "daemon", side_effect=fake_daemon):
            self.assertEqual(self.relay.daemon(object()), 0)
        self.assertEqual(Path(observed["file"]).name, "ssh_relay.py")
        self.assertEqual(observed["version"], "0.7.0")
        self.assertEqual(self.relay._core.__file__, original_file)

    def test_job_tail_limits_are_rejected_by_argparse(self):
        parser = self.relay.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["job", "tail", "--job", "x", "--lines", "1001"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["job", "tail", "--job", "x", "--bytes", "257K"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
