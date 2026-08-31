import io
import unittest
from datetime import datetime, timedelta, timezone

from ssh_relay_logging import TimestampedDaemonStream, format_local_timestamp


class DaemonLoggingTests(unittest.TestCase):
    def test_timestamp_has_milliseconds_and_offset(self):
        moment = datetime(2026, 8, 31, 11, 53, 2, 184000, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual("2026-08-31T11:53:02.184+03:00", format_local_timestamp(moment))

    def test_print_style_writes_get_one_prefix(self):
        raw = io.StringIO()
        stream = TimestampedDaemonStream(raw, timestamp_factory=lambda: "2026-08-31T11:53:02.184+03:00")
        stream.write("SSH-соединение установлено")
        stream.write("\n")
        self.assertEqual(
            "[2026-08-31T11:53:02.184+03:00] SSH-соединение установлено\n",
            raw.getvalue(),
        )

    def test_multiline_write_prefixes_each_nonempty_line(self):
        raw = io.StringIO()
        stream = TimestampedDaemonStream(raw, timestamp_factory=lambda: "T")
        stream.write("первая\nвторая\n")
        self.assertEqual("[T] первая\n[T] вторая\n", raw.getvalue())

    def test_password_prompt_is_not_prefixed_or_buffered(self):
        raw = io.StringIO()
        stream = TimestampedDaemonStream(raw, timestamp_factory=lambda: "T")
        stream.write("SSH-пароль для user@example: ")
        stream.flush()
        self.assertEqual("SSH-пароль для user@example: ", raw.getvalue())

    def test_partial_diagnostic_is_emitted_on_flush(self):
        raw = io.StringIO()
        stream = TimestampedDaemonStream(raw, timestamp_factory=lambda: "T")
        stream.write("Socket exception: No route to host (113)")
        stream.flush()
        self.assertEqual("[T] Socket exception: No route to host (113)", raw.getvalue())


if __name__ == "__main__":
    unittest.main()
