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
        self._recompute_state()

    def mark_disconnected(self):
        if self.connected:
            self.connected = False
            self.on_disconnected()
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
        # running -> idle means the drawing ended (finished or stopped). A pause
        # is running -> "paused", so this branch won't fire on an intentional hold.
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
                    # motion resumed after a recovery cycle -> confirmed recovery
                    self.await_confirm = False
                    self.recovering = False
                    self.stalls_recovered += 1
                    self.on_stall_recovered(self.stalls_recovered)
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
                self._recompute_state()
                return
            self._begin_recovery(now)
