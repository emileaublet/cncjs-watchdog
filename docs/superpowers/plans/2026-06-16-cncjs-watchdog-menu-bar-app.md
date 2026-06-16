# CNCjs Watchdog Menu Bar App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the one-shot `cncjs_watchdog.py` script into a persistent macOS menu bar app that auto-connects to CNCjs on `grbl.local`, watches every job for stalls, recovers them, and idles between jobs forever.

**Architecture:** Two layers. `engine.py` holds the CNCjs Socket.IO/Engine.IO protocol + stall logic refactored into a persistent, UI-free engine with injectable clock and send function (fully unit-testable, no hardware). `app.py` is a thin [rumps](https://github.com/jaredks/rumps) menu bar shell that polls engine state, shows a colored dot, drains an event queue into macOS notifications, and toggles launch-at-login.

**Tech Stack:** Python 3, rumps (menu bar), PyJWT, websocket-client, py2app (packaging). Tests with pytest.

**Reference:** The original logic lives in `cncjs_watchdog.py` (copied into this repo root). The design spec is at `docs/superpowers/specs/2026-06-16-cncjs-watchdog-menu-bar-app-design.md`.

---

## File Structure

```
cncjs-watchdog/
  cncwatch/
    __init__.py
    config.py          # Config dataclass + JSON loader (~/.cncjs-watchdog.json)
    engine.py          # WatchdogEngine: persistent observer, state machine, stall recovery
    notifications.py   # Throttled macOS-notification helper
    login_item.py      # Launch-at-login via a ~/Library/LaunchAgents plist
    app.py             # rumps menu bar app
  tests/
    __init__.py
    conftest.py        # FakeClock + helpers
    test_config.py
    test_engine.py
    test_notifications.py
    test_login_item.py
  run_app.py           # entry point: WatchdogApp().run()
  setup.py             # py2app build config
  requirements.txt
  cncjs_watchdog.py    # original reference script (kept as CLI fallback)
```

**Responsibilities:**
- `engine.py` knows nothing about rumps or notifications. It exposes `state`, `sent`, `total`, `stalls_recovered`, and five callbacks. Transport (websocket) is a thin method on it; the parsing/state/stall logic is pure and driven by `handle_frame()` and `tick()`.
- `app.py` knows nothing about Socket.IO. It polls the engine once a second on the main thread and drains a `queue.Queue` of events into notifications — never calling rumps from a background thread.

---

## Task 1: Project scaffold + config

**Files:**
- Create: `requirements.txt`, `cncwatch/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `cncwatch/config.py`, `tests/test_config.py`

- [ ] **Step 1: Create the package skeleton and requirements**

Create `requirements.txt`:

```
rumps==0.4.0
PyJWT==2.8.0
websocket-client==1.7.0
pytest==8.0.0
py2app==0.28.7
```

Create empty `cncwatch/__init__.py` and empty `tests/__init__.py` (zero bytes each).

- [ ] **Step 2: Create the venv and install deps**

Run:
```bash
cd /Users/emileaublet/Dev/cncjs-watchdog
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```
Expected: installs succeed (rumps pulls in pyobjc).

- [ ] **Step 3: Write the failing config test**

Create `tests/test_config.py`:

```python
import json
from cncwatch.config import Config, load_config


def test_defaults_match_original_script():
    c = Config()
    assert c.host == "grbl.local"
    assert c.port == 8000
    assert c.serial_port == "/dev/ttyACM0"
    assert c.stall_secs == 5.0
    assert c.hold_secs == 2.0
    assert c.done_lines == 1


def test_load_missing_file_returns_defaults(tmp_path):
    c = load_config(str(tmp_path / "nope.json"))
    assert c.host == "grbl.local"


def test_load_overrides_known_keys_only(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"host": "192.168.1.50", "stall_secs": 8, "bogus": 1}))
    c = load_config(str(p))
    assert c.host == "192.168.1.50"
    assert c.stall_secs == 8
    assert not hasattr(c, "bogus")
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cncwatch.config'`.

- [ ] **Step 5: Implement config**

Create `cncwatch/config.py`:

