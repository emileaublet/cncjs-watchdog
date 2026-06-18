import json
import logging
import os
import threading
import time
from enum import Enum
from logging.handlers import RotatingFileHandler

import jwt
import websocket

log = logging.getLogger("cncwatch")


def setup_logging(path=None):
    """Configure the 'cncwatch' logger with a rotating file handler + console.
    Idempotent: re-clears handlers so repeated calls don't duplicate output."""
    from .config import LOG_FILE
    path = path or LOG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fileh = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    fileh.setFormatter(fmt)
    log.addHandler(fileh)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    return log


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
        self._last_tick = None  # wall-clock of the previous tick; detects sleep/wake jumps

    # state helpers
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

    # send helpers
    def _raw(self, frame):
        if self._send:
            self._send(frame)

    def _emit(self, event, *args):
        self._raw("42" + json.dumps([event, *args], separators=(',', ':')))

    # connection lifecycle (called by transport)
    def mark_connected(self):
        if not self.connected:
            self.connected = True
            self.on_connected()
            log.info("connected to %s:%d", self.cfg.host, self.cfg.port)
        self._recompute_state()

    def mark_disconnected(self):
        if self.connected:
            self.connected = False
            self.on_disconnected()
            log.warning("disconnected from %s:%d", self.cfg.host, self.cfg.port)
        self.connected = False
        self._recompute_state()

    # frame parsing
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
        if eio == "2":                       # server ping -> pong
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

    # event dispatch
    def _handle_event(self, event, args):
        if event == "startup":
            # CNCjs only broadcasts controller:state / workflow:state /
            # sender:status to sockets that have JOINED a port via "open".
            # This is a non-disruptive subscribe — if the web UI already has
            # the port open, CNCjs reuses that connection and just adds us as
            # a listener; it does not restart the port or start/stop jobs.
            log.info("CNCjs ready — subscribing to %s", self.cfg.serial_port)
            self._emit("open", self.cfg.serial_port, {
                "controllerType": self.cfg.controller_type,
                "baudrate": self.cfg.baud,
            })
        elif event == "controller:state":
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
            log.info("job started")
        # running -> idle means the drawing ended (finished or stopped). A pause
        # is running -> "paused", so this branch won't fire on an intentional hold.
        if new == "idle" and self.job_active:
            self.job_active = False
            done = bool(self.total) and self.sent >= self.total
            self.on_job_finished("complete" if done else "stopped", self.sent, self.total)
            log.info("job finished (%s) — %d/%d lines", "complete" if done else "stopped", self.sent, self.total)
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
                    # motion resumed after a recovery cycle -> confirmed recovery
                    self.await_confirm = False
                    self.recovering = False
                    self.stalls_recovered += 1
                    self.on_stall_recovered(self.stalls_recovered)
                    log.info("stall recovered (#%d) — motion resumed", self.stalls_recovered)
                    self._recompute_state()

    def _reset_recovery(self):
        self.recovering = False
        self.resume_sent = False
        self.await_confirm = False

    # stall detection (called ~1/s by transport; pure given the clock)
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
        log.warning("stall detected — pausing sender (attempt #%d)", self.recovery_attempts)
        self._recompute_state()

    def _wake_gap(self):
        # A tick gap this large means the process was suspended (system sleep),
        # not that the machine stalled. Comfortably above stall_secs so a normal
        # stall is never mistaken for a wake.
        return max(30.0, self.cfg.stall_secs * 3)

    def tick(self):
        now = self._clock()
        prev = self._last_tick
        self._last_tick = now
        # If we were frozen (Mac slept) the wall clock jumps forward. Don't read
        # that gap as a stall — reset the movement timer and skip this cycle.
        if prev is not None and (now - prev) > self._wake_gap():
            self.last_move = now
            log.info("woke after %.0fs gap — resetting stall timer", now - prev)
            return

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
                # motion never came back -> re-arm so the next stall re-triggers
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
                # fed -> treat as complete instead of looping pause/resume forever.
                self.job_active = False
                self.workflow_state = "idle"
                self.on_job_finished("complete", self.sent, self.total)
                log.info("job complete by line count — %d/%d", self.sent, self.total)
                self._recompute_state()
                return
            self._begin_recovery(now)

    # websocket transport
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
        last_heartbeat = time.time()
        while not stop_event.is_set():
            time.sleep(1)
            self.tick()
            now = time.time()
            if self.workflow_state == "running" and \
                    now - last_heartbeat >= self.cfg.heartbeat_secs:
                last_heartbeat = now
                log.info("watching — machine=%s, %.1fs since last move",
                         self.machine_state, now - self.last_move)

    def run_forever(self, stop_event):
        """Maintain a connection forever. Reconnects every 5s when the Pi is
        unreachable; the stall loop and ping loop run for the whole lifetime."""
        threading.Thread(target=self._ping_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._stall_loop, args=(stop_event,), daemon=True).start()
        while not stop_event.is_set():
            # Build URL/token from the CURRENT config each connection, so a
            # live config reload (which closes the socket) reconnects with the
            # new host/port/secret.
            token = make_token(self.cfg.secret)
            url = (f"ws://{self.cfg.host}:{self.cfg.port}"
                   f"/socket.io/?transport=websocket&token={token}")
            ws = websocket.WebSocketApp(
                url,
                on_open=self._on_ws_open,
                on_message=lambda _w, m: self.handle_frame(m),
                on_error=lambda _w, _e: None,
                on_close=lambda _w, _c, _m: self.mark_disconnected(),
            )
            self._ws = ws
            ws.run_forever()
            self.mark_disconnected()
            if stop_event.is_set():
                break
            time.sleep(5)

    def reload(self, new_cfg):
        """Apply a new Config to the running engine. Threshold/serial settings
        take effect on the next tick; connection settings (host/port/secret/
        baud) take effect by forcing a reconnect, which also re-subscribes via
        'open' on the next startup."""
        self.cfg = new_cfg
        log.info("config reloaded")
        ws = getattr(self, "_ws", None)
        if ws is not None:
            try:
                ws.close()   # triggers reconnect with the new config
            except Exception:
                pass
