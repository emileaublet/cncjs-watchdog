import json
import os
from cncwatch.config import Config, load_config, save_config, ensure_config_file


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


def test_save_then_load_roundtrips_all_fields(tmp_path):
    p = str(tmp_path / "cfg.json")
    cfg = Config(host="1.2.3.4", stall_secs=9.0, secret="abc", controller_type="Grbl")
    save_config(cfg, p)
    assert load_config(p) == cfg


def test_save_restricts_permissions(tmp_path):
    p = str(tmp_path / "cfg.json")
    save_config(Config(), p)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_ensure_config_file_creates_prefilled_defaults(tmp_path):
    p = str(tmp_path / "cfg.json")
    ensure_config_file(p)
    assert os.path.exists(p)
    assert load_config(p) == Config()