```python
import json
import os
from dataclasses import dataclass, fields

CONFIG_PATH = os.path.expanduser("~/.cncjs-watchdog.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cncjs_watchdog.log")
LOG_FILE = os.path.abspath(LOG_FILE)


@dataclass
class Config:
    host: str = "grbl.local"
    port: int = 8000
    serial_port: str = "/dev/ttyACM0"
    baud: int = 115200
    secret: str = "$2a$10$EXZGL.UpR1K2z1DQuFJRpe"
    stall_secs: float = 5.0
    hold_secs: float = 2.0
    confirm_secs: float = 2.0   # after resume, how long to wait for motion before re-arming
    done_lines: int = 1
    heartbeat_secs: float = 60.0


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        return Config()
    with open(path) as f:
        data = json.load(f)
    known = {fld.name for fld in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `./venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add requirements.txt cncwatch/__init__.py cncwatch/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: project scaffold and config loader"
```

---

## Task 2: Engine — state enum, frame parsing, connect/disconnect

**Files:**
- Create: `cncwatch/engine.py`, `tests/conftest.py`
- Test: `tests/test_engine.py`

This task creates the engine with its **complete `__init__`** (all fields, including recovery fields used in Task 4, so `__init__` is never rewritten), frame parsing, connection lifecycle, and state recomputation. `_handle_event` handles only `sender:status` for now; Task 3 extends it.

- [ ] **Step 1: Write the FakeClock helper**

Create `tests/conftest.py`:

```python
class FakeClock:
    """Deterministic clock for engine tests. Call like time.time()."""
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t
```

- [ ] **Step 2: Write the failing engine test**

Create `tests/test_engine.py`:

```python
from cncwatch.config import Config
from cncwatch.engine import WatchdogEngine, State
from tests.conftest import FakeClock


def make_engine():
    sent = []
    clock = FakeClock()
    eng = WatchdogEngine(Config(), send=sent.append, clock=clock)
    return eng, sent, clock


def test_starts_disconnected():
    eng, _, _ = make_engine()
    assert eng.state == State.DISCONNECTED
    assert eng.connected is False


def test_engineio_open_marks_connected_and_idle():
    eng, _, _ = make_engine()
    events = []
    eng.on_connected = lambda: events.append("connected")
    eng.handle_frame('0{"pingInterval":25000,"pingTimeout":20000}')
    assert eng.connected is True
    assert eng.state == State.IDLE
    assert eng.ping_interval == 25.0
    assert events == ["connected"]


def test_server_ping_triggers_pong():
    eng, sent, _ = make_engine()
    eng.handle_frame("2")
    assert sent == ["3"]


def test_disconnect_fires_callback_once():
    eng, _, _ = make_engine()
    eng.handle_frame('0{"pingInterval":25000}')
    events = []
    eng.on_disconnected = lambda: events.append("lost")
    eng.mark_disconnected()
    eng.mark_disconnected()  # idempotent — no second callback
    assert events == ["lost"]
    assert eng.state == State.DISCONNECTED


def test_sender_status_updates_progress():
    eng, _, _ = make_engine()
    eng.handle_frame('0{}')
    eng.handle_frame('42["sender:status",{"sent":1240,"total":5000}]')
    assert eng.sent == 1240
    assert eng.total == 5000
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cncwatch.engine'`.

- [ ] **Step 4: Implement the engine core**

Create `cncwatch/engine.py`:

