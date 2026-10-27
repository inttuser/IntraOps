#!/usr/libexec/platform-python -I
"""vncreport.py - read-only VNC + RAM report of all servers, collected over SSH as refuser

  ./vncreport.py [info] [ip ...]     all servers, or only the given ones

Needs refuser's SSH key on every server and the server's host key in known_hosts.
A server whose host key is not accepted yet is reported as an error and skipped - accept it once
by hand:  ssh refuser@<ip>  (and check the fingerprint).
"""

from __future__ import print_function

import os
import re
import socket
import subprocess
import sys
import threading
from multiprocessing.pool import ThreadPool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vnc  # noqa: E402  (its ps parsing and session / memory logic are reused)

SSH_USER = "refuser"
SERVERS = [("192.168.1.212", "srv-rocky-1")]
SERVERS += [("192.168.1.%d" % (16 + i), "Comodel%d" % (i + 1)) for i in range(8)]
SERVERS += [("192.168.1.210", "-"), ("192.168.1.11", "-")]
SERVERS += [("192.168.1.%d" % (12 + i), "Runtime%d" % (i + 1)) for i in range(4)]
SSH_TIMEOUT = 10  # seconds to connect
RUN_TIMEOUT = 30  # seconds for the whole server
WORKERS = 8

# the only thing run on a server: reads, no changes. Sections are separated by @@ lines.
REMOTE = (" ".join(vnc.PS_COMMAND) + "; "
          "echo @@HOST; hostname 2>/dev/null; "
          "echo @@MEM; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
          "echo @@USERS; cat /etc/tigervnc/vncserver.users 2>/dev/null; "
          "echo @@UNITS; systemctl list-units 'vncserver@*' --all --no-legend --plain 2>/dev/null; "
          "echo @@CGROUP; grep -H session- /proc/[0-9]*/cgroup 2>/dev/null; "
          "true")

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=%d" % SSH_TIMEOUT, "-o", "PasswordAuthentication=no"]


def explain(stderr):
    """ssh error text -> a short reason"""
    for needle, reason in (("Host key verification failed", "host key not accepted yet"),
                           ("Permission denied", "no SSH key access"),
                           ("timed out", "connection timed out"),
                           ("No route to host", "no route to host"),
                           ("Connection refused", "connection refused")):
        if needle in stderr:
            return reason
    lines = stderr.strip().splitlines()
    return lines[-1] if lines else "unknown ssh error"


def local_ips():
    """Addresses of this machine (hostname -I)"""
    return set(vnc.capture("hostname", "-I")[1].split())


def command_for(ip, local):
    """The same read-only REMOTE command: through ssh, or in a local shell on this machine's own addresses"""
    if ip in local:
        return ["sh", "-c", REMOTE]
    return SSH + ["%s@%s" % (SSH_USER, ip), REMOTE]


def collect(ip, local=()):
    """One server -> ("ok", text) or ("error", reason)"""
    timed_out = []
    try:
        with open(os.devnull) as devnull:
            p = subprocess.Popen(command_for(ip, local), stdin=devnull,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            timer = threading.Timer(RUN_TIMEOUT, lambda: (timed_out.append(1), p.kill()))  # no communicate(timeout) on Python 2
            timer.start()
            try:
                stdout, stderr = p.communicate()
            finally:
                timer.cancel()
    except OSError as e:
        return "error", "cannot run ssh: %s" % e
    if timed_out:
        return "error", "no answer in %d s" % RUN_TIMEOUT
    if p.returncode != 0:
        return "error", "local command failed" if ip in local else explain(stderr)
    return "ok", stdout


def hostname_of(text):
    """Hostname reported by the server, empty if none"""
    lines = sections(text).get("HOST", [])
    return lines[0].strip() if lines else ""


def label_of(ip, known, reported):
    """Name shown in the header: the fixed name, else the server's own hostname, else reverse DNS, else -"""
    if known != "-":
        return known
    if reported:
        return reported
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.error, socket.herror, socket.gaierror):
        return "-"


def sections(text):
    """remote output -> {"PS": [lines], "MEM": [...], "USERS": [...], "UNITS": [...], "CGROUP": [...]}"""
    result, current = {"PS": []}, "PS"
    for line in text.splitlines():
        m = vnc.fullmatch(r"@@([A-Z]+)", line)
        if m:
            current = m.group(1)
            result[current] = []
        else:
            result[current].append(line)
    return result


def parse(text):
    """-> (ram_total_kb, ram_available_kb, rows); row: (display, user, kind, status, started, mem_kb, duration)"""
    s = sections(text)
    kb = {}
    for line in s.get("MEM", []):
        m = re.match(r"(MemTotal|MemAvailable):\s+([0-9]+)", line)
        if m:
            kb[m.group(1)] = int(m.group(2))

    running = vnc.xvnc_sessions(vnc.parse_processes(s["PS"], vnc.parse_scopes(s.get("CGROUP", []))))
    owners = {}
    for line in s.get("USERS", []):
        m = vnc.fullmatch(r"\s*:([0-9]{1,2})=(\S*)\s*", line)
        if m:
            owners.setdefault(int(m.group(1)), m.group(2))
    units = {}
    for line in s.get("UNITS", []):
        f = line.split()  # "vncserver@:1.service loaded failed failed Remote desktop service"
        m = vnc.fullmatch(r"vncserver@:([0-9]+)\.service", f[0]) if len(f) > 2 else None
        if m:
            units[int(m.group(1))] = vnc.STATES.get(f[2], "unknown")

    rows = []
    for d in sorted(set(owners) | set(running)):
        if d in running:
            user, started, mem_kb, is_xrdp, duration = running[d]
            rows.append((d, user, "xrdp" if is_xrdp else "vnc", "running", started, mem_kb, duration))
        else:  # a unit that systemd does not list at all is not started
            rows.append((d, owners[d] or "-", "-", units.get(d, "stopped"), "-", None, "-"))
    return kb.get("MemTotal"), kb.get("MemAvailable"), rows


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    names = dict(SERVERS)
    if argv and argv[0] == "info":  # same word as vnc.py info
        argv = argv[1:]
    ips = argv or [ip for ip, _ in SERVERS]
    unknown = [ip for ip in ips if ip not in names]
    if unknown:
        print("ERROR: not a known server: " + " ".join(unknown), file=sys.stderr)
        return 2

    local = local_ips()
    pool = ThreadPool(WORKERS)
    try:
        results = pool.map(lambda ip: collect(ip, local), ips)
    finally:
        pool.close()

    failed = 0
    for ip, (status, text) in zip(ips, results):
        print("===== %s %s =====" % (ip, label_of(ip, names[ip], hostname_of(text) if status == "ok" else "")))
        if status != "ok":
            failed += 1
            print("ERROR: " + text, "", sep="\n")
            continue
        total, available, rows = parse(text)
        if total and available is not None:
            used = total - available
            print("RAM: %s used of %s (%d%%), %s available" % (
                vnc.human_kb(used), vnc.human_kb(total), round(100.0 * used / total),
                vnc.human_kb(available)))
        else:
            print("RAM: unknown")
        if not rows:
            print("No VNC / xrdp sessions")
        else:
            fmt = "%-4s %-30s %-5s %-8s %-20s %6s"
            print(fmt % ("ID", "USER", "TYPE", "STATUS", "STARTED", "MEM"))
            for d, user, kind, state, started, mem_kb, duration in rows:
                print(fmt % (
                    ":%d" % d, user, kind, state, vnc.started_with_duration(started, duration),
                    vnc.human_kb(mem_kb) if mem_kb else "-"))
        print()

    print("%d of %d servers answered" % (len(ips) - failed, len(ips)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
