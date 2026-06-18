#!/usr/bin/env python3
"""
CNCjs stall watchdog
Detects when a CNCjs job is running but the machine has stopped moving
(a stalled sender: Grbl sits Idle while CNCjs still feeds the job), then
toggles the sender pause/resume to kick it back into feeding lines.

CNCjs exposes a Socket.IO (Engine.IO) endpoint, NOT a raw-JSON WebSocket.
This client speaks just enough of that protocol to talk to it:

  Engine.IO packet types (first char of every frame):
    0 open   1 close   2 ping   3 pong   4 message
  Socket.IO packet types (second char, only when Engine.IO type is 4 "message"):
    0 CONNECT   1 DISCONNECT   2 EVENT
  An event frame therefore looks like:  42["event", arg1, arg2, ...]
"""

import os
import json
import time
import logging
import threading
from logging.handlers import RotatingFileHandler

import jwt
import websocket

# ── config ────────────────────────────────────────────────────────────────────
HOST           = "grbl.local"
PORT           = 8000
SERIAL_PORT    = "/dev/ttyACM0"
BAUD           = 115200
SECRET         = ""     # your CNCjs secret (from ~/.cncrc / cncrc.cfg) — fill this in
STALL_SECS     = 5      # seconds of no movement while a job runs → stall
HOLD_SECS      = 2      # seconds to wait between sender pause and resume
DONE_LINES     = 1      # lines still unfed at/under this count → treat job as complete
HEARTBEAT_SECS = 60     # log a "still watching" line at least this often
LOG_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "cncjs_watchdog.log")
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("watchdog")

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    # keep the last ~1 MB across 3 rotated files so a long run doesn't grow forever
    fileh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    fileh.setFormatter(fmt)
    log.addHandler(fileh)

def make_token(secret):
    return jwt.encode({"iss": "cncjs"}, secret, algorithm="HS256")