```python
import json
import time
from enum import Enum

import jwt


class State(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    RUNNING = "running"
    RECOVERING = "recovering"


def make_token(secret):
    return jwt.encode({"iss": "cncjs"}, secret, algorithm="HS256")


def _noop(*_args, **_kwargs):
    return None


class WatchdogEngine:
    """Persistent, UI-free CNCjs stall watchdog.

    Drive it with handle_frame(raw) for incoming Socket.IO frames and tick()
    once a second for stall detection. Both use the injected clock, so the
    whole state machine is testable with no network and no real time.
    """

    def __init__(self, config, send=None, clock=time.time):
        self.cfg = config
        self._send = send            # callable(frame:str); set by transport on ws open
        self._clock = clock

        # callbacks (UI overrides these; default no-ops)
        self.on_state_change = _noop   # (old: State, new: State)
        self.on_connected = _noop
        self.on_disconnected = _noop
        self.on_stall_recovered = _noop  # (count: int)
        self.on_job_finished = _noop     # (reason: str, sent: int, total: int)

        # connection
        self.connected = False
        self.state = State.DISCONNECTED
        self.ping_interval = 25.0

        # machine / job tracking
        self.last_pos = None
        self.last_move = self._clock()
        self.machine_state = "Idle"
        self.workflow_state = "idle"
        self.job_active = False
        self.sent = 0
        self.total = 0

        # recovery sub-state
        self.recovering = False
        self.resume_sent = False
        self.await_confirm = False
        self.recover_started = 0.0
        self.resume_at = 0.0
        self.recovery_attempts = 0
        self.stalls_recovered = 0

    # ── state helpers ──────────────────────────────────────────────────────
    def _set_state(self, new):
        if new != self.state:
            old = self.state
            self.state = new
            self.on_state_change(old, new)

    def _recompute_state(self):
        if not self.connected:
            self._set_state(State.DISCONNECTED)
        elif self.recovering:
            self._set_state(State.RECOVERING)
        elif self.workflow_state == "running":
            self._set_state(State.RUNNING)
        else:
            self._set_state(State.IDLE)

    # ── send helpers ───────────────────────────────────────────────────────
    def _raw(self, frame):
        if self._send:
            self._send(frame)

    def _emit(self, event, *args):
        self._raw("42" + json.dumps([event, *args]))

    # ── connection lifecycle (called by transport) ─────────────────────────
    def mark_connected(self):
        if not self.connected:
            self.connected = True
            self.on_connected()
        self._recompute_state()

    def mark_disconnected(self):
        if self.connected:
            self.connected = False
            self.on_disconnected()
        self.connected = False
        self._recompute_state()

    # ── frame parsing ──────────────────────────────────────────────────────
    def handle_frame(self, raw):
        if not raw:
            return
        eio = raw[0]
        if eio == "0":                       # Engine.IO OPEN (handshake)
            try:
                info = json.loads(raw[1:])
                self.ping_interval = info.get("pingInterval", 25000) / 1000.0
            except Exception:
                pass
            self.mark_connected()
            return
        if eio == "2":                       # server ping → pong
            self._raw("3")
            return
        if eio in ("1", "3"):                # close / pong
            return
        if eio != "4":                       # only "message" frames carry events
            return
        sio = raw[1:]
        if not sio or sio[0] != "2":         # only Socket.IO EVENT frames
            return
        try:
            arr = json.loads(sio[1:])
        except Exception:
            return
        if not isinstance(arr, list) or not arr:
            return
        self._handle_event(arr[0], arr[1:])

    # ── event dispatch (extended in Task 3) ────────────────────────────────
    def _handle_event(self, event, args):
        if event == "sender:status":
            s = args[0] if args and isinstance(args[0], dict) else {}
            self.sent = s.get("sent", self.sent)
            self.total = s.get("total", self.total)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add cncwatch/engine.py tests/conftest.py tests/test_engine.py
git commit -m "feat: engine core — frame parsing, connection lifecycle, state"
```

---

## Task 3: Engine — job lifecycle and movement tracking

**Files:**
- Modify: `cncwatch/engine.py` (extend `_handle_event`, add `_on_workflow`, `_handle_state`, `_reset_recovery`)
- Test: `tests/test_engine.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
def connect(eng):
    eng.handle_frame('0{}')


def test_workflow_running_enters_running_state():
    eng, _, _ = make_engine()
    connect(eng)
    transitions = []
    eng.on_state_change = lambda old, new: transitions.append((old, new))
    eng.handle_frame('42["workflow:state","running"]')
    assert eng.state == State.RUNNING
    assert eng.job_active is True
    assert eng.stalls_recovered == 0
    assert (State.IDLE, State.RUNNING) in transitions


def test_workflow_idle_after_complete_fires_job_finished():
    eng, _, _ = make_engine()
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":5000,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')
    done = []
    eng.on_job_finished = lambda reason, sent, total: done.append((reason, sent, total))
    eng.handle_frame('42["workflow:state","idle"]')
    assert done == [("complete", 5000, 5000)]
    assert eng.job_active is False
    assert eng.state == State.IDLE


def test_workflow_idle_when_unfinished_reports_stopped():
    eng, _, _ = make_engine()
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":1200,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')
    done = []
    eng.on_job_finished = lambda reason, sent, total: done.append((reason, sent, total))
    eng.handle_frame('42["workflow:state","idle"]')
    assert done == [("stopped", 1200, 5000)]


def test_controller_state_records_movement():
    eng, _, clock = make_engine()
    connect(eng)
    clock.advance(3)
    frame = '42["controller:state","Grbl",{"status":{"activeState":"Run","mpos":{"x":1.0,"y":2.0,"z":0.0}}}]'
    eng.handle_frame(frame)
    assert eng.machine_state == "Run"
    assert eng.last_move == clock.t
    assert eng.last_pos == (1.0, 2.0, 0.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: FAIL — `workflow:state` is ignored so state stays IDLE and `on_job_finished` never fires.

- [ ] **Step 3: Extend the engine**

In `cncwatch/engine.py`, replace the entire `_handle_event` method with:

```python
    # ── event dispatch ─────────────────────────────────────────────────────
    def _handle_event(self, event, args):
        if event == "controller:state":
            # ["controller:state", <controllerType>, <state>]
            state = args[1] if len(args) > 1 else (args[0] if args else {})
            self._handle_state(state)
        elif event == "workflow:state":
            self._on_workflow(args[0] if args else "idle")
        elif event == "sender:status":
            s = args[0] if args and isinstance(args[0], dict) else {}
            self.sent = s.get("sent", self.sent)
            self.total = s.get("total", self.total)

    def _on_workflow(self, new):
        if new == "running" and self.workflow_state != "running":
            # fresh job: clear stale movement history and per-job counters
            self.last_move = self._clock()
            self.job_active = True
            self.recovery_attempts = 0
            self.stalls_recovered = 0
            self._reset_recovery()
        # running → idle means the drawing ended (finished or stopped). A pause
        # is running → "paused", so this branch won't fire on an intentional hold.
        if new == "idle" and self.job_active:
            self.job_active = False
            done = bool(self.total) and self.sent >= self.total
            self.on_job_finished("complete" if done else "stopped", self.sent, self.total)
        self.workflow_state = new
        self._recompute_state()

    def _handle_state(self, state):
        status = state.get("status", {}) if isinstance(state, dict) else {}
        self.machine_state = status.get("activeState", self.machine_state)
        mpos = status.get("mpos")
        if mpos:
            pos = (mpos.get("x"), mpos.get("y"), mpos.get("z"))
            if pos != self.last_pos:
                self.last_pos = pos
                self.last_move = self._clock()
                if self.await_confirm:
                    # motion resumed after a recovery cycle → confirmed recovery
                    self.await_confirm = False
                    self.recovering = False
                    self.stalls_recovered += 1
                    self.on_stall_recovered(self.stalls_recovered)
                    self._recompute_state()

    def _reset_recovery(self):
        self.recovering = False
        self.resume_sent = False
        self.await_confirm = False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add cncwatch/engine.py tests/test_engine.py
git commit -m "feat: engine job lifecycle and movement tracking"
```

---

## Task 4: Engine — stall detection and non-blocking recovery

**Files:**
- Modify: `cncwatch/engine.py` (add `tick`, `_begin_recovery`, `_job_finished_by_lines`)
- Test: `tests/test_engine.py` (add cases)

Recovery is modeled as a non-blocking sub-state machine driven by `tick()` (no `time.sleep`), so it is fully testable with the FakeClock:
1. Stall seen (`tick`): emit `gcode:pause`, set `recovering=True`, stamp `recover_started`.
2. After `hold_secs` (`tick`): emit `gcode:resume`, set `await_confirm=True`, stamp `resume_at`.
3. Motion resumes (`_handle_state`): confirmed — increment `stalls_recovered`, clear recovering.
4. If `confirm_secs` pass with no motion (`tick`): re-arm — clear recovering so the next stall re-triggers, exactly like the original loop's "detect again, cycle again".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine.py`:

```python
def run_job(eng):
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":10,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')


def test_no_stall_before_threshold():
    eng, sent, clock = make_engine()
    run_job(eng)
    sent.clear()
    clock.advance(4)          # < stall_secs (5)
    eng.tick()
    assert sent == []
    assert eng.state == State.RUNNING


def test_stall_emits_pause_then_resume_and_confirms():
    eng, sent, clock = make_engine()
    run_job(eng)
    sent.clear()

    clock.advance(5)          # reach stall threshold
    eng.tick()
    assert sent == ['42["command","/dev/ttyACM0","gcode:pause"]']
    assert eng.state == State.RECOVERING
    assert eng.recovery_attempts == 1

    clock.advance(2)          # hold_secs elapsed → resume
    eng.tick()
    assert sent[-1] == '42["command","/dev/ttyACM0","gcode:resume"]'
    assert eng.await_confirm is True

    recovered = []
    eng.on_stall_recovered = recovered.append
    # motion resumes
    eng.handle_frame('42["controller:state","Grbl",{"status":{"activeState":"Run","mpos":{"x":1,"y":0,"z":0}}}]')
    assert recovered == [1]
    assert eng.recovering is False
    assert eng.state == State.RUNNING


def test_recovery_rearms_if_motion_never_resumes():
    eng, sent, clock = make_engine()
    run_job(eng)
    sent.clear()
    clock.advance(5)
    eng.tick()                # pause
    clock.advance(2)
    eng.tick()                # resume, await_confirm
    clock.advance(2)          # confirm_secs elapsed, no motion
    eng.tick()                # re-arm
    assert eng.recovering is False
    assert eng.await_confirm is False
    # next stall triggers a second cycle
    clock.advance(5)
    eng.tick()
    assert eng.recovery_attempts == 2


def test_stall_at_end_of_job_finishes_instead_of_recovering():
    eng, sent, clock = make_engine()
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":5000,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')
    sent.clear()
    done = []
    eng.on_job_finished = lambda r, s, t: done.append(r)
    clock.advance(5)
    eng.tick()
    assert done == ["complete"]
    assert sent == []          # no pause/resume — nothing left to kick
    assert eng.state == State.IDLE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: FAIL with `AttributeError: 'WatchdogEngine' object has no attribute 'tick'`.

- [ ] **Step 3: Add the stall/recovery logic**

Append these methods to the `WatchdogEngine` class in `cncwatch/engine.py`:

```python
    # ── stall detection (called ~1/s by transport; pure given the clock) ────
    def _job_finished_by_lines(self):
        # total is 0 until the first sender:status; never "done" before then.
        return self.total > 0 and self.sent >= self.total - self.cfg.done_lines

    def _begin_recovery(self, now):
        self.recovery_attempts += 1
        self.recovering = True
        self.resume_sent = False
        self.await_confirm = False
        self.recover_started = now
        self.last_move = now
        self._emit("command", self.cfg.serial_port, "gcode:pause")
        self._recompute_state()

    def tick(self):
        now = self._clock()
        if not self.connected:
            return

        if self.recovering:
            if not self.resume_sent and (now - self.recover_started) >= self.cfg.hold_secs:
                self._emit("command", self.cfg.serial_port, "gcode:resume")
                self.resume_sent = True
                self.await_confirm = True
                self.resume_at = now
                self.last_move = now
            elif self.resume_sent and self.await_confirm and \
                    (now - self.resume_at) >= self.cfg.confirm_secs:
                # motion never came back — re-arm so the next stall re-triggers
                self.recovering = False
                self.await_confirm = False
                self.last_move = now
                self._recompute_state()
            return

        if self.workflow_state != "running":
            self.last_move = now
            return

        if (now - self.last_move) >= self.cfg.stall_secs:
            if self._job_finished_by_lines():
                # CNCjs never flipped workflow back to idle, but every line is
                # fed — treat as complete instead of looping pause/resume forever.
                self.job_active = False
                self.workflow_state = "idle"
                self.on_job_finished("complete", self.sent, self.total)
                self._recompute_state()
                return
            self._begin_recovery(now)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_engine.py -v`
Expected: PASS (14 passed).

- [ ] **Step 5: Commit**

```bash
git add cncwatch/engine.py tests/test_engine.py
git commit -m "feat: non-blocking stall detection and recovery"
```

---

## Task 5: Engine — websocket transport (manual verification)

**Files:**
- Modify: `cncwatch/engine.py` (add `run_forever`, `_on_ws_open`, `_safe_send`, `_ping_loop`, `_stall_loop`)

This thin layer wires the pure engine to a real websocket and the 1-second tick. It is verified manually against the live Pi (no unit test — it is I/O glue).

- [ ] **Step 1: Add transport imports**

At the top of `cncwatch/engine.py`, add to the imports:

```python
import threading
import websocket
```

- [ ] **Step 2: Add the transport methods**

Append to the `WatchdogEngine` class:

```python
    # ── websocket transport ────────────────────────────────────────────────
    def _on_ws_open(self, ws):
        self._send = lambda frame: self._safe_send(ws, frame)

    @staticmethod
    def _safe_send(ws, frame):
        try:
            ws.send(frame)
        except Exception:
            pass

    def _ping_loop(self, stop_event):
        while not stop_event.is_set():
            time.sleep(self.ping_interval)
            if self.connected:
                self._raw("2")

    def _stall_loop(self, stop_event):
        while not stop_event.is_set():
            time.sleep(1)
            self.tick()

    def run_forever(self, stop_event):
        """Maintain a connection forever. Reconnects every 5s when the Pi is
        unreachable; the stall loop and ping loop run for the whole lifetime."""
        threading.Thread(target=self._ping_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._stall_loop, args=(stop_event,), daemon=True).start()
        token = make_token(self.cfg.secret)
        url = (f"ws://{self.cfg.host}:{self.cfg.port}"
               f"/socket.io/?transport=websocket&token={token}")
        while not stop_event.is_set():
            ws = websocket.WebSocketApp(
                url,
                on_open=self._on_ws_open,
                on_message=lambda _w, m: self.handle_frame(m),
                on_error=lambda _w, _e: None,
                on_close=lambda _w, _c, _m: self.mark_disconnected(),
            )
            ws.run_forever()
            self.mark_disconnected()
            if stop_event.is_set():
                break
            time.sleep(5)
```

- [ ] **Step 3: Smoke-test the engine against the live Pi**

With the Pi on and CNCjs reachable, create a throwaway `scratch_run.py`:

```python
import threading
from cncwatch.config import load_config
from cncwatch.engine import WatchdogEngine

eng = WatchdogEngine(load_config())
eng.on_connected = lambda: print("CONNECTED")
eng.on_disconnected = lambda: print("DISCONNECTED")
eng.on_state_change = lambda o, n: print("STATE", o.value, "→", n.value)
eng.on_stall_recovered = lambda n: print("RECOVERED", n)
eng.on_job_finished = lambda r, s, t: print("JOB", r, s, t)
eng.run_forever(threading.Event())
```

Run: `./venv/bin/python scratch_run.py`
Expected: prints `CONNECTED` and `STATE disconnected → idle`. Open the CNCjs UI, start a small job → see `STATE idle → running`; on completion → `JOB complete …` and back to `idle`. Power the Pi off → `DISCONNECTED` and `STATE … → disconnected`; power on → reconnects.

- [ ] **Step 4: Delete the scratch file and commit**

```bash
rm scratch_run.py
git add cncwatch/engine.py
git commit -m "feat: websocket transport with persistent reconnect"
```

---

## Task 6: Throttled notifications helper

**Files:**
- Create: `cncwatch/notifications.py`
- Test: `tests/test_notifications.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notifications.py`:

```python
from cncwatch.notifications import Notifier


def make():
    calls = []
    n = Notifier(notify=lambda t, s, m: calls.append((t, s, m)))
    return n, calls


def test_send_passes_through():
    n, calls = make()
    n.send("T", "S", "M")
    assert calls == [("T", "S", "M")]


def test_once_key_dedupes_until_reset():
    n, calls = make()
    n.send("Pi lost", "", "", key="disc", once=True)
    n.send("Pi lost", "", "", key="disc", once=True)
    assert len(calls) == 1
    n.reset("disc")
    n.send("Pi lost", "", "", key="disc", once=True)
    assert len(calls) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_notifications.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cncwatch.notifications'`.

- [ ] **Step 3: Implement the notifier**

Create `cncwatch/notifications.py`:

```python
def _default_notify(title, subtitle, message):
    import rumps
    rumps.notification(title, subtitle, message)


class Notifier:
    """macOS banner notifications with optional once-per-key deduping, so a
    flurry of reconnects can't spam the same banner."""

    def __init__(self, notify=_default_notify):
        self._notify = notify
        self._sent_once = set()

    def send(self, title, subtitle, message, key=None, once=False):
        if once and key is not None:
            if key in self._sent_once:
                return
            self._sent_once.add(key)
        self._notify(title, subtitle, message)

    def reset(self, key):
        self._sent_once.discard(key)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./venv/bin/python -m pytest tests/test_notifications.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add cncwatch/notifications.py tests/test_notifications.py
git commit -m "feat: throttled notification helper"
```

---

## Task 7: Launch-at-login

**Files:**
- Create: `cncwatch/login_item.py`
- Test: `tests/test_login_item.py`

Uses a `LaunchAgent` plist (simple, no extra deps, toggleable from the menu).

- [ ] **Step 1: Write the failing test**

Create `tests/test_login_item.py`:

```python
from cncwatch import login_item


def test_disabled_by_default(tmp_path):
    p = str(tmp_path / "agent.plist")
    assert login_item.is_enabled(p) is False


def test_enable_writes_plist_with_args(tmp_path):
    p = str(tmp_path / "agent.plist")
    login_item.enable(["/usr/bin/open", "-a", "CNCjs Watchdog"], path=p)
    assert login_item.is_enabled(p) is True
    text = open(p).read()
    assert "<string>/usr/bin/open</string>" in text
    assert "<string>CNCjs Watchdog</string>" in text
    assert "<key>RunAtLoad</key>" in text


def test_disable_removes_plist(tmp_path):
    p = str(tmp_path / "agent.plist")
    login_item.enable(["/usr/bin/open"], path=p)
    login_item.disable(path=p)
    assert login_item.is_enabled(p) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_login_item.py -v`
