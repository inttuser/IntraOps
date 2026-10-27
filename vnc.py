#!/usr/libexec/platform-python -I
"""vnc.py - TigerVNC servers (systemd + vncserver.users) and VNC viewers (TigerVNC / RealVNC)
Target: Rocky Linux 8, tigervnc-server 1.15

VNC servers:
  sudo ./vnc.py create  <user> [:N]   assign a display (next free one if :N is omitted) and start VNC
  sudo ./vnc.py kill    <user> :N     stop VNC and free the display
  sudo ./vnc.py restart <user> :N     restart a frozen VNC
       ./vnc.py info    [:N|user]     server hostname, VNC / RDP sessions, memory and simple notes
       ./vnc.py infoall               VNC / RDP sessions and RAM on all configured servers
       ./vnc.py mem                   server RAM: total, used, available
VNC viewers (clients):
  sudo ./vnc.py viewer setup   <tigervnc|realvnc> [file.rpm] [--replace]   install a viewer and select it
  sudo ./vnc.py viewer mode    [tigervnc|realvnc]   show / select the viewer mode
       ./vnc.py viewer clients                      installed viewers, versions, which one is selected
       ./vnc.py viewer list    [user]               running viewers: owner, target, duration, CPU, memory
       ./vnc.py viewer kill    <user> <pid|all>     stop one viewer (or all viewers) of a user
       ./vnc.py viewer connect <host::port|host:N>  open the selected viewer
"""

from __future__ import print_function

import getpass
import io
import os
import re
import signal
import socket
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: only the dry-run tests run there
    fcntl = None

ENV = os.environ
PROG = sys.argv[0]
DRY_RUN = ENV.get("VNCTOOL_DRY_RUN") == "1"
USERS_FILE = ENV.get("VNCTOOL_USERS_FILE") or "/etc/tigervnc/vncserver.users"
LOG_FILE = ENV.get("VNCTOOL_LOG") or "/var/log/vnctool.log"
MEMINFO = ENV.get("VNCTOOL_MEMINFO_FILE") or "/proc/meminfo"
VIEWER_CONF = ENV.get("VNCTOOL_VIEWER_CONF") or "/etc/vnctool/viewer.conf"
BASE_PORT = 5900
KILL_WAIT = 5   # seconds between SIGTERM and SIGKILL of a viewer
LOCK_WAIT = 10  # seconds to wait for the vncserver.users lock

VIEWERS = {"tigervnc": ("tigervnc", "TigerVNC"), "realvnc": ("realvnc-vnc-viewer", "RealVNC")}  # mode -> package, name
STATES = {"active": "running", "activating": "running", "reloading": "running",
          "inactive": "stopped", "deactivating": "stopped", "failed": "failed"}
NO_STATUS = ("info-all", "infoall", "mem", "viewer-clients", "viewer-list", "viewer-connect")  # tables / started programs
INFO_FMT = "%-4s %-30s %-8s %-20s %6s"  # ID USER STATUS STARTED MEM
PS_XVNC = "ps -e -o pid -o ppid -o sid -o user:64 -o etimes -o rss -o args"  # how info finds the running sessions and their memory
XVNC_RE = re.compile(r"\S*Xvnc\s+:([0-9]+)(\s|$)")
# one -o per column: procps-ng 3.3.10 (CentOS 7) reads "-eo pid=,ppid=,..." as the pid column only
PS_COMMAND = ("ps", "-e", "-o", "pid=", "-o", "ppid=", "-o", "sid=", "-o", "user:64=", "-o", "etimes=", "-o", "rss=",
              "-o", "args=")

action = ""
_lock = None  # stays open until the process exits, so the lock is held

# ------------------------------------------------------------------ helpers


def out(*lines):
    for line in lines:
        print(line)
        sys.stdout.flush()


def err(text):
    print(text, file=sys.stderr)
    sys.stderr.flush()


