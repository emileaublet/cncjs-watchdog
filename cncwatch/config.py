import json
import os
from dataclasses import asdict, dataclass, fields

CONFIG_PATH = os.path.expanduser("~/.cncjs-watchdog.json")
LOG_FILE = os.path.expanduser("~/Library/Logs/cncjs-watchdog.log")


@dataclass
class Config:
    host: str = ""  # hostname or IP of the machine running CNCjs — set in ~/.cncjs-watchdog.json
    port: int = 8000
    serial_port: str = "/dev/ttyACM0"
    baud: int = 115200
    controller_type: str = "Grbl"
    secret: str = ""  # your CNCjs secret — set it in ~/.cncjs-watchdog.json (see README)
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


def save_config(cfg, path=CONFIG_PATH):
    """Write every field of `cfg` to `path` as pretty JSON. The file holds the
    CNCjs secret, so restrict it to the owner (0600)."""
    with open(path, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
        f.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def ensure_config_file(path=CONFIG_PATH):
    """Make sure a config file exists, pre-filled with the current effective
    config (defaults merged with any existing file). Returns the path."""
    save_config(load_config(path), path)
    return path
