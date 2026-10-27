"""Tests of the per-session memory of vnc / vncreport (run: python -m unittest test_vnc_mem)"""

import unittest

import vncreport
import vnc

SCOPE = "/user.slice/user-1000.slice/session-2.scope"


def ps(pid, ppid, sid, user, rss, args):
    return "%d %d %d %s 100000 %d %s" % (pid, ppid, sid, user, rss, args)


def cgroup(pid, scope=SCOPE):
    return "/proc/%d/cgroup:1:name=systemd:%s" % (pid, scope)


def report(ps_lines, cgroup_lines):
    return "\n".join(ps_lines + ["@@MEM", "MemTotal: 1000 kB", "MemAvailable: 400 kB", "@@USERS", "@@UNITS",
                                 "@@CGROUP"] + cgroup_lines)


def mem_of(text, display):
    rows = dict((r[0], r) for r in vncreport.parse(text)[2])
    return rows[display][5]


# Comodel2 :1 (admin): Xvnc, xstartup and startkde share SID 8850 (59000 kB = 58 MB), but plasma-desktop,
# kded4 and mysqld daemonized (ppid 1, new SID) and are only in the login session scope.
COMODEL2_PS = [
    ps(8854, 1, 8850, "admin", 52000, "/usr/bin/Xvnc :1 -auth /home/admin/.Xauthority"),
    ps(8855, 1, 8850, "admin", 3000, "/bin/sh /home/admin/.vnc/xstartup"),
    ps(8860, 8855, 8850, "admin", 4000, "/usr/bin/startkde"),
    ps(9141, 1, 9141, "admin", 380000, "/usr/bin/plasma-desktop"),
    ps(9100, 1, 9100, "admin", 90000, "kdeinit4: kded4"),
    ps(9200, 1, 9200, "admin", 300000, "/usr/sbin/mysqld"),
    ps(9210, 1, 9210, "admin", 118200, "/usr/bin/akonadi_control"),
    ps(9900, 1, 9900, "other", 500000, "/usr/bin/firefox"),  # another login
    ps(1100, 1, 1100, "root", 200000, "/usr/sbin/some-daemon"),  # not in any session scope
]
COMODEL2_CGROUP = [cgroup(p) for p in (8854, 8855, 8860, 9141, 9100, 9200, 9210)] + \
    [cgroup(9900, "/user.slice/user-1001.slice/session-5.scope")]


class SessionMemory(unittest.TestCase):
    def test_comodel2_counts_daemonized_kde_processes(self):
        mem = mem_of(report(COMODEL2_PS, COMODEL2_CGROUP), 1)
        self.assertEqual(mem, 947200)  # 925 MB, not 58 MB
        self.assertEqual(round(mem / 1024.0), 925)

    def test_without_scope_falls_back_to_sid(self):
        mem = mem_of(report(COMODEL2_PS, []), 1)
        self.assertEqual(mem, 59000)  # the old, too low value

    def test_two_vnc_servers_in_one_scope_are_not_double_counted(self):
        lines = [
            ps(10, 1, 10, "admin", 50000, "/usr/bin/Xvnc :1 -auth x"),
            ps(11, 10, 10, "admin", 5000, "/usr/bin/startkde"),
            ps(20, 1, 20, "admin", 60000, "/usr/bin/Xvnc :2 -auth x"),
            ps(21, 20, 20, "admin", 6000, "/usr/bin/startkde"),
            ps(30, 1, 30, "admin", 400000, "/usr/bin/plasma-desktop"),
        ]
        text = report(lines, [cgroup(p) for p in (10, 11, 20, 21, 30)])
        self.assertEqual(mem_of(text, 1), 55000)
        self.assertEqual(mem_of(text, 2), 66000)

    def test_xrdp_keeps_the_sesexec_tree(self):
        lines = [
            ps(7000, 1486, 1486, "root", 8000, "xrdp-sesexec"),
            ps(7001, 7000, 1486, "admin", 100000, "Xvnc :10 -auth .Xauthority -rfbunixpath /run/xrdp/1000/x"),
            ps(7002, 7000, 1486, "admin", 5000, "/usr/sbin/xrdp-chansrv"),
            ps(7100, 1, 7100, "admin", 999999, "/usr/bin/other-in-same-scope"),
        ]
        text = report(lines, [cgroup(p) for p in (7001, 7002, 7100)])
        self.assertEqual(mem_of(text, 10), 113000)


class PsCommand(unittest.TestCase):
    ONE_O_PER_COLUMN = "ps -e -o pid= -o ppid= -o sid= -o user:64= -o etimes= -o rss= -o args="

    def test_remote_command_uses_one_o_per_column(self):
        self.assertTrue(vncreport.REMOTE.startswith(self.ONE_O_PER_COLUMN + ";"))
        self.assertNotIn("-eo", vncreport.REMOTE)

    def test_local_ps_call_uses_one_o_per_column(self):
        calls = []
        real = vnc.capture
        vnc.capture = lambda *cmd: calls.append(" ".join(cmd)) or (0, "")
        try:
            vnc.processes()
        finally:
            vnc.capture = real
        self.assertEqual(calls[0], self.ONE_O_PER_COLUMN)


class OwnAddress(unittest.TestCase):
    def test_own_ip_runs_locally_others_over_ssh(self):
        local = set(["192.168.1.212"])
        self.assertEqual(vncreport.command_for("192.168.1.212", local), ["sh", "-c", vncreport.REMOTE])
        other = vncreport.command_for("192.168.1.17", local)
        self.assertEqual(other[0], "ssh")
        self.assertEqual(other[-2:], ["refuser@192.168.1.17", vncreport.REMOTE])

    def test_local_run_collects_without_ssh(self):
        status, text = vncreport.collect("192.168.1.212", set(["192.168.1.212"]))
        self.assertEqual(status, "ok")
        self.assertIn("@@MEM", text)
        self.assertIn("MemTotal", text)

    def test_hostnames(self):
        self.assertEqual(dict(vncreport.SERVERS)["192.168.1.212"], "srv-rocky-1")
        text = "@@HOST\nsrv-a\n@@MEM"
        self.assertEqual(vncreport.hostname_of(text), "srv-a")
        self.assertEqual(vncreport.label_of("192.168.1.210", "-", "srv-a"), "srv-a")
        self.assertEqual(vncreport.label_of("192.168.1.15", "Runtime4", "srv-a"), "Runtime4")


if __name__ == "__main__":
    unittest.main()