def log(msg):
    try:
        with io.open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(u"%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), ENV.get("SUDO_USER") or getpass.getuser(), msg))
    except (IOError, OSError):  # separate classes on Python 2
        pass


def die(code, msg):
    err("ERROR: " + msg)
    log("ERROR(%d) %s: %s" % (code, action, msg))
    if action not in NO_STATUS:
        out("STATUS=error")
    sys.exit(code)


def need(args, low, high, syntax):
    if not low <= len(args) <= high:
        die(2, "usage: %s %s" % (PROG, syntax))
    return args


def capture(*cmd):
    """Read-only command -> (exit code, stdout); a missing program counts as failed."""
    try:
        with open(os.devnull, "w") as devnull:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=devnull, universal_newlines=True)
            stdout = p.communicate()[0]
        return p.returncode, stdout
    except OSError:
        return 127, ""


def run(*cmd):
    """Command that changes the system (only printed in dry-run) -> True if it worked."""
    if DRY_RUN:
        err("[dry-run] " + " ".join(cmd))
        return True
    try:
        return subprocess.call(cmd) == 0
    except OSError:
        return False


def read_lines(path):
    try:
        with io.open(path, encoding="utf-8", newline="") as f:
            return f.read().split("\n")
    except (IOError, OSError):
        return []


def write(path, text, mode="w"):
    with open(path, mode, encoding="utf-8", newline="") as f:
        f.write(text)


def fullmatch(pattern, text):
    """re.fullmatch (missing in Python 2)"""
    return re.match("(?:%s)\\Z" % pattern, text)


def validate_user(user):
    # local names (haykpet) or AD names with domain (ani.mashinyan@inttegral.local); no spaces, '=', '/', ';'
    if not fullmatch(r"[a-z_][a-z0-9._-]{0,63}(@[a-z0-9][a-z0-9.-]{0,252})?", user):
        die(2, "invalid username: '%s'" % user)


def parse_display(arg):
    """":5", "5" or "05" -> 5 (1..99)"""
    d = arg[1:] if arg.startswith(":") else arg
    if not fullmatch(r"0?[1-9]|[1-9][0-9]", d):
        die(2, "invalid display: '%s' (use :1 .. :99)" % arg)
    return int(d)


def is_root():
    return not ENV.get("VNCTOOL_FAKE_USER") if DRY_RUN else os.geteuid() == 0


def caller():
    if DRY_RUN:
        return ENV.get("VNCTOOL_FAKE_USER") or "root"
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def require_root():
    if not is_root():
        die(2, "run it with sudo")


def require_user_ready(user):
    """The Linux user exists and has set a VNC password."""
    if ENV.get("VNCTOOL_SKIP_USER_CHECK") == "1":
        return
    import pwd
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        die(5, "Linux user '%s' not found" % user)
    files = [os.path.join(home, p) for p in (".config/tigervnc/passwd", ".vnc/passwd")]
    if not any(os.path.isfile(f) and os.path.getsize(f) for f in files):
        die(5, "user '%s' has no VNC password yet: ask them to run 'vncpasswd' once" % user)

# ------------------------------------------------------------------ vncserver.users + systemd


def entries():
    """{display: user} from lines like ":3=haykpet", in file order (the first line of a display wins)."""
    result = {}
    for line in read_lines(USERS_FILE):
        m = fullmatch(r"\s*:([0-9]{1,2})=(\S*)\s*", line)
        if m:
            result.setdefault(int(m.group(1)), m.group(2))
    return result


def require_owner(user, display):
    owner = entries().get(display)
    if not owner:
        die(3, "display :%d is not assigned to anyone" % display)
    if owner != user:
        die(3, "display :%d belongs to '%s', not '%s'" % (display, owner, user))


def remove_line(display):
    """The previous version is kept as vncserver.users.bak"""
    try:
        with open(USERS_FILE, encoding="utf-8", newline="") as f:
            text = f.read()
        write(USERS_FILE + ".bak", text)
        write(USERS_FILE, "\n".join(l for l in text.split("\n") if not re.match(r"\s*:%d=" % display, l)))
    except OSError:
        die(4, "cannot edit " + USERS_FILE)


