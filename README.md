This project is to control Linux environment of Inttegral.
It allows to contorl VNC and Veloce 

vnc.py
1) allows to report vnc status of the servers

Run `python3 vnc.py info` to show the local server hostname and its VNC / RDP
sessions. Use `info :N` or `info username` to filter the sessions.

Run `python3 vnc.py infoall` to report VNC / RDP sessions and RAM on all
configured servers. Keep `vncreport.py` beside `vnc.py`; the server list is
displayed in this order: Rocky, Comodel1-8, the remaining servers (`.210` and `.11`),
then Runtime1-4. The list is
defined in `vncreport.py` as `SERVERS`. Remote collection uses SSH as `refuser`
and requires SSH-key access and verified server host keys in `known_hosts`.
The local server is collected without SSH when its IP matches a configured server.
Unreachable servers are reported with an error; the command exits nonzero if any
server fails. The standalone `python3 vncreport.py info` command remains available.

STARTED shows the month.year with completed days in parentheses, for example
`10.2026 (19 days)`, `10.2026 (1 day)`, or `10.2026 (0 days)` for sessions under
24 hours. Stopped sessions show `-`. There is no separate DURATION column.
The multi-server report uses the same columns and date format;
the viewer list also uses the same date format.

The NOTE column uses short descriptions:
- `Remote desktop (RDP)`: an RDP session.
- `Not configured`: a running VNC session without a display entry in the settings.
- `Assigned to USER in settings`: the configured user differs from the running user.
- `No user assigned in settings`: the display entry has no configured user.
- `Service stopped`, `Service failed` or `Service unknown`: the session is running,
  but its service is not reported as running.

A blank note means no extra information is shown. Notes are informational only;
they do not change sessions. The Sources section below the table gives technical
details about where the values come from.

## Managing your own assigned session

Run these commands without sudo and without knowing your display number:

```bash
./vnc.py start
./vnc.py kill
./vnc.py restart
```

You can also use `python3 vnc.py` instead of `./vnc.py`. Run the self-service
commands as your normal Linux account, not through `sudo`; the tool uses the
account running it to find the assignment.

Verified example on `srv-rocky-1`, logged in as `refuser`:

```text
/proj/IntraOps$ ./vnc.py start
Started :2 for refuser (assignment kept)
Connect: srv-rocky-1.inttegral.local::5902
STATUS=running
```

Enter `srv-rocky-1.inttegral.local::5902` in your VNC viewer and authenticate
with your VNC password. The double colon specifies an explicit port. Your own
connection address is printed by `start` and `restart`.
Check your sessions with `./vnc.py info refuser` (replace `refuser` with your
Linux username).

The tool looks up the current Linux account in `/etc/tigervnc/vncserver.users`.
If several displays are assigned, it lists them with status and start time and
asks you to select a numbered entry. `kill` without arguments stops the service
but keeps the assignment. Stopping or restarting ends the desktop and can lose
unsaved work. RDP sessions and running sessions with a different process owner
are rejected by this workflow.

Existing administrator commands are unchanged: `sudo python3 vnc.py create USER`
allocates a display, and `sudo python3 vnc.py kill USER :N` stops, disables and
removes an assignment. The latter is not the self-service stop command.

### One-time setup on each server

An administrator assigns each user a fixed display and verifies the user has
run `vncpasswd`. Keep the mapping, service units and their executable files
protected against modification by delegated users.

For example, `/etc/tigervnc/vncserver.users` must contain this assignment
before granting `refuser` access to display `:2`:

```text
:2=refuser
```

Check that the display is not already running as another user. Do not delegate
displays with mismatched configured/running owners or XRDP displays.
Each user sets their own VNC connection password by running `vncpasswd` as
their normal account; this is separate from Linux/sudo authentication.

Use `sudo visudo -f /etc/sudoers.d/vnc-self-service` to grant each user only the
exact start, stop and restart commands for their assigned displays. For example,
for `refuser` assigned display `:2` on this server:

```sudoers
refuser ALL=(root) NOPASSWD: /usr/bin/systemctl start vncserver@\:2.service, /usr/bin/systemctl stop vncserver@\:2.service, /usr/bin/systemctl restart vncserver@\:2.service
```

Escape the colon in service arguments as `\:` inside sudoers only; ordinary
shell commands use `vncserver@:2.service` without the backslash.
Never force-save a sudoers file with syntax errors.
Verify `/usr/bin/systemctl` exists, keep the sudoers file owned by root with mode
`0440`, and validate the complete policy with `sudo visudo -c`. For VNC
administrators, add explicit rules for each display they may control (or a group
rule with the same exact commands). Do not grant arbitrary Python, systemctl,
wildcard arguments, or access to the editable script as root.

After saving the policy, run:

```bash
sudo chown root:root /etc/sudoers.d/vnc-self-service
sudo chmod 0440 /etc/sudoers.d/vnc-self-service
sudo visudo -c
```

Both `/etc/sudoers` and `/etc/sudoers.d/vnc-self-service` should report
`parsed OK`. Keep an administrator terminal open until validation succeeds.
Repeat the setup on each server; a policy installed on Rocky does not configure
Comodel or Runtime servers.

The script calls `/usr/bin/sudo -n /usr/bin/systemctl ...` internally; it never
prompts for a password. Sudoers, not the editable script, enforces permission.
When an assignment changes, revoke the old user's sudoers entries before
reassigning that display. The policy authorizes a fixed display, not its current
owner dynamically. Only administrator-managed assignments are supported.

Without changing sessions, a user can check permission using:

```bash
sudo -n -l /usr/bin/systemctl start vncserver@:2.service
sudo -n -l /usr/bin/systemctl stop vncserver@:2.service
sudo -n -l /usr/bin/systemctl restart vncserver@:2.service
```

The administrator should also verify an unassigned display is denied. Read-only
reporting needs no additional permissions.

### Troubleshooting

| Symptom | Meaning and action |
|---|---|
| Direct `systemctl start ...` asks for `org.freedesktop.systemd1.manage-units` authentication | This is Polkit, not sudoers. Cancel with Ctrl-C and use `./vnc.py start`; the script invokes restricted sudo internally. |
| `sudo: a password is required` | The passwordless rule is missing or does not match the current user, executable path, operation or display. Have an administrator check it with `visudo`. |
| Sudoers syntax errors near `vncserver@:N.service` | Use `vncserver@\:N.service` in sudoers. At the `What now?` prompt, choose `e` to edit or `x` to discard, never `Q` to force-save errors. |
| `no VNC session assigned` | An administrator must assign a fixed display and grant its exact sudoers permissions. |
| Different owner or RDP session reported | Ask an administrator to resolve the mismatch or use the appropriate RDP workflow; do not broaden sudo permissions. |
| Service fails to start | An administrator should inspect `systemctl status vncserver@:N.service` and `journalctl -u vncserver@:N.service`, substituting the assigned display. |

No separate script password or general administrator access is needed. The
passwordless permissions cover only the exact service commands listed in sudoers;
they do not authorize assigning displays, installing viewers, or managing other
system services.