import os
import queue
import subprocess
import sys
import threading

import rumps

from .config import load_config, LOG_FILE
from .engine import WatchdogEngine, State, setup_logging
from .notifications import Notifier
from . import login_item

# Colored dots map to the spec's status colors:
# disconnected->grey/white, idle->white, running->green, recovering->amber.
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
        setup_logging()
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

    # main-thread polling
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

    # menu actions
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