def lock():
    """Exclusive lock, so two "create" calls never take the same display."""
    global _lock
    if fcntl is None:
        if DRY_RUN:
            return
        die(4, "file locking is not available on this system")
    try:
        _lock = open(USERS_FILE + ".lock", "w")
    except OSError:
        die(4, "cannot open lock file %s.lock" % USERS_FILE)
    deadline = time.time() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return  # we have the lock
        except OSError:  # someone else has it: wait a little and try again
            if time.time() >= deadline:
                die(4, "vncserver.users is locked by another operation, try again")
            time.sleep(0.2)


def unit(display):
    return "vncserver@:%d.service" % display


def states(displays):
    """{display: running|stopped|failed|unknown} - one "systemctl is-active" call for all of them."""
    if not displays:
        return {}
    if DRY_RUN:
        answers = [ENV.get("VNCTOOL_FAKE_STATE") or "active"] * len(displays)
    else:
        units = [unit(d) for d in displays]
        answers = capture("systemctl", "is-active", *units)[1].split()  # one answer line per unit

    result = {}
    for i, display in enumerate(displays):
        answer = answers[i] if i < len(answers) else ""
        result[display] = STATES.get(answer, "unknown")
    return result


def state_of(display):
    return states([display])[display]

# ------------------------------------------------------------------ server actions


def do_create(args):  # <user> [:N]
    need(args, 1, 2, "create <user> [:N]")
    user = args[0]
    validate_user(user)
    display = parse_display(args[1]) if len(args) == 2 else None
    require_root()
    require_user_ready(user)

    lock()
    taken = entries()
    if display is None:
        display = first_free_display(taken)
    elif taken.get(display):
        die(3, "display :%d is already assigned to '%s'" % (display, taken[display]))

    try:
        with open(USERS_FILE, "a+", encoding="utf-8", newline="") as f:
            f.seek(0)
            text = f.read()
            if text and not text.endswith("\n"):
                f.write("\n")  # keep the file well-formed if its last line has no newline
            f.write(":%d=%s\n" % (display, user))
    except OSError:
        die(4, "cannot write " + USERS_FILE)

    if not run("systemctl", "enable", "--now", unit(display)):
        run("systemctl", "disable", "--now", unit(display))  # roll back so the display is free again
        remove_line(display)
        die(4, "could not start %s; see: journalctl -u %s" % (unit(display), unit(display)))

    state = state_of(display)
    port = BASE_PORT + display
    log("create :%d for %s state=%s" % (display, user, state))
    out("Created :%d for %s, port %d" % (display, user, port),
        "Connect: %s::%d" % (server_ip(), port),
        "STATUS=" + state)


def server_ip():
    """First address of "hostname -I", or "" """
    code, text = capture("hostname", "-I")
    ips = text.split()
    if code == 0 and ips:
        return ips[0]
    return ""


def first_free_display(taken):
    for display in range(1, 100):
        if not taken.get(display):
            return display
    die(3, "no free display numbers left")


def owned_display(args, name):
    """<user> :N of kill / restart -> (user, display)"""
    user, arg = need(args, 2, 2, name + " <user> :N")
    validate_user(user)
    display = parse_display(arg)
    require_root()
    return user, display


def do_kill(args):  # <user> :N
    user, display = owned_display(args, "kill")
    lock()
    require_owner(user, display)
    # stop first; if that fails the line stays, so nothing is half-deleted
    if not run("systemctl", "disable", "--now", unit(display)):
        die(4, "could not stop " + unit(display))
    remove_line(display)
    log("kill :%d of %s" % (display, user))
    out("Killed :%d of %s, display is free now" % (display, user), "STATUS=stopped")


def do_restart(args):  # <user> :N
    user, display = owned_display(args, "restart")
    require_owner(user, display)
    if not run("systemctl", "restart", unit(display)):
        die(4, "restart of %s failed; see: journalctl -u %s" % (unit(display), unit(display)))
    state = state_of(display)
    log("restart :%d of %s state=%s" % (display, user, state))
    out("Restarted :%d of %s" % (display, user), "STATUS=" + state)


def started_at(seconds):
    """Process age in seconds -> start month, "10.2026"."""
    return time.strftime("%m.%Y", time.localtime(time.time() - seconds))


def duration_days(seconds):
    days = seconds // 86400
    return "%d %s" % (days, "day" if days == 1 else "days")


