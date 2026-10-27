"""Tests for the local VNC info display (python3 -m unittest test_vnc_info)."""

import io
import time
import unittest
from unittest.mock import patch

import vnc
import vncreport


class InfoOutput(unittest.TestCase):
    def render(self, owners, running, states, args=(), have_file=True):
        output = io.StringIO()
        with patch.object(vnc.os.path, "exists", return_value=have_file), \
                patch.object(vnc.os, "access", return_value=True), \
                patch.object(vnc, "entries", return_value=owners), \
                patch.object(vnc, "xvnc_sessions", return_value=running), \
                patch.object(vnc, "states", return_value=states), \
                patch.object(vnc.socket, "gethostname", return_value="vnc-server-1"), \
                patch.object(vnc, "action", "info"), \
                patch("sys.stdout", output):
            vnc.do_info(list(args))
        return output.getvalue()

    def test_hostname_and_normal_session(self):
        text = self.render({1: "alice"}, {1: ("alice", "10.2026", 2048, False, "19 days")},
                           {1: "running"})
        self.assertTrue(text.startswith("Server: vnc-server-1\n\n"))
        row = next(line for line in text.splitlines() if line.startswith(":1 "))
        self.assertTrue(row.endswith("2M"))
        self.assertIn("10.2026 (19 days)", row)
        self.assertNotIn("DURATION", text)
        self.assertLess(text.index("STARTED"), text.index("MEM"))
        self.assertIn("Sources:", text)
        self.assertNotIn("STATUS=", text)

    def test_simple_notes_preserve_warnings(self):
        text = self.render({1: "bob"}, {1: ("alice", "-", 2048, False, "00:00:01")}, {1: "failed"})
        self.assertIn("Assigned to bob in settings; Service failed", text)
        text = self.render({}, {2: ("alice", "-", 2048, False, "00:00:01")}, {})
        self.assertIn("Not configured", text)
        text = self.render({1: ""}, {1: ("alice", "-", 2048, False, "00:00:01")}, {1: "stopped"})
        self.assertIn("No user assigned in settings; Service stopped", text)

    def test_xrdp_does_not_warn_about_configuration_or_service(self):
        text = self.render({}, {10: ("alice", "-", 2048, True, "00:00:01")}, {10: "stopped"})
        self.assertIn("Remote desktop (RDP)", text)
        self.assertNotIn("Not configured", text)
        self.assertNotIn("Service stopped", text)

    def test_missing_settings_does_not_mark_session_unconfigured(self):
        text = self.render({}, {1: ("alice", "-", 2048, False, "00:00:01")}, {}, have_file=False)
        self.assertNotIn("Not configured", text)

    def test_single_display_keeps_status(self):
        text = self.render({1: "alice"}, {}, {1: "stopped"}, args=(":1",))
        self.assertTrue(text.startswith("Server: vnc-server-1\n"))
        self.assertTrue(text.endswith("STATUS=stopped\n"))
        self.assertNotIn("(-)", text)

    def test_user_filter(self):
        text = self.render({1: "alice", 2: "bob"}, {}, {1: "stopped", 2: "failed"},
                           args=("alice",))
        self.assertIn(":1 ", text)
        self.assertNotIn(":2 ", text)
        self.assertNotIn("STATUS=", text)

    def test_empty_report_still_identifies_server(self):
        text = self.render({}, {}, {})
        self.assertTrue(text.startswith("Server: vnc-server-1\n"))
        self.assertIn("NOTE", text)
        self.assertNotIn("Sources:", text)


