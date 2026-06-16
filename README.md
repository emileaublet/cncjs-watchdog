# CNCjs Watchdog

A macOS menu bar app that watches a CNCjs sender on `grbl.local` and auto-recovers
stalled jobs (Grbl sits Idle while CNCjs keeps "running"). It connects whenever the
Raspberry Pi is reachable, watches every job, recovers stalls by toggling the sender
pause/resume, and idles between jobs — so you never launch a script by hand.

## Status dot
- ⚪ grey/white — disconnected (Pi off) or connected & idle
- 🟢 green — a job is running, being watched
- 🟠 amber — recovering a stall

## Develop
    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
    ./venv/bin/python -m pytest          # run tests
    ./venv/bin/python run_app.py         # run in dev

## Build the app
    ./venv/bin/python setup.py py2app
    open "dist/CNCjs Watchdog.app"

## Config
Defaults live in `cncwatch/config.py`. Override any field via `~/.cncjs-watchdog.json`,
e.g. `{"host": "192.168.1.50", "stall_secs": 8}`.

Enable **Launch at login** from the menu to start it automatically.