def started_with_duration(started, duration):
    return "%s (%s)" % (started, duration) if started != "-" else "-"


def human_kb(kb):
    """kilobytes -> "350M" / "1.2G" """
    mb = kb / 1024.0
    return "%dM" % round(mb) if mb < 1024 else "%.1fG" % (mb / 1024)


def processes():
    """All processes -> {pid: (ppid, user, etimes, rss_kb, args, sid, scope)}. ps and /proc need no root."""
    if DRY_RUN:
        lines = read_lines(ENV.get("VNCTOOL_XVNC_FILE") or "")  # lines "pid ppid sid user etimes rss_kb args..."
        scope_lines = read_lines(ENV.get("VNCTOOL_CGROUP_FILE") or "")
    else:
        lines = capture(*PS_COMMAND)[1].splitlines()
        scope_lines = local_scope_lines()
    return parse_processes(lines, parse_scopes(scope_lines))


def local_scope_lines():
    """Like "grep -H session- /proc/[0-9]*/cgroup": lines "/proc/9141/cgroup:1:name=systemd:/user.slice/..."."""
    result = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            result.extend("/proc/%s/cgroup:%s" % (name, l) for l in read_lines("/proc/%s/cgroup" % name) if "session-" in l)
    return result


def parse_scopes(lines):
    """grep -H lines -> {pid: "/user.slice/user-1000.slice/session-2.scope"}"""
    scopes = {}
    for line in lines:
        m = re.match(r"/proc/([0-9]+)/cgroup:[^:]*:[^:]*:(/\S*?session-[^/\s]+\.scope)(/|$)", line)
        if m:
            scopes.setdefault(int(m.group(1)), m.group(2))
    return scopes


def parse_processes(lines, scopes=None):
    scopes = scopes or {}
    procs = {}
    for line in lines:
        fields = line.split(None, 6)
        if len(fields) < 7 or line.startswith("#"):
            continue
        pid, ppid, sid, user, etimes, rss, args = fields
        procs[int(pid)] = (int(ppid), user, int(etimes), int(rss), args, int(sid), scopes.get(int(pid), ""))
    return procs


def xrdp_sesexec(pid, procs):
    """The xrdp-sesexec ancestor of this Xvnc, or None for a plain VNC session."""
    parent = procs[pid][0]
    while parent > 1 and parent in procs:
        if re.search(r"(^|/)xrdp-sesexec(\s|$)", procs[parent][4]):
            return parent
        parent = procs[parent][0]
    return None


def session_pids(pid, procs):
    """All processes of the desktop of this Xvnc. xrdp: the tree of its own xrdp-sesexec (the SID is
    shared by all xrdp sessions). VNC: every process of its login session scope (KDE daemons such
    as plasma-desktop leave the process tree and the SID, but stay in the scope); if the scope is
    unknown or holds another VNC server, the processes of the same SID plus the tree of the top
    vncsession (systemd VNC)."""
    sesexec = xrdp_sesexec(pid, procs)
    if sesexec:
        return tree_pids(sesexec, procs)
    scope = procs[pid][6]
    if scope:
        servers = [p for p, v in procs.items() if v[6] == scope and XVNC_RE.match(v[4]) and xrdp_sesexec(p, procs) is None]
        if len(servers) == 1:  # with several the scope cannot be split between them
            return [p for p, v in procs.items() if v[6] == scope]
    root, parent = pid, procs[pid][0]
    while parent > 1 and parent in procs:
        args = procs[parent][4]
        if re.search(r"(^|/)vncsession(\s|$)", args):
            root = parent
        parent = procs[parent][0]
    found = set(tree_pids(root, procs))
    sid = procs[pid][5]
    if sid > 1:
        found.update(p for p, v in procs.items() if v[5] == sid)
    return found


def tree_pids(root, procs):
    """A process and all its children."""
    children = {}
    for pid, p in procs.items():
        children.setdefault(p[0], []).append(pid)
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo.extend(children.get(pid, []))
    return found


def session_rss(pid, procs):
    """RSS of the whole desktop in kB. Shared libraries are counted in every process, so this is
    a little more than the session really takes - an upper estimate."""
    return sum(procs[p][3] for p in session_pids(pid, procs))


def xvnc_sessions(procs=None):
    """Running Xvnc -> {display: (user, started, mem_kb, is_xrdp, duration)}."""
    if procs is None:
        procs = processes()
    sessions = {}
    for pid, (_ppid, user, etimes, _rss, args, _sid, _scope) in procs.items():
        m = XVNC_RE.match(args)  # "/usr/bin/Xvnc :9 -auth ..."
        if not m:
            continue
        mem_kb = session_rss(pid, procs)
        sessions[int(m.group(1))] = (user, started_at(etimes), mem_kb,
                                   xrdp_sesexec(pid, procs) is not None, duration_days(etimes))
    return sessions


def do_info(args):  # [:N | user]   (read-only)
    global action
    need(args, 0, 1, "info [:N|user]")
    display, only_user = None, None
    if args and fullmatch(r":?[0-9]+", args[0]):
        display = parse_display(args[0])
    else:
        action = "info-all"  # a table without a STATUS= line
        if args:
            only_user = args[0]
            validate_user(only_user)
    have_file = os.path.exists(USERS_FILE)  # without it only the running sessions (ps) are shown
    if have_file and not os.access(USERS_FILE, os.R_OK):
        die(4, "cannot read " + USERS_FILE)

    owners = entries() if have_file else {}  # what the file says
    running = xvnc_sessions()   # what really runs
    # systemd: why a display does not run (stopped / failed), and whether a running VNC has an active unit
    state = states([d for d in owners if d not in running or not running[d][3]])

    rows = []
    for d in sorted(set(owners) | set(running)):
        if d in running:
            user, started, mem_kb, is_xrdp, duration = running[d]
            notes = []
            if is_xrdp:
                notes.append("Remote desktop (RDP)")
            elif have_file and d not in owners:
                notes.append("Not configured")
            if d in owners and owners[d] != user:
                notes.append("Assigned to %s in settings" % owners[d] if owners[d] else "No user assigned in settings")
            if not is_xrdp and state.get(d, "running") != "running":
                notes.append("Service " + state[d])
            rows.append((d, user, "running", started, human_kb(mem_kb), "ps", "; ".join(notes), duration))
        else:
            rows.append((d, owners[d] or "-", state[d], "-", "-", "file", "", "-"))

    if display is not None:
        rows = [r for r in rows if r[0] == display]
        if not rows:
            die(3, "display :%d is not assigned to anyone and not running" % display)
    if only_user:
        rows = [r for r in rows if r[1] == only_user]

    out("Server: " + socket.gethostname(), "",
        INFO_FMT % ("ID", "USER", "STATUS", "STARTED", "MEM") + "  NOTE")
    for row in rows:
        started = started_with_duration(row[3], row[7])
        out((INFO_FMT % ((":%d" % row[0],) + row[1:3] + (started, row[4])) + "  " + row[6]).rstrip())

    # where the values come from: once under the table, not on every row
    sources = {
        "ps": "Xvnc is running:  %s  (USER = process owner, MEM = whole desktop)" % PS_XVNC,
        "file": "no Xvnc process:  USER from %s, STATUS from systemctl is-active vncserver@:N.service"
                % USERS_FILE,
    }
    used = [k for k in ("ps", "file") if any(row[5] == k for row in rows)]
    if used:
        out("", "Sources:")
        for k in used:
            out("  " + sources[k])
    if display is not None:
        out("STATUS=" + rows[0][2])


def do_infoall(args):
    need(args, 0, 0, "infoall")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import vncreport
    sys.exit(vncreport.main([]))


def do_mem(args):  # (read-only)
    need(args, 0, 0, "mem")
    kb = {}
    for line in read_lines(MEMINFO):  # "MemTotal:       65588412 kB"
        m = re.match(r"(MemTotal|MemAvailable):\s+([0-9]+)", line)
        if m:
            kb[m.group(1)] = int(m.group(2))
    if "MemTotal" not in kb or "MemAvailable" not in kb:
        die(4, "cannot read MemTotal / MemAvailable from " + MEMINFO)
    total, available = kb["MemTotal"], kb["MemAvailable"]
    used = total - available  # what "free" shows as used, without cache that can be freed
    out("Memory: %s used of %s (%d%%), %s available" % (human_kb(used), human_kb(total),
                                                        round(100.0 * used / total), human_kb(available)),
        "MEM_TOTAL_KB=%d" % total, "MEM_USED_KB=%d" % used, "MEM_AVAILABLE_KB=%d" % available)