class InfoAll(unittest.TestCase):
    def test_dispatch_preserves_report_exit_code(self):
        for code in (0, 1, 2):
            with self.subTest(code=code), \
                    patch.object(vncreport, "main", return_value=code) as report_main, \
                    patch.object(vnc, "action", ""), \
                    patch.object(vnc.sys, "path", list(vnc.sys.path)), \
                    self.assertRaises(SystemExit) as result:
                vnc.main(["infoall"])
            self.assertEqual(result.exception.code, code)
            report_main.assert_called_once_with([])

    def test_extra_arguments_are_rejected(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(vncreport, "main") as report_main, \
                patch.object(vnc, "log"), \
                patch.object(vnc, "action", ""), \
                patch("sys.stdout", output), patch("sys.stderr", errors), \
                self.assertRaises(SystemExit) as result:
            vnc.main(["infoall", "extra"])
        self.assertEqual(result.exception.code, 2)
        report_main.assert_not_called()
        self.assertIn("infoall", errors.getvalue())
        self.assertNotIn("STATUS=", output.getvalue())

    def test_reports_every_configured_server_and_failures(self):
        output = io.StringIO()
        with patch.object(vncreport, "local_ips", return_value=set()), \
                patch.object(vncreport, "collect", return_value=("error", "no SSH key access")) as collect, \
                patch.object(vncreport, "label_of", return_value="test-server"), \
                patch.object(vnc, "action", ""), \
                patch.object(vnc.sys, "path", list(vnc.sys.path)), \
                patch("sys.stdout", output), self.assertRaises(SystemExit) as result:
            vnc.main(["infoall"])
        self.assertEqual(result.exception.code, 1)
        self.assertCountEqual([call[0][0] for call in collect.call_args_list],
                              [ip for ip, _ in vncreport.SERVERS])
        self.assertIn("ERROR: no SSH key access", output.getvalue())
        self.assertIn("0 of %d servers answered" % len(vncreport.SERVERS), output.getvalue())
        headers = [line.split()[1] for line in output.getvalue().splitlines() if line.startswith("===== ")]
        expected = (["192.168.1.212"] +
                    ["192.168.1.%d" % i for i in range(16, 24)] +
                    ["192.168.1.210", "192.168.1.11"] +
                    ["192.168.1.%d" % i for i in range(12, 16)])
        self.assertEqual(headers, expected)


class SessionTiming(unittest.TestCase):
    def test_combined_start_and_duration(self):
        self.assertEqual(vnc.started_with_duration("10.2026", "19 days"), "10.2026 (19 days)")
        self.assertEqual(vnc.started_with_duration("10.2026", "1 day"), "10.2026 (1 day)")
        self.assertEqual(vnc.started_with_duration("10.2026", "0 days"), "10.2026 (0 days)")
        self.assertEqual(vnc.started_with_duration("-", "-"), "-")

    def test_date_format_and_year_rollover(self):
        now = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
        with patch.object(vnc.time, "time", return_value=now):
            self.assertEqual(vnc.started_at(0), "01.2026")
            self.assertEqual(vnc.started_at(86400), "12.2025")

    def test_completed_days(self):
        for seconds, expected in ((0, "0 days"), (86399, "0 days"), (86400, "1 day"),
                                  (172861, "2 days"), (1671754, "19 days")):
            with self.subTest(seconds=seconds):
                self.assertEqual(vnc.duration_days(seconds), expected)

    def test_duration_boundaries(self):
        for seconds, expected in ((0, "00:00:00"), (59, "00:00:59"), (60, "00:01:00"),
                                  (3600, "01:00:00"), (86400, "1d 00:00:00"),
                                  (172861, "2d 00:01:01")):
            with self.subTest(seconds=seconds):
                self.assertEqual(vnc.human_duration(seconds), expected)

    def test_report_preserves_process_duration(self):
        text = ("10 1 10 alice 172861 2048 /usr/bin/Xvnc :1 -auth x\n"
                "@@MEM\nMemTotal: 10000 kB\nMemAvailable: 5000 kB\n"
                "@@USERS\n:2=bob\n@@UNITS\n@@CGROUP\n")
        rows = vncreport.parse(text)[2]
        self.assertRegex(rows[0][4], r"^\d{2}\.\d{4}$")
        self.assertEqual(rows[0][6], "2 days")
        self.assertEqual(rows[1][4:], ("-", None, "-"))
        output = io.StringIO()
        with patch.object(vncreport, "local_ips", return_value=set()), \
                patch.object(vncreport, "collect", return_value=("ok", text)), \
                patch("sys.stdout", output):
            self.assertEqual(vncreport.main(["192.168.1.212"]), 0)
        rendered = output.getvalue()
        self.assertNotIn("DURATION", rendered)
        self.assertIn(rows[0][4] + " (2 days)", rendered)
        self.assertNotIn("(-)", rendered)
        self.assertLess(rendered.index("STARTED"), rendered.index("MEM"))


if __name__ == "__main__":
    unittest.main()
