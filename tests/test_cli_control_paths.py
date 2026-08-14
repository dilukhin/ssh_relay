#!/usr/bin/env python3
"""Cross-platform тесты CLI control-plane и error-paths ssh_relay."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core


class CliControlPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ssh_relay.build_parser()
        self.session = {
            "name": "ci",
            "version": ssh_relay.__version__,
            "host": "198.51.100.42",
            "port": 22,
            "user": "donpedro",
            "daemon_port": 41000,
            "auth_token": "token",
            "command_timeout": 12,
            "reconnect_wait": 5,
            "download_timeout": 30,
            "upload_timeout": 30,
            "upload_max_size": 1024,
            "_session_file_path": "C:/state/ci.json",
        }

    @staticmethod
    def job_status(state: str = "running", exit_code: int | None = None) -> dict:
        return {
            "job": "build",
            "state": state,
            "pid": 123 if state == "running" else None,
            "elapsed": 4,
            "exit_code": exit_code,
            "log_size": 16,
            "log_age": 1,
        }

    def test_argument_and_transfer_helpers(self) -> None:
        self.assertEqual(1.5, ssh_relay.parse_positive_float_seconds("1.5"))
        for value in ("bad", "0", "-1"):
            with self.subTest(positive=value), self.assertRaises(argparse.ArgumentTypeError):
                ssh_relay.parse_positive_float_seconds(value)

        self.assertEqual(0.0, ssh_relay.parse_nonnegative_float_seconds("0"))
        self.assertEqual(60.0, ssh_relay.parse_nonnegative_float_seconds("60"))
        for value in ("bad", "-0.1", "60.1"):
            with self.subTest(nonnegative=value), self.assertRaises(argparse.ArgumentTypeError):
                ssh_relay.parse_nonnegative_float_seconds(value)

        self.assertEqual(1, ssh_relay.parse_tail_lines("1"))
        for value in ("bad", "0", str(ssh_relay.relay_jobs.MAX_TAIL_LINES + 1)):
            with self.subTest(lines=value), self.assertRaises(argparse.ArgumentTypeError):
                ssh_relay.parse_tail_lines(value)

        self.assertEqual((0, 8, 2), ssh_relay._version_tuple("0.8.2"))
        self.assertEqual((0, 0, 0), ssh_relay._version_tuple("0.8"))
        self.assertEqual((0, 0, 0), ssh_relay._version_tuple("a.b.c"))
        ssh_relay._require_transfer_daemon({"version": "0.8.0"})
        with self.assertRaises(core.RelayError):
            ssh_relay._require_transfer_daemon({"version": "0.7.9"})

        with patch.object(ssh_relay.time, "monotonic", return_value=11.0):
            self.assertEqual(4.0, ssh_relay._transfer_remaining(10.0, 5.0))
        with patch.object(ssh_relay.time, "monotonic", return_value=16.0):
            with self.assertRaisesRegex(core.RelayError, "общий аварийный предел"):
                ssh_relay._transfer_remaining(10.0, 5.0)

        with patch.object(ssh_relay, "request_daemon", side_effect=core.DaemonUnavailableError("timeout")):
            with self.assertRaisesRegex(core.RelayError, "Результат чанка неизвестен"):
                ssh_relay._transfer_request(self.session, "download", idle_timeout=2, remaining=5)
        with patch.object(ssh_relay, "request_daemon", return_value={"ok": False, "protocol_error": "wire"}):
            with self.assertRaisesRegex(core.RelayError, "wire"):
                ssh_relay._transfer_request(self.session, "upload", idle_timeout=2, remaining=5)

    def test_job_context_and_service_error_contract(self) -> None:
        args = argparse.Namespace(name="ci", job="build")
        with patch.object(ssh_relay, "read_session", return_value=self.session):
            session, job_name, timeout = ssh_relay._job_context(args)
        self.assertIs(session, self.session)
        self.assertEqual("build", job_name)
        self.assertEqual(27, timeout)

        with self.assertRaises(core.RelayError):
            ssh_relay._job_context(argparse.Namespace(name="ci", job="bad/name"))

        with patch.object(ssh_relay, "read_session", return_value=self.session):
            _, job_name, timeout = ssh_relay._job_context(argparse.Namespace(name="ci"), require_job=False)
        self.assertIsNone(job_name)
        self.assertEqual(27, timeout)

        self.assertEqual("wire", ssh_relay._service_error({"ok": False, "protocol_error": "wire"}))
        with patch.object(ssh_relay.relay_jobs, "classify_job_command_failure", return_value="job_active_exists"):
            self.assertIn("уже существует", ssh_relay._service_error({"ok": True, "exit_code": 17, "stdout": ""}))
        with patch.object(ssh_relay.relay_jobs, "classify_job_command_failure", return_value="future_reason"):
            self.assertEqual(
                "Служебная job-команда завершилась с ошибкой: future_reason.",
                ssh_relay._service_error({"ok": True, "exit_code": 70, "stdout": ""}),
            )
        with patch.object(ssh_relay.relay_jobs, "classify_job_command_failure", return_value=None):
            self.assertEqual("boom", ssh_relay._service_error({"ok": True, "exit_code": 2, "stderr": "boom\n"}))
            self.assertEqual(
                "Служебная job-команда завершилась с кодом 3.",
                ssh_relay._service_error({"ok": True, "exit_code": 3, "stderr": ""}),
            )
            self.assertIsNone(ssh_relay._service_error({"ok": True, "exit_code": 0, "stdout": ""}))

    def test_status_from_control_and_print_job_status(self) -> None:
        parsed = self.job_status("succeeded", 0)
        with patch.object(ssh_relay, "_service_error", return_value=None), patch.object(
            ssh_relay.relay_jobs, "parse_job_status", return_value=parsed
        ):
            self.assertEqual(parsed, ssh_relay._status_from_control({"ok": True, "stdout": "payload"}))
        with patch.object(ssh_relay, "_service_error", return_value="service failed"):
            with self.assertRaisesRegex(core.RelayError, "service failed"):
                ssh_relay._status_from_control({"ok": True})

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            ssh_relay.print_job_status(parsed)
        self.assertIn("Job: build", stdout.getvalue())
        self.assertIn("Код завершения: 0", stdout.getvalue())

    def test_job_start_success_and_error_paths(self) -> None:
        args = self.parser.parse_args(["job", "start", "--name", "ci", "--job", "build", "sleep 1"])
        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay, "_run_job_control", return_value={"ok": True, "stdout": "payload", "exit_code": 0}),
            patch.object(ssh_relay, "_service_error", return_value=None),
            patch.object(ssh_relay.relay_jobs, "parse_job_status", return_value=self.job_status("running")),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(0, ssh_relay.job_start_cmd(args))
        self.assertIn("Запуск механизма job подтверждён", captured.getvalue())

        stderr = io.StringIO()
        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(
                ssh_relay,
                "_run_job_control",
                return_value={"ok": False, "protocol_error": "Результат операции неизвестен: transport lost"},
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(1, ssh_relay.job_start_cmd(args))
        self.assertIn("Не повторяйте job start автоматически", stderr.getvalue())

        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay, "_run_job_control", return_value={"ok": True, "exit_code": 17, "stdout": ""}),
            patch.object(ssh_relay, "_service_error", return_value="duplicate"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, ssh_relay.job_start_cmd(args))
        with patch.object(ssh_relay, "_job_context", side_effect=core.RelayError("bad session")), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(1, ssh_relay.job_start_cmd(args))

    def test_job_status_tail_stop_and_list_paths(self) -> None:
        status_args = self.parser.parse_args(["job", "status", "--name", "ci", "--job", "build"])
        for state, expected in (("running", 0), ("unknown", 1)):
            with self.subTest(status=state):
                with (
                    patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
                    patch.object(ssh_relay, "_job_status_request", return_value=self.job_status(state)),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(expected, ssh_relay.job_status_cmd(status_args))

        tail_args = self.parser.parse_args(["job", "tail", "--name", "ci", "--job", "build"])
        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay, "_run_job_control", return_value={"ok": True, "stdout": "line\n", "exit_code": 0}),
            patch.object(ssh_relay, "_service_error", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(0, ssh_relay.job_tail_cmd(tail_args))
        self.assertEqual("line\n", captured.getvalue())

        stop_args = self.parser.parse_args(["job", "stop", "--name", "ci", "--job", "build"])
        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay, "_run_job_control", return_value={"ok": True, "stdout": "payload", "exit_code": 0}),
            patch.object(ssh_relay, "_service_error", return_value=None),
            patch.object(ssh_relay.relay_jobs, "parse_job_status", return_value=self.job_status("failed", 143)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, ssh_relay.job_stop_cmd(stop_args))

        list_args = self.parser.parse_args(["job", "list", "--name", "ci"])
        for items, expected_text in (([], "не найдены"), ([self.job_status("running")], "Job\tСостояние")):
            with self.subTest(items=bool(items)):
                with (
                    patch.object(ssh_relay, "_job_context", return_value=(self.session, None, 27)),
                    patch.object(ssh_relay, "_run_job_control", return_value={"ok": True, "stdout": "payload", "exit_code": 0}),
                    patch.object(ssh_relay, "_service_error", return_value=None),
                    patch.object(ssh_relay.relay_jobs, "parse_job_list", return_value=items),
                    contextlib.redirect_stdout(io.StringIO()) as captured,
                ):
                    self.assertEqual(0, ssh_relay.job_list_cmd(list_args))
                self.assertIn(expected_text, captured.getvalue())

        for handler, args in (
            (ssh_relay.job_status_cmd, status_args),
            (ssh_relay.job_tail_cmd, tail_args),
            (ssh_relay.job_stop_cmd, stop_args),
            (ssh_relay.job_list_cmd, list_args),
        ):
            with self.subTest(error_handler=handler.__name__), patch.object(
                ssh_relay, "_job_context", side_effect=core.RelayError("session failed")
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, handler(args))

    def test_job_wait_terminal_timeout_and_transport_errors(self) -> None:
        args = self.parser.parse_args(
            ["job", "wait", "--name", "ci", "--job", "build", "--poll-interval", "0.1", "--timeout", "1"]
        )
        for status, expected in ((self.job_status("succeeded", 0), 0), (self.job_status("failed", 7), 7)):
            with self.subTest(wait_state=status["state"]):
                with (
                    patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
                    patch.object(ssh_relay.time, "monotonic", side_effect=[100.0, 100.1]),
                    patch.object(ssh_relay, "request_daemon", return_value={"ok": True, "ssh_status": "connected"}),
                    patch.object(ssh_relay, "_job_status_request", return_value=status),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(expected, ssh_relay.job_wait_cmd(args))

        stderr = io.StringIO()
        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay.time, "monotonic", side_effect=[100.0, 102.0]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(124, ssh_relay.job_wait_cmd(args))
        self.assertIn("удалённая задача не остановлена", stderr.getvalue())

        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay.time, "monotonic", side_effect=[100.0, 100.1]),
            patch.object(ssh_relay, "request_daemon", side_effect=core.DaemonUnavailableError("local transport lost")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, ssh_relay.job_wait_cmd(args))

        with (
            patch.object(ssh_relay, "_job_context", return_value=(self.session, "build", 27)),
            patch.object(ssh_relay.time, "monotonic", side_effect=[100.0, 100.1]),
            patch.object(ssh_relay, "request_daemon", return_value={"ok": True, "ssh_status": "connected"}),
            patch.object(ssh_relay, "_job_status_request", side_effect=core.RelayError("status failed")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, ssh_relay.job_wait_cmd(args))

    def test_daemon_wrapper_restores_core_file_after_exception(self) -> None:
        original_file = ssh_relay._core.__file__
        with patch.object(ssh_relay._core, "daemon", side_effect=core.RelayError("boom")):
            with self.assertRaises(core.RelayError):
                ssh_relay.daemon(object())
        self.assertEqual(original_file, ssh_relay._core.__file__)

    def test_print_command_result_and_exec_sudo_error_paths(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(7, core.print_command_result({"ok": True, "stdout": "out\n", "stderr": "err\n", "exit_code": 7}))
        self.assertEqual("out\n", stdout.getvalue())
        self.assertEqual("err\n", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            self.assertEqual(1, core.print_command_result({"ok": False, "protocol_error": "wire"}))
        self.assertIn("wire", captured.getvalue())

        exec_args = argparse.Namespace(name="ci", remote_command="true", risky=False, receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH)
        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(core, "request_daemon", side_effect=core.DaemonUnavailableError("lost")),
            patch.object(core, "remove_session_file") as remove,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, core.exec_cmd(exec_args))
        remove.assert_called_once_with("ci")

        sudo_args = argparse.Namespace(name="ci", remote_command="id", risky=False, receipt_path=core.DEFAULT_RISKY_RECEIPT_PATH)
        with patch.object(core, "read_session", side_effect=core.RelayError("bad session")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, core.sudo_exec_cmd(sudo_args))
        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(core, "request_daemon", return_value={"ok": True, "stdout": "root\n", "stderr": "", "exit_code": 0}),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(0, core.sudo_exec_cmd(sudo_args))
        self.assertEqual("root\n", captured.getvalue())

    def test_stop_status_and_list_collection_paths(self) -> None:
        stop_all = argparse.Namespace(all=True, name="ci")
        with patch.object(core, "iter_session_names", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(0, core.stop(stop_all))
        self.assertIn("не найдены", captured.getvalue())
        with patch.object(core, "iter_session_names", return_value=["a", "b"]), patch.object(core, "stop_one_session", side_effect=[0, 1]):
            self.assertEqual(1, core.stop(stop_all))

        status_all = argparse.Namespace(all=True, name="ci")
        with patch.object(core, "iter_session_names", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, core.status(status_all))
        with patch.object(core, "iter_session_names", return_value=["a", "b"]), patch.object(
            core, "status_one_session", side_effect=[0, 1]
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, core.status(status_all))

        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(
                core,
                "request_daemon",
                return_value={"ok": True, "daemon_status": "active", "ssh_status": "connected", "version": ssh_relay.__version__, "sudo_enabled": False},
            ),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(0, core.status_one_session("ci", cleanup_stale=True))
        self.assertIn("SSH: подключено", captured.getvalue())

        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(
                core,
                "request_daemon",
                return_value={"ok": True, "daemon_status": "active", "ssh_status": "reconnecting", "reconnect_attempt": 3, "last_error": "network down", "version": ssh_relay.__version__, "sudo_enabled": True},
            ),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(1, core.status_one_session("ci", cleanup_stale=False))
        self.assertIn("network down", captured.getvalue())

        with (
            patch.object(core, "read_session", return_value=self.session),
            patch.object(core, "request_daemon", side_effect=core.DaemonUnavailableError("lost")),
            patch.object(core, "remove_session_file") as remove,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, core.status_one_session("ci", cleanup_stale=True))
        remove.assert_called_once_with("ci")

        names = ["up", "recon", "down", "bad", "broken"]

        def read_session(name: str) -> dict:
            if name == "broken":
                raise core.RelayError("corrupt")
            session = dict(self.session)
            session["name"] = name
            return session

        def request_daemon(session: dict, action: str, **_kwargs) -> dict:
            self.assertEqual("status", action)
            if session["name"] == "up":
                return {"ok": True, "ssh_status": "connected", "sudo_enabled": False, "version": ssh_relay.__version__}
            if session["name"] == "recon":
                return {"ok": True, "ssh_status": "reconnecting", "sudo_enabled": True, "version": ssh_relay.__version__}
            if session["name"] == "down":
                return {"ok": True, "ssh_status": "disconnected", "sudo_enabled": False, "version": ssh_relay.__version__}
            return {"ok": False, "protocol_error": "bad"}

        stdout = io.StringIO()
        with (
            patch.object(core, "iter_session_names", return_value=names),
            patch.object(core, "read_session", side_effect=read_session),
            patch.object(core, "request_daemon", side_effect=request_daemon),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(1, core.list_sessions(argparse.Namespace()))
        text = stdout.getvalue()
        self.assertIn("up\tактивна", text)
        self.assertIn("recon\tвосстановление", text)
        self.assertIn("down\tSSH недоступен", text)
        self.assertIn("bad\tошибка", text)
        self.assertIn("broken\tнедоступна\t?", text)

    def test_risky_receipt_and_sudo_helpers(self) -> None:
        self.assertEqual('"$HOME"', core.quote_posix_path("~"))
        self.assertIn('"$HOME"/', core.quote_posix_path("~/a b"))
        self.assertIn("'", core.quote_posix_path("/tmp/a b"))
        with self.assertRaises(core.RelayError):
            core.build_risky_receipt_command(path=" ", session=self.session, action="exec", command="true", sudo=False)

        command = core.build_risky_receipt_command(
            path="~/.local/state/agent-safe/changes.jsonl", session=self.session, action="exec", command="printf 'x'", sudo=False
        )
        self.assertIn("mkdir -p", command)
        self.assertIn("changes.jsonl", command)

        with patch.object(core, "execute_remote_command", return_value={"ok": True, "exit_code": 0}) as execute:
            result = core.execute_risky_receipt(
                object(), session=self.session, action="exec", command="true", sudo=False,
                receipt_path="~/.local/state/agent-safe/changes.jsonl", timeout_seconds=5, sudo_password=None,
            )
        execute.assert_called_once()
        self.assertIn("receipt_command", result)

        with self.assertRaises(core.RelayError):
            core.execute_risky_receipt(
                object(), session=self.session, action="sudo_exec", command="id", sudo=True,
                receipt_path="~/changes.jsonl", timeout_seconds=5, sudo_password=None,
            )
        with patch.object(core, "execute_sudo_command", return_value={"ok": True, "exit_code": 0}):
            self.assertEqual(
                "~/changes.jsonl",
                core.execute_risky_receipt(
                    object(), session=self.session, action="sudo_exec", command="id", sudo=True,
                    receipt_path="~/changes.jsonl", timeout_seconds=5, sudo_password="secret",
                )["receipt_path"],
            )

        with patch.object(core, "execute_remote_command", return_value={"ok": True, "exit_code": 0}) as execute:
            core.verify_sudo_password(object(), "secret", 5)
        self.assertEqual(b"secret\n", execute.call_args.kwargs["stdin_data"])
        with patch.object(core, "execute_remote_command", return_value={"ok": True, "exit_code": 1}):
            with self.assertRaisesRegex(core.RelayError, "Проверка sudo-пароля"):
                core.verify_sudo_password(object(), "secret", 5)
        with patch.object(core, "execute_remote_command", return_value={"ok": True, "exit_code": 0}) as execute:
            core.execute_sudo_command(object(), "printf x", 5, "secret")
        self.assertIn("sudo -S -p '' -- sh -c", execute.call_args.args[1])
        self.assertEqual(b"secret\n", execute.call_args.kwargs["stdin_data"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