# ------------------------------------------------------------------ viewers: helpers


def validate_mode(mode):
    if mode not in VIEWERS:
        die(2, "invalid mode: '%s' (use tigervnc or realvnc)" % mode)


def pkg_version(mode):
    """Installed version of the mode's package, or "" """
    pkg = VIEWERS[mode][0]
    if DRY_RUN:
        # VNCTOOL_FAKE_PKGS="tigervnc=1.15.0-7.el8_10 other=1.0"
        for item in (ENV.get("VNCTOOL_FAKE_PKGS") or "").split():
            name, _, version = item.partition("=")
            if name == pkg:
                return version
        return ""
    # rpm prints "package X is not installed" on stdout, so trust only its exit code
    code, ver = capture("rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg)
    return ver.strip() if code == 0 else ""


def viewer_bin(mode):
    """Path of the mode's vncviewer binary, or "" """
    if DRY_RUN:
        return "/usr/bin/vncviewer"
    files = capture("rpm", "-ql", VIEWERS[mode][0])[1].splitlines()
    for f in files:
        if f.endswith("/bin/vncviewer"):
            return f
    return ""


def current_mode():
    """From the config file - parsed, never executed."""
    mode = ""
    for line in read_lines(VIEWER_CONF):
        m = re.fullmatch(r"VIEWER_MODE=(tigervnc|realvnc)\s*", line)
        if m:
            mode = m.group(1)  # the last VIEWER_MODE line wins
    return mode


def save_mode(mode):
    try:
        if not DRY_RUN:
            os.makedirs(os.path.dirname(VIEWER_CONF), exist_ok=True)
        write(VIEWER_CONF, "# written by vnc.py\nVIEWER_MODE=%s\n" % mode)
    except OSError:
        die(4, "cannot write " + VIEWER_CONF)


def human_duration(s):
    """seconds -> "2d 03:14:05" """
    d, s = divmod(s, 86400)
    hms = "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)
    return "%dd %s" % (d, hms) if d else hms


def viewers():
    """One tuple per running viewer: (pid, user, etimes, pcpu, pmem, rss_kb, type, args)"""
    result = []
    if DRY_RUN:
        for line in read_lines(ENV.get("VNCTOOL_PS_FILE") or ""):
            fields = line.split(None, 7)
            if len(fields) == 8 and not line.startswith("#"):
                result.append(tuple(fields))
        return result

    types = {}  # binary path -> TigerVNC / RealVNC / other (rpm is asked once per binary)
    ps = capture("ps", "-eo", "pid=,user:64=,etimes=,pcpu=,pmem=,rss=,comm=,args=")[1]
    for line in ps.splitlines():
        fields = line.split(None, 7)
        if len(fields) < 8 or "vncviewer" not in fields[6]:
            continue
        pid, user, etimes, cpu, mem, rss, _comm, args = fields
        try:
            exe = os.readlink("/proc/%s/exe" % pid)
        except OSError:
            exe = ""
        if exe not in types:
            types[exe] = type_of_exe(exe)
        result.append((pid, user, etimes, cpu, mem, rss, types[exe], args))
    return result


def type_of_exe(path):
    """Which package the binary comes from -> TigerVNC / RealVNC / other"""
    package = capture("rpm", "-qf", "--qf", "%{NAME}", path)[1]
    if package.startswith("tigervnc"):
        return "TigerVNC"
    if package.startswith("realvnc"):
        return "RealVNC"
    return "other"


def target_of(args):
    """Viewer command line -> "host::port" / "host:N" (first argument with ':' after the binary)"""
    last = "-"
    for word in args.split()[1:]:
        if word.startswith("-") or "/" in word:  # options and file paths
            continue
        if ":" in word:
            return word
        last = word
    return last


def send(pids, sig):
    """kill(1) for several pids -> the pids that could not be signalled"""
    failed = []
    for p in pids:
        try:
            os.kill(int(p), sig)
        except OSError:
            failed.append(p)
    return failed

# ------------------------------------------------------------------ viewer actions


def viewer_setup(args):  # <mode> [file.rpm] [--replace]
    mode, rpm_file, replace = "", "", False
    for a in args:
        if a == "--replace":
            replace = True
        elif a.endswith(".rpm"):
            rpm_file = a
        elif not mode:
            mode = a
        else:
            mode = ""  # two modes given
            break
    if not mode:
        die(2, "usage: %s viewer setup <tigervnc|realvnc> [file.rpm] [--replace]" % PROG)
    validate_mode(mode)
    require_root()

    other = "realvnc" if mode == "tigervnc" else "tigervnc"
    pkg, name = VIEWERS[mode]
    other_pkg, other_name = VIEWERS[other]
    if pkg_version(other):
        if not replace:
            die(3, "%s viewer (%s) is installed and also owns /usr/bin/vncviewer; add --replace to remove it"
                % (other_name, other_pkg))
        if not run("dnf", "-y", "remove", other_pkg):
            die(4, "dnf remove %s failed" % other_pkg)
        log("viewer setup: removed " + other_pkg)

    version = pkg_version(mode)
    if version:
        out("%s viewer already installed: %s %s" % (name, pkg, version))
    elif mode == "tigervnc":
        if not run("dnf", "-y", "install", "tigervnc"):
            die(4, "dnf install tigervnc failed")
    else:
        if not rpm_file:
            die(2, "RealVNC is not in the Rocky repos: download VNC-Viewer-*-Linux-x64.rpm and run: "
                   "%s viewer setup realvnc <file.rpm>" % PROG)
        if not DRY_RUN and not os.access(rpm_file, os.R_OK):
            die(2, "cannot read " + rpm_file)
        if not run("dnf", "-y", "install", rpm_file):
            die(4, "dnf install %s failed" % rpm_file)

    save_mode(mode)
    log("viewer setup " + mode)
    out("Viewer mode: %s (%s)" % (mode, name), "STATUS=ok")


def viewer_mode(args):  # [mode]
    need(args, 0, 1, "viewer mode [tigervnc|realvnc]")
    if not args:
        out("Viewer mode: " + (current_mode() or "not set"), "STATUS=ok")
        return
    mode = args[0]
    validate_mode(mode)
    require_root()
    if not pkg_version(mode):
        die(3, "%s viewer is not installed; run: %s viewer setup %s" % (VIEWERS[mode][1], PROG, mode))
    save_mode(mode)
    log("viewer mode " + mode)
    out("Viewer mode: %s (%s)" % (mode, VIEWERS[mode][1]), "STATUS=ok")


def viewer_clients(args):  # (read-only)  "tigervnc TigerVNC 1.15.0-7.el8_10 /usr/bin/vncviewer *"
    fmt = "%-9s %-9s %-22s %-22s %s"
    selected = current_mode()
    out(fmt % ("MODE", "NAME", "VERSION", "BINARY", "SELECTED"))
    for mode in ("tigervnc", "realvnc"):
        version = pkg_version(mode)
        binary = (viewer_bin(mode) or "-") if version else "-"
        mark = "*" if mode == selected else "-"
        out(fmt % (mode, VIEWERS[mode][1], version or "not-installed", binary, mark))


def viewer_list(args):  # [user]   (read-only)
    need(args, 0, 1, "viewer list [user]")
    if args:
        validate_user(args[0])
    fmt = "%-7s %-20s %-8s %-24s %-19s %-12s %5s %5s %8s"
    out(fmt % ("PID", "USER", "TYPE", "TARGET", "STARTED", "DURATION", "CPU%", "MEM%", "RSS_MB"))
    for pid, user, etimes, cpu, mem, rss, vtype, vargs in viewers():
        if args and user != args[0]:
            continue
        seconds = int(etimes)  # how long the viewer has been running
        started = started_at(seconds)
        out(fmt % (pid, user, vtype, target_of(vargs), started, human_duration(seconds),
                   cpu, mem, int(rss) // 1024))


def viewer_kill(args):  # <user> <pid|all>
    user, which = need(args, 2, 2, "viewer kill <user> <pid|all>")
    validate_user(user)
    if which != "all" and not re.fullmatch(r"[0-9]{1,9}", which):
        die(2, "invalid pid: '%s'" % which)
    if not is_root() and caller() != user:
        die(3, "you can only kill your own viewers (you are '%s')" % caller())

    pids = []
    for viewer in viewers():
        pid, owner = viewer[0], viewer[1]
        if which == "all" and owner == user:
            pids.append(pid)
        elif pid == which:
            if owner != user:
                die(3, "viewer %s belongs to '%s', not '%s'" % (pid, owner, user))
            pids.append(pid)

    if not pids:
        if which != "all":
            die(3, "pid %s is not a running VNC viewer" % which)
        out("No viewers of %s are running" % user, "STATUS=ok")
        return

    if DRY_RUN:
        err("[dry-run] kill -TERM " + " ".join(pids))
    else:
        if send(pids, signal.SIGTERM):
            die(4, "kill -TERM %s failed" % " ".join(pids))
        # give the viewers time to close, then force the ones that hang
        left = pids
        for _ in range(KILL_WAIT):
            left = [p for p in left if not send([p], 0)]
            if not left:
                break
            time.sleep(1)
        if left and send(left, signal.SIGKILL):
            die(4, "kill -KILL %s failed" % " ".join(left))

    log("viewer kill of %s: %s" % (user, " ".join(pids)))
    out("Killed %d viewer(s) of %s: %s" % (len(pids), user, " ".join(pids)), "STATUS=ok")


def viewer_connect(args):  # <host::port|host:N>
    need(args, 1, 1, "viewer connect <host::port|host:N>")
    target = args[0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}(::[0-9]{1,5}|:[0-9]{1,2})", target):
        die(2, "invalid target: '%s' (use host::5903 or host:3)" % target)
    mode = current_mode()
    if not mode:
        die(3, "no viewer mode selected; run: sudo %s viewer setup <tigervnc|realvnc>" % PROG)
    pkg, name = VIEWERS[mode]
    if not pkg_version(mode):
        die(3, "%s viewer is not installed; run: sudo %s viewer setup %s" % (name, PROG, mode))
    binary = viewer_bin(mode)
    if not binary:
        die(4, "cannot find the vncviewer binary of " + pkg)
    if not DRY_RUN and not ENV.get("DISPLAY"):
        die(4, "no $DISPLAY: run it inside a graphical (VNC/X11) session")

    log("viewer connect %s with %s" % (target, mode))
    if DRY_RUN:
        err("[dry-run] %s %s" % (binary, target))
        return
    try:
        # like nohup ... &: detached from this terminal, keeps running after we exit
        p = subprocess.Popen([binary, target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        die(4, "cannot start %s: %s" % (binary, e))
    out("Started %s viewer for %s, pid %d" % (name, target, p.pid))

# ------------------------------------------------------------------ main


ACTIONS = {"create": do_create, "kill": do_kill, "delete": do_kill, "restart": do_restart, "info": do_info,
           "infoall": do_infoall, "mem": do_mem}
VIEWER_ACTIONS = {"setup": viewer_setup, "mode": viewer_mode, "clients": viewer_clients,
                  "list": viewer_list, "kill": viewer_kill, "connect": viewer_connect}


def usage(code):
    out(__doc__.strip())
    sys.exit(code)


def main(argv):
    global action
    action = argv[0] if argv else ""
    args = argv[1:]
    if action in ("-h", "--help", "help"):
        usage(0)

    if action == "viewer":            # vnc.py viewer <sub> ...
        sub = args[0] if args else ""
        args = args[1:]
        action = "viewer-" + sub
        func = VIEWER_ACTIONS.get(sub)
    else:                             # vnc.py <action> ...
        if action == "delete":
            action = "kill"
        func = ACTIONS.get(action)

    if func is None:
        action = "help"
        usage(2)
    func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
