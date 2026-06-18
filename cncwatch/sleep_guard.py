import os
import subprocess


def _default_spawn():
    # -i prevents macOS idle system sleep; -w ties caffeinate's lifetime to ours
    # so it can never outlive the app (it auto-exits if our process dies).
    return subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])


class SleepGuard:
    """Holds a macOS power assertion (via `caffeinate`) to keep the Mac awake
    while a job is running, so the stall watchdog isn't frozen by idle sleep.
    acquire()/release() are idempotent."""

    def __init__(self, spawn=_default_spawn):
        self._spawn = spawn
        self._proc = None

    @property
    def active(self):
        return self._proc is not None

    def acquire(self):
        if self._proc is None:
            self._proc = self._spawn()

    def release(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
