#!/usr/bin/env python3
"""Cross-platform тесты публичного CLI-контракта ssh_relay."""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core


ROOT = Path(__file__).resolve().parents[1]


class CliContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ssh_relay.build_parser()

    @staticmethod
    def subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        raise AssertionError("В parser отсутствуют подкоманды.")

    def parse_help(self, arguments: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as captured:
                self.parser.parse_args([*arguments, "--help"])
        self.assertEqual(0, captured.exception.code)
        return stdout.getvalue()

    def test_top_level_and_job_commands_are_stable(self) -> None:
        top = self.subparsers(self.parser)
        self.assertEqual(
            {"daemon", "exec", "sudo-exec", "download", "upload", "status", "stop", "list", "job"},
            set(top.choices),
        )
        job = self.subparsers(top.choices["job"])
        self.assertEqual({"start", "status", "tail", "wait", "stop", "list"}, set(job.choices))

    def test_handlers_route_to_expected_public_operations(self) -> None:
        top = self.subparsers(self.parser)
        self.assertIs(ssh_relay.daemon, top.choices["daemon"].get_default("handler"))
        # exec/sudo-exec теперь имеют dispatch между text-mode и --json; точная
        # семантика обоих путей проверяется отдельными CLI-тестами ниже.
        self.assertTrue(callable(top.choices["exec"].get_default("handler")))
        self.assertTrue(callable(top.choices["sudo-exec"].get_default("handler")))
        self.assertIs(ssh_relay.download_cmd, top.choices["download"].get_default("handler"))
        self.assertIs(ssh_relay.upload_cmd, top.choices["upload"].get_default("handler"))
        self.assertIs(core.status, top.choices["status"].get_default("handler"))
        self.assertIs(core.stop, top.choices["stop"].get_default("handler"))
        self.assertIs(core.list_sessions, top.choices["list"].get_default("handler"))

        job = self.subparsers(top.choices["job"])
        expected = {
            "start": ssh_relay.job_start_cmd,
            "status": ssh_relay.job_status_cmd,
            "tail": ssh_relay.job_tail_cmd,
            "wait": ssh_relay.job_wait_cmd,
            "stop": ssh_relay.job_stop_cmd,
            "list": ssh_relay.job_list_cmd,
        }
        for name, handler in expected.items():
            self.assertIs(handler, job.choices[name].get_default("handler"), name)

    def test_version_entrypoint_is_exact_and_successful(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ssh_relay.py"), "--version"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(f"ssh_relay {ssh_relay.__version__}\n", result.stdout)
        self.assertEqual("", result.stderr)

    def test_help_is_russian_and_available_for_all_commands(self) -> None:
        top_help = self.parse_help([])
        self.assertIn("использование:", top_help)
        self.assertIn("Локальный SSH-relay", top_help)

        for command in ("daemon", "exec", "sudo-exec", "download", "upload", "status", "stop", "list", "job"):
            help_text = self.parse_help([command])
            self.assertIn("использование:", help_text, command)

        for command in ("start", "status", "tail", "wait", "stop", "list"):
            help_text = self.parse_help(["job", command])
            self.assertIn("использование:", help_text, f"job {command}")

    def test_exec_preserves_stdout_stderr_and_remote_exit_code(self) -> None:
        args = self.parser.parse_args(["exec", "--name", "ci", "printf test"])
        session = {
            "name": "ci",
            "command_timeout": 30,
            "reconnect_wait": 30,
            "auth_token": "token",
            "daemon_port": 41000,
        }
        result = {
            "ok": True,
            "stdout": "remote-out\n",
            "stderr": "remote-err\n",
            "exit_code": 7,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(core, "read_session", return_value=session),
            patch.object(core, "request_daemon", return_value=result) as request,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = args.handler(args)

        self.assertEqual(7, exit_code)
        self.assertEqual("remote-out\n", stdout.getvalue())
        self.assertEqual("remote-err\n", stderr.getvalue())
        request.assert_called_once()
        self.assertEqual("exec", request.call_args.args[1])
        self.assertEqual("printf test", request.call_args.kwargs["command"])

    def test_exec_protocol_error_returns_one_on_stderr_only(self) -> None:
        args = self.parser.parse_args(["exec", "--name", "ci", "false"])
        session = {
            "name": "ci",
            "command_timeout": 30,
            "reconnect_wait": 30,
            "auth_token": "token",
            "daemon_port": 41000,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(core, "read_session", return_value=session),
            patch.object(core, "request_daemon", return_value={"ok": False, "protocol_error": "тестовая ошибка"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = args.handler(args)

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("Ошибка relay: тестовая ошибка\n", stderr.getvalue())

    def test_status_invalid_session_name_returns_usage_error_code_two(self) -> None:
        args = self.parser.parse_args(["status", "--name", "../bad"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = args.handler(args)
        self.assertEqual(2, exit_code)
        self.assertIn("Недопустимое имя сессии", stderr.getvalue())

    def test_transfer_idle_timeout_rejects_zero_and_negative_values(self) -> None:
        cases = [
            ["download", "--idle-timeout", "0", "/remote", "local.bin"],
            ["upload", "--idle-timeout", "-1", "local.bin", "/remote"],
        ]
        for arguments in cases:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as captured:
                    self.parser.parse_args(arguments)
            self.assertEqual(2, captured.exception.code)
            self.assertIn("положительным", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