class Watchdog:
    def __init__(self):
        self.token         = make_token(SECRET)
        self.ws            = None
        self.last_pos      = None
        self.last_move     = time.time()
        self.machine_state = "Idle"
        self.workflow_state = "idle"       # "running" while a CNCjs job is in progress
        self.recovering    = False
        self.connected     = False
        self.ping_interval = 25.0          # seconds; updated from server handshake
        self._stall_started = False        # ensure only one stall loop ever runs
        self.attempt       = 0             # recovery attempts for the current stall
        self.await_confirm = False         # waiting to see motion resume post-recovery
        self.job_active    = False         # True once a job has started running
        self.sent          = 0             # lines fed so far (from sender:status)
        self.total         = 0             # total lines in the job
        self.stop          = threading.Event()  # set → exit instead of reconnecting

    # ── WebSocket callbacks ───────────────────────────────────────────────────

    def on_open(self, ws):
        log.info("socket connected — waiting for CNCjs handshake")

    def on_message(self, ws, raw):
        if not raw:
            return

        eio_type = raw[0]

        if eio_type == "0":                    # Engine.IO OPEN (handshake)
            try:
                info = json.loads(raw[1:])
                self.ping_interval = info.get("pingInterval", 25000) / 1000.0
            except Exception:
                pass
            self.connected = True
            log.info("connected to CNCjs (ping interval %.0fs)", self.ping_interval)
            return

        if eio_type == "2":                    # server ping → pong
            self._raw_send("3")
            return

        if eio_type in ("1", "3"):             # close / pong — nothing to do
            return

        if eio_type != "4":                    # only "message" frames carry events
            return

        sio = raw[1:]
        if not sio:
            return
        sio_type = sio[0]

        if sio_type == "0":                    # Socket.IO CONNECT (namespace ready)
            return
        if sio_type == "1":                    # Socket.IO DISCONNECT
            return
        if sio_type != "2":                    # only EVENT frames from here
            return

        try:
            arr = json.loads(sio[1:])
        except Exception:
            return
        if not isinstance(arr, list) or not arr:
            return

        event = arr[0]
        args  = arr[1:]
        self._handle_event(event, args)

    def on_error(self, ws, err):
        log.error("WebSocket error: %s", err)

    def on_close(self, ws, code, msg):
        self.connected = False
        log.warning("disconnected (code=%s) — retrying in 5s…", code)

    # ── event dispatch ────────────────────────────────────────────────────────

    def _handle_event(self, event, args):
        if event == "startup":
            log.info("CNCjs ready — opening serial port %s @ %d", SERIAL_PORT, BAUD)
            # CNCjs 'open' takes positional args: (port, options)
            self._emit("open", SERIAL_PORT, {
                "controllerType": "Grbl",
                "baudrate":       BAUD,
            })

        elif event == "serialport:open":
            log.info("serial port open — watching for stalls (threshold: %ds)", STALL_SECS)
            if not self._stall_started:
                self._stall_started = True
                threading.Thread(target=self._stall_loop, daemon=True).start()

        elif event == "controller:state":
            # emitted as ["controller:state", <controllerType>, <state>]
            state = args[1] if len(args) > 1 else (args[0] if args else {})
            self._handle_state(state)

        elif event == "workflow:state":
            # ["workflow:state", "running" | "paused" | "idle"]
            new_state = args[0] if args else "idle"
            if new_state != self.workflow_state:
                log.info("workflow state: %s → %s", self.workflow_state, new_state)
            if new_state == "running" and self.workflow_state != "running":
                self.last_move = time.time()   # fresh start; no stale movement history
                self.attempt = 0
                self.job_active = True
            # running → idle means the drawing ended (finished at 100% or stopped).
            # A pause is running → "paused", so this won't fire on an intentional hold.
            if new_state == "idle" and self.job_active:
                self.job_active = False
                done = self.total and self.sent >= self.total
                self._shutdown("drawing complete" if done else "drawing stopped")
            self.workflow_state = new_state

        elif event == "sender:status":
            # ["sender:status", {sent, total, ...}] — track progress for shutdown
            s = args[0] if args and isinstance(args[0], dict) else {}
            self.sent  = s.get("sent",  self.sent)
            self.total = s.get("total", self.total)

        elif event == "serialport:error":
            log.error("serial error: %s", args)

    # ── state tracking ────────────────────────────────────────────────────────

    def _handle_state(self, state):
        status = state.get("status", {}) if isinstance(state, dict) else {}
        self.machine_state = status.get("activeState", self.machine_state)

        mpos = status.get("mpos")
        if mpos:
            pos = (mpos.get("x"), mpos.get("y"), mpos.get("z"))
            if pos != self.last_pos:
                self.last_pos  = pos
                self.last_move = time.time()
                if self.await_confirm:
                    self.await_confirm = False
                    log.info("✓ motion resumed after %d attempt(s) — recovered", self.attempt)
                    self.attempt = 0

    # ── stall detection loop ──────────────────────────────────────────────────

    def _stall_loop(self):
        last_heartbeat = time.time()
        while True:
            time.sleep(1)
            if self.recovering or not self.connected:
                continue
            # A stall = a job is running but the machine has not moved.
            # When stalled, Grbl sits in "Idle" (planner buffer drained) while
            # CNCjs still reports the workflow as "running" — so we key off the
            # workflow state and movement, NOT the machine's Run/Idle state.
            if self.workflow_state != "running":
                self.last_move = time.time()  # reset timer when no job is running
                continue

            stalled_for = time.time() - self.last_move

            # periodic "still alive" line so the log shows it's actively watching
            if time.time() - last_heartbeat >= HEARTBEAT_SECS:
                last_heartbeat = time.time()
                log.info("watching — state=%s, job running, %.1fs since last move",
                         self.machine_state, stalled_for)

            if stalled_for >= STALL_SECS:
                last_heartbeat = time.time()
                # If every line (or all but the last few) has already been fed,
                # there is nothing left to kick — CNCjs just never flipped the
                # workflow back to "idle". Recovering here would loop pause/resume
                # forever, so treat this as a finished drawing and shut down.
                if self._job_finished():
                    self._shutdown("drawing complete")
                    return
                self._recover(stalled_for)

    def _job_finished(self):
        # total is 0 until the first sender:status arrives; never call done then.
        return self.total > 0 and self.sent >= self.total - DONE_LINES

    def _recover(self, stalled_for):
        # One (pause → resume) cycle. CNCjs's *sender* is what stalls (it stops
        # feeding lines while Grbl sits Idle), so we toggle the sender — not the
        # raw Grbl !/~ realtime bytes, which don't move the sender state machine.
        # If this cycle doesn't take, the watch loop simply detects the stall
        # again and runs another cycle, the same way you do it by hand.
        self.recovering = True
        print(f"\n[watchdog] ⚠ stall detected ({stalled_for:.1f}s no movement, state={self.machine_state}, job running)")
        print("[watchdog] → pausing sender (gcode:pause)")
        self._emit("command", SERIAL_PORT, "gcode:pause")
        time.sleep(HOLD_SECS)
        print("[watchdog] → resuming sender (gcode:resume)")
        self._emit("command", SERIAL_PORT, "gcode:resume")
        self.last_move  = time.time()
        time.sleep(2)  # let motion actually start before judging the next stall
        self.recovering = False
        print("[watchdog] recovery sent — watching…\n")

    def _shutdown(self, reason):
        log.info("%s (%d/%d lines) — shutting down watchdog", reason, self.sent, self.total)
        self.stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ── helpers ───────────────────────────────────────────────────────────────

    def _raw_send(self, frame):
        """Send a raw Engine.IO frame, ignoring errors on a dead socket."""
        try:
            if self.ws:
                self.ws.send(frame)
        except Exception:
            pass

    def _emit(self, event, *args):
        """Send a Socket.IO EVENT frame: 42["event", arg1, ...]."""
        self._raw_send("42" + json.dumps([event, *args]))

    def _ping_loop(self):
        """Engine.IO keepalive: client pings the server periodically."""
        while True:
            time.sleep(self.ping_interval)
            if self.connected:
                self._raw_send("2")

    def run(self):
        threading.Thread(target=self._ping_loop, daemon=True).start()
        url = f"ws://{HOST}:{PORT}/socket.io/?transport=websocket&token={self.token}"
        while not self.stop.is_set():
            self.ws = websocket.WebSocketApp(
                url,
                on_open    = self.on_open,
                on_message = self.on_message,
                on_error   = self.on_error,
                on_close   = self.on_close,
            )
            log.info("connecting to ws://%s:%d …", HOST, PORT)
            self.ws.run_forever()
            self.connected = False
            if self.stop.is_set():
                break
            time.sleep(5)


if __name__ == "__main__":
    setup_logging()
    log.info("CNCjs stall watchdog starting — Ctrl+C to stop")
    log.info("  host:      %s:%d", HOST, PORT)
    log.info("  port:      %s @ %d", SERIAL_PORT, BAUD)
    log.info("  threshold: %ds stall → pause %ds → resume", STALL_SECS, HOLD_SECS)
    log.info("  log file:  %s", LOG_FILE)
    try:
        Watchdog().run()
    except KeyboardInterrupt:
        log.info("stopped (Ctrl+C)")
    else:
        log.info("watchdog exited")
