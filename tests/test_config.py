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
