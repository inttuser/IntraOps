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