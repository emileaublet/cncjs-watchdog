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
