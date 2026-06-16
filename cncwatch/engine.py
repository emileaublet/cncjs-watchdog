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
        self._raw("42" + json.dumps([event, *args]))

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

    # event dispatch (extended in Task 3)
    def _handle_event(self, event, args):
        if event == "sender:status":
            s = args[0] if args and isinstance(args[0], dict) else {}
            self.sent = s.get("sent", self.sent)
            self.total = s.get("total", self.total)
