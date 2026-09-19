"""Справка argparse без дат; журнал daemon с датами после разбора аргументов."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ssh_relay
import ssh_relay_core as core
import ssh_relay_daemon_launcher as launcher
import ssh_relay_entrypoint as entrypoint


TIMESTAMP = r"\[\d{4}-\d{2}-\d{2}T[^\]]+\] "


class ArgparseOutputTests(unittest.TestCase):
    def invoke(self, callback, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["ssh_relay", *arguments]), \
                mock.patch.object(ssh_relay, "__file__", ssh_relay.__file__), \
                mock.patch.object(entrypoint, "record_invocation_identity"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = callback()
            except SystemExit as exc:
                result = exc.code
        return result, stdout.getvalue(), stderr.getvalue()

    def test_all_help_matches_argparse_exactly(self):
        def help_cases(parser, prefix=()):
            yield [*prefix, "--help"]
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        yield from help_cases(child, (*prefix, name))

        with mock.patch.object(core, "daemon", side_effect=AssertionError("daemon не должен запускаться")):
            for arguments in help_cases(ssh_relay.build_parser()):
                expected = self.invoke(lambda: ssh_relay.build_parser().parse_args(), arguments)
                self.assertEqual(0, expected[0])
                self.assertEqual("", expected[2])
                self.assertNotRegex(expected[1], TIMESTAMP)
                for callback in (ssh_relay.main, entrypoint.main, launcher.main):
                    with self.subTest(entry=callback.__module__, arguments=arguments):
                        self.assertEqual(expected, self.invoke(callback, arguments))

    def test_parse_errors_keep_stderr_format_and_exit_two(self):
        cases = [
            ["daemon"],
            ["daemon", "--host", "198.51.100.42", "--port", "не-число"],
            ["daemon", "--host", "198.51.100.42", "--unknown-option"],
            ["daemon", "--detach"],
            ["exec"],
            ["job", "tail"],
        ]
        with mock.patch.object(core, "daemon", side_effect=AssertionError("daemon не должен запускаться")):
            for arguments in cases:
                expected = self.invoke(lambda: ssh_relay.build_parser().parse_args(), arguments)
                self.assertEqual(2, expected[0])
                self.assertEqual("", expected[1])
                self.assertIn("использование:", expected[2])
                self.assertNotRegex(expected[2], TIMESTAMP)
                for callback in (ssh_relay.main, entrypoint.main, launcher.main):
                    with self.subTest(entry=callback.__module__, arguments=arguments):
                        self.assertEqual(expected, self.invoke(callback, arguments))

    def test_daemon_diagnostics_keep_one_timestamp_and_exit_code(self):
        def fake_daemon(args):
            print("Событие daemon")
            print("Диагностика daemon", file=sys.stderr)
            return 17

        for callback in (ssh_relay.main, entrypoint.main, launcher.main):
            for detached in (False, True):
                with self.subTest(entry=callback.__module__, detached=detached), \
                        mock.patch.object(core, "daemon", side_effect=fake_daemon) as daemon:
                    arguments = ["daemon", "--host", "198.51.100.42", "--user", "donpedro"]
                    if detached:
                        arguments += ["--detach", "--identity-file", "test-key"]
                    result, stdout, stderr = self.invoke(callback, arguments)
                    self.assertEqual(17, result)
                    self.assertRegex(stdout, "^" + TIMESTAMP + "Событие daemon\n$")
                    self.assertRegex(stderr, "^" + TIMESTAMP + "Диагностика daemon\n$")
                    self.assertEqual(detached, daemon.call_args.args[0].detach)
                    daemon.assert_called_once()

    def test_process_help_and_parse_error_do_not_gain_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ, PYTHONIOENCODING="utf-8",
                               SSH_RELAY_DIAGNOSTIC_LOG=str(Path(temporary) / "diagnostic.log"))
            for module in (ssh_relay, entrypoint, launcher):
                for arguments, code in ((["daemon", "--help"], 0), (["daemon"], 2)):
                    with self.subTest(entry=module.__name__, arguments=arguments):
                        result = subprocess.run(
                            [sys.executable, str(Path(module.__file__).resolve()), *arguments],
                            env=environment, capture_output=True, text=True, encoding="utf-8",
                            timeout=10, check=False,
                        )
                        self.assertEqual(code, result.returncode, result.stderr)
                        output = result.stdout if code == 0 else result.stderr
                        self.assertTrue(output.startswith("использование:"), output)
                        self.assertNotRegex(output, TIMESTAMP)
                        self.assertEqual("", result.stderr if code == 0 else result.stdout)


if __name__ == "__main__":
    unittest.main()