Expected: FAIL with `ImportError: cannot import name 'login_item'`.

- [ ] **Step 3: Implement the login item**

Create `cncwatch/login_item.py`:

```python
import os
from xml.sax.saxutils import escape

LABEL = "com.emileaublet.cncjs-watchdog"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _plist_xml(program_args):
    args = "".join(f"    <string>{escape(a)}</string>\n" for a in program_args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array>\n{args}  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def is_enabled(path=PLIST_PATH):
    return os.path.exists(path)


def enable(program_args, path=PLIST_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(_plist_xml(program_args))


def disable(path=PLIST_PATH):
    if os.path.exists(path):
        os.remove(path)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./venv/bin/python -m pytest tests/test_login_item.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add cncwatch/login_item.py tests/test_login_item.py
git commit -m "feat: launch-at-login via LaunchAgent plist"
```

---

## Task 8: Menu bar app (manual verification)

**Files:**
- Create: `cncwatch/app.py`, `run_app.py`

The app polls engine state once a second on the main thread and drains a `queue.Queue` of events into notifications — never touching rumps from a background thread. Verified manually.

- [ ] **Step 1: Implement the app**

Create `cncwatch/app.py`:

```python
import os
import queue
import subprocess
import sys
import threading

import rumps

from .config import load_config, LOG_FILE
from .engine import WatchdogEngine, State
from .notifications import Notifier
from . import login_item

# Colored dots map to the spec's status colors:
# disconnected→grey/white, idle→white, running→green, recovering→amber.
DOTS = {
    State.DISCONNECTED: "⚪",
    State.IDLE: "⚪",
    State.RUNNING: "🟢",
    State.RECOVERING: "🟠",
}


def _login_program_args():
    """How launchd should relaunch us. When frozen into a .app bundle, open the
    bundle; in dev, re-run this module with the same interpreter."""
    if getattr(sys, "frozen", False):
        bundle = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
        return ["/usr/bin/open", "-a", bundle]
    return [sys.executable, "-m", "cncwatch.app"]


class WatchdogApp(rumps.App):
    def __init__(self):
        super().__init__("⚪ CNC", quit_button=None)
        self.cfg = load_config()
        self.notifier = Notifier()
        self.events = queue.Queue()
        self.stop_event = threading.Event()

        self.engine = WatchdogEngine(self.cfg)
        self._wire_engine()

        self.status_item = rumps.MenuItem("Connecting…")
        self.recovered_item = rumps.MenuItem("Stalls recovered: 0")
        self.login_btn = rumps.MenuItem("Launch at login", callback=self._toggle_login)
        self.login_btn.state = login_item.is_enabled()
        self.menu = [
            self.status_item,
            self.recovered_item,
            None,
            rumps.MenuItem("Open log", callback=self._open_log),
            self.login_btn,
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        threading.Thread(
            target=self.engine.run_forever, args=(self.stop_event,), daemon=True
        ).start()
        self._timer = rumps.Timer(self._poll, 1)
        self._timer.start()

    def _wire_engine(self):
        e = self.engine
        e.on_connected = lambda: self.events.put(("connected",))
        e.on_disconnected = lambda: self.events.put(("disconnected",))
        e.on_stall_recovered = lambda n: self.events.put(("recovered", n))
        e.on_job_finished = lambda r, s, t: self.events.put(("job", r, s, t))

    # ── main-thread polling ────────────────────────────────────────────────
    def _poll(self, _sender):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(ev)
        st = self.engine.state
        self.title = f"{DOTS.get(st, '⚪')} CNC"
        self.status_item.title = self._status_text(st)
        self.recovered_item.title = f"Stalls recovered: {self.engine.stalls_recovered}"

    def _status_text(self, st):
        if st == State.DISCONNECTED:
            return "Disconnected — waiting for grbl.local"
        if st == State.IDLE:
            return "Connected — idle"
        sent, total = self.engine.sent, self.engine.total
        label = "Recovering" if st == State.RECOVERING else "Running"
        return f"{label} — {sent:,} / {total:,} lines"

    def _handle_event(self, ev):
        kind = ev[0]
        if kind == "connected":
            self.notifier.reset("disconnected")
            self.notifier.send("CNCjs Watchdog", "", "Connected to grbl.local",
                               key="connected", once=True)
        elif kind == "disconnected":
            self.notifier.reset("connected")
            self.notifier.send("CNCjs Watchdog", "", "Lost connection to grbl.local",
                               key="disconnected", once=True)
        elif kind == "recovered":
            self.notifier.send("CNCjs Watchdog", "Stall recovered",
                               f"Recovery #{ev[1]} — motion resumed")
        elif kind == "job":
            _, reason, sent, total = ev
            msg = (f"Drawing complete — {sent:,}/{total:,} lines"
                   if reason == "complete" else f"Drawing stopped — {sent:,}/{total:,} lines")
            self.notifier.send("CNCjs Watchdog", "Job finished", msg)

    # ── menu actions ───────────────────────────────────────────────────────
    def _open_log(self, _sender):
        subprocess.run(["open", LOG_FILE], check=False)

    def _toggle_login(self, sender):
        if sender.state:
            login_item.disable()
            sender.state = False
        else:
            login_item.enable(_login_program_args())
            sender.state = True

    def _quit(self, _sender):
        self.stop_event.set()
        rumps.quit_application()
```

Create `run_app.py`:

```python
from cncwatch.app import WatchdogApp

if __name__ == "__main__":
    WatchdogApp().run()
```

- [ ] **Step 2: Run the app in dev and verify**

Run: `./venv/bin/python run_app.py`
Expected:
- A `⚪ CNC` item appears in the menu bar.
- With the Pi up, within ~1s it shows `Connected — idle` and a "Connected to grbl.local" banner.
- Start a small job in the CNCjs UI → dot turns 🟢, status shows `Running — n / total lines`.
- Force a stall (or let one happen) → dot turns 🟠, then a "Stall recovered" banner, count increments.
- Job completes → "Job finished" banner, dot back to ⚪.
- Toggle "Launch at login" → check `ls ~/Library/LaunchAgents/com.emileaublet.cncjs-watchdog.plist` appears/disappears.
- "Open log" opens the log; "Quit" exits.

- [ ] **Step 3: Commit**

```bash
git add cncwatch/app.py run_app.py
git commit -m "feat: rumps menu bar app"
```

---

## Task 9: Packaging + README

**Files:**
- Create: `setup.py`, `README.md`

- [ ] **Step 1: Create the py2app build config**

Create `setup.py`:

```python
from setuptools import setup

APP = ["run_app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "CNCjs Watchdog",
        "LSUIElement": True,   # menu bar only — no Dock icon
    },
    "packages": ["rumps", "jwt", "websocket", "cncwatch"],
}

setup(
    name="CNCjs Watchdog",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
```

- [ ] **Step 2: Build the .app and verify it launches**

Run:
```bash
./venv/bin/python setup.py py2app
open "dist/CNCjs Watchdog.app"
```
Expected: builds without error; launching shows the `⚪ CNC` menu bar item with no Dock icon and the same behavior as Task 8. (First launch may need the Notifications permission granted in System Settings.)

- [ ] **Step 3: Write the README**

Create `README.md`:

```markdown
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
```

- [ ] **Step 4: Run the full test suite**

Run: `./venv/bin/python -m pytest -v`
Expected: all tests pass (config, engine, notifications, login_item).

- [ ] **Step 5: Commit**

```bash
git add setup.py README.md
git commit -m "feat: py2app packaging and README"
```

- [ ] **Step 6: Ignore build artifacts (if not already)**

Confirm `.gitignore` contains `build/` and `dist/`. If `dist/` or `build/` were committed, run `git rm -r --cached build dist` and commit.

```bash
git commit -am "chore: ignore py2app build artifacts" || true
```

---

## Self-review notes (spec coverage)

- Persistent observer, never self-terminates → Task 3 (`_on_workflow` resets to idle) + Task 5 (reconnect loop).
- Passive observer, no `open` emit → engine never emits `open` (Tasks 2–5).
- Always-on stall loop gated on `workflow:state` → Task 4 `tick()` + Task 5 `_stall_loop`.
- State machine (disconnected/idle/running/recovering) → Task 2 `State` + `_recompute_state`.
- Colored dot + menu (status, recovered count, open log, launch at login, quit) → Task 8.
- macOS notifications, throttled → Task 6 + Task 8 `_handle_event`.
- Launch at login → Task 7 + Task 8 toggle.
- Config file → Task 1.
- Packaging into a menu-bar-only .app → Task 9.
```
