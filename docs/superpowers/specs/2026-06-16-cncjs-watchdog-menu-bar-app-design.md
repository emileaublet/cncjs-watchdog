# CNCjs Watchdog Menu Bar App — Design

**Date:** 2026-06-16
**Status:** Approved (design)

## Problem

The CNCjs stall watchdog (`cncjs_watchdog.py`) currently has to be launched manually
from the terminal every time a drawing is run, and it exits after a single job. The
goal is a macOS menu bar app that runs persistently, detects when the Raspberry Pi
(`grbl.local`) running CNCjs is reachable, watches every job automatically, recovers
stalls, and returns to idle between jobs — so the script never has to be invoked by
hand again.

## Workflow context

The user's normal flow: power on the Pi, wait for `grbl.local` to come up, open the
CNCjs **web UI**, drop in the drawing, and start it. **The web UI owns the serial
port and starts jobs.** The watchdog is therefore a passive observer — it never opens
the serial port and never starts jobs.

## Overall shape

A macOS menu bar app (Python + [rumps](https://github.com/jaredks/rumps)) wrapping the
existing watchdog logic as a long-running engine. Launched once (and at login), it
maintains a connection to CNCjs, watches every job, recovers stalls, and idles between
jobs indefinitely.

Two layers:

- **`engine.py`** — the watchdog logic from `cncjs_watchdog.py`, refactored into a
  reusable, persistent engine with no UI knowledge. Exposes current state and fires
  callbacks on transitions.
- **`app.py`** — the rumps menu bar app. Owns the icon, menu, notifications, and log
  view. Knows nothing about Socket.IO; subscribes to engine callbacks.

This isolates the proven Socket.IO/Engine.IO protocol code so it can be tested headless,
with the UI as a thin shell.

## Behavior changes to the existing logic

Three changes turn the one-shot script into a persistent observer:

1. **Never self-terminate.** Today `_shutdown` closes everything when a job completes
   or stops. Instead, on job complete/stop the engine fires a `job_finished` callback
   (carrying reason + sent/total) and **resets to idle**, staying connected and ready
   for the next drawing. `_shutdown` is reserved for actual app quit.
2. **Passive observer.** Remove the `open` emit on the `startup` event — the web UI
   owns the serial port. The engine only listens to `controller:state`,
   `workflow:state`, and `sender:status`.
3. **Always-on stall loop.** Start the stall loop once on connect rather than gating
   it on the `serialport:open` event (which a persistent app would miss if it connects
   after the port is already open). Stall *detection* still gates purely on
   `workflow:state == running`, exactly as the current code does.

## State machine

The engine is always in exactly one state, which drives the icon and notifications:

| State | When | Dot color | Notification |
|---|---|---|---|
| **Disconnected** | Pi off/asleep/unreachable; reconnect loop retrying every 5s | grey | "Pi lost" (only on transition from connected) |
| **Idle** | Connected to CNCjs, no job running | white | "Pi connected" on first connect |
| **Running** | `workflow:state == running`, watching for stalls | green | — |
| **Recovering** | Stall detected, mid pause→resume cycle | amber | "Stall recovered (#N)" after motion resumes |
| **Done** (brief) | Job completed/stopped, about to return to Idle | white | "Job complete (sent/total)" or "Job stopped" |

"Pi connected/lost" is derived from the existing reconnect loop — a successful socket
connect is itself the reachability signal, so no separate ping/reachability logic is
needed.

## Menu bar UI (rumps)

Icon: a **colored dot** reflecting the current state (per the table above).

Dropdown menu, top to bottom:

- **Status line** (non-clickable) — e.g. `● Running — 1,240 / 5,000 lines`
- **Stalls recovered: N** — count for the current job; resets when a new job starts
- `─────────`
- **Open log** — opens `cncjs_watchdog.log` in the default app
- **Launch at login** ✓ — toggle that registers/unregisters the login item
- `─────────`
- **Quit**

## Config

The hardcoded constants (`HOST`, `PORT`, `SERIAL_PORT`, `BAUD`, `SECRET`,
`STALL_SECS`, `HOLD_SECS`, `DONE_LINES`, `HEARTBEAT_SECS`) move into a small
`config.py` (or a `~/.cncjs-watchdog.json` read at startup). No preferences UI in v1
(YAGNI) — edit the file directly. `SECRET` stays local to the machine.

## Notifications

Native macOS banners via `rumps.notification`: Pi connected, Pi lost, stall recovered
(#N), job complete, job stopped. Throttled so reconnect flurries can't spam — e.g. do
not repeat "Pi lost" while already disconnected; coalesce rapid reconnects.

## Packaging & launch-at-login

- Bundled into `CNCjs Watchdog.app` via **py2app**, with its own venv — a real
  double-clickable app, no terminal, no manual `python` invocation.
- **Launch at login** via a macOS login item, toggled from the menu. Preferred:
  `SMAppService` login-item registration; simpler fallback: a `LaunchAgent` plist in
  `~/Library/LaunchAgents`.

## Error handling & testing

- The reconnect loop already handles the Pi disappearing. Make its logging quiet when
  disconnection is expected (Pi simply off) vs. noisy on genuine errors.
- **Engine is testable headless.** With no UI dependencies, drive it with synthetic
  Socket.IO frames (job start → stall → recover → complete) and assert state
  transitions and callbacks — no hardware required.
- The menu bar layer stays thin enough to verify manually.

## Out of scope (v1)

- Preferences/settings UI (config is file-based).
- Multiple machines / multiple serial ports.
- Pausing or controlling jobs from the menu bar (observer only).
- Reimplementing the protocol in Swift (Python reuse chosen deliberately).

## File layout

```
cncjs_watchdog/
  engine.py      # persistent watchdog engine (refactored from cncjs_watchdog.py)
  app.py         # rumps menu bar app (entry point)
  config.py      # configuration constants / loader
  tests/
    test_engine.py
  setup.py       # py2app build config
```

The original `cncjs_watchdog.py` remains as a reference / standalone CLI fallback.
