"""Self-service session commands: python3 -m unittest test_vnc_sessions."""

import io
import unittest
from unittest.mock import mock_open, patch

import vnc


class SelfService(unittest.TestCase):
    def setUp(self):
        patches = {
            "caller": patch.object(vnc, "caller", return_value="alice"),
            "root": patch.object(vnc, "is_root", return_value=False),
            "file": patch.object(vnc.io, "open", mock_open(read_data=":3=alice\n:4=bob\n")),
            "running": patch.object(vnc, "xvnc_sessions", return_value={}),
            "states": patch.object(vnc, "states", return_value={3: "stopped", 5: "running"}),
            "state": patch.object(vnc, "state_of", return_value="running"),
            "owner": patch.object(vnc, "require_owner"),
            "run": patch.object(vnc, "run", return_value=True),
            "log": patch.object(vnc, "log"),
            "action": patch.object(vnc, "action", ""),
            "stdout": patch("sys.stdout", new_callable=io.StringIO),
            "stderr": patch("sys.stderr", new_callable=io.StringIO),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def invoke_error(self, argv, code):
        with self.assertRaises(SystemExit) as result:
            vnc.main(argv)
        self.assertEqual(result.exception.code, code)
        self.mocks["run"].assert_not_called()
        self.assertIn("STATUS=error", self.mocks["stdout"].getvalue())

    def test_commands_infer_user_and_keep_assignment(self):
        for command, verb, state in (("start", "start", "running"),
                                     ("kill", "stop", "stopped"),
                                     ("restart", "restart", "running")):
            with self.subTest(command=command), \
                    patch.object(vnc, "remove_line") as remove, patch.object(vnc, "lock") as lock:
                self.mocks["state"].return_value = state
                vnc.main([command])
                self.mocks["run"].assert_called_with(
                    "/usr/bin/sudo", "-n", "/usr/bin/systemctl", verb, "vncserver@:3.service")
                self.mocks["owner"].assert_called_with("alice", 3)
                remove.assert_not_called()
                lock.assert_not_called()
        self.assertIn("assignment kept", self.mocks["stdout"].getvalue())

    def test_multiple_assignments_prompt_and_choose_one(self):
        self.mocks["file"].return_value.read.return_value = ":3=alice\n:5=alice\n:4=bob\n"
        with patch("sys.stdin", io.StringIO("2\n")):
            vnc.main(["start"])
        self.mocks["run"].assert_called_once_with(
            "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "vncserver@:5.service")
        self.assertIn("1) :3", self.mocks["stdout"].getvalue())
        self.assertIn("2) :5", self.mocks["stdout"].getvalue())
        self.assertNotIn(":4", self.mocks["stdout"].getvalue())

    def test_invalid_selection_and_eof_do_not_change_sessions(self):
        self.mocks["file"].return_value.read.return_value = ":3=alice\n:5=alice\n"
        for answer in ("", "0\n", "3\n", "invalid\n"):
            with self.subTest(answer=answer), patch("sys.stdin", io.StringIO(answer)):
                self.invoke_error(["start"], 2)

    def test_no_assignment(self):
        self.mocks["file"].return_value.read.return_value = ":4=bob\n"
        self.invoke_error(["start"], 3)

    def test_unreadable_configuration(self):
        self.mocks["file"].side_effect = IOError("permission denied")
        self.invoke_error(["start"], 4)

    def test_duplicate_assignment(self):
        self.mocks["file"].return_value.read.return_value = ":3=alice\n:3=bob\n"
        self.invoke_error(["start"], 4)

    def test_process_owner_mismatch_or_xrdp(self):
        for user, xrdp in (("bob", False), ("alice", True)):
            self.mocks["running"].return_value = {3: (user, "10.2026", 2048, xrdp, "2 days")}
            self.invoke_error(["restart"], 3)

    def test_denied_systemctl_reports_error(self):
        self.mocks["run"].return_value = False
        with self.assertRaises(SystemExit) as result:
            vnc.main(["start"])
        self.assertEqual(result.exception.code, 4)
        self.assertIn("passwordless", self.mocks["stderr"].getvalue())
        self.assertNotIn("STATUS=running", self.mocks["stdout"].getvalue())

    def test_unexpected_service_state_reports_error(self):
        self.mocks["state"].return_value = "failed"
        with self.assertRaises(SystemExit) as result:
            vnc.main(["start"])
        self.assertEqual(result.exception.code, 4)
        self.assertNotIn("STATUS=running", self.mocks["stdout"].getvalue())

    def test_explicit_target_requires_root(self):
        for argv in (["kill", "bob", ":4"], ["restart", "bob", ":4"]):
            self.invoke_error(argv, 2)

    def test_start_rejects_arguments(self):
        self.invoke_error(["start", ":4"], 2)

    def test_root_uses_systemctl_without_sudo(self):
        self.mocks["root"].return_value = True
        vnc.main(["start"])
        self.mocks["run"].assert_called_once_with(
            "/usr/bin/systemctl", "start", "vncserver@:3.service")

    def test_administrator_kill_still_removes_assignment(self):
        self.mocks["root"].return_value = True
        with patch.object(vnc, "lock"), patch.object(vnc, "remove_line") as remove:
            vnc.main(["kill", "bob", ":4"])
        self.mocks["run"].assert_called_once_with(
            "systemctl", "disable", "--now", "vncserver@:4.service")
        remove.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
