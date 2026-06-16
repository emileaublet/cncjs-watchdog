from cncwatch import login_item


def test_disabled_by_default(tmp_path):
    p = str(tmp_path / "agent.plist")
    assert login_item.is_enabled(p) is False


def test_enable_writes_plist_with_args(tmp_path):
    p = str(tmp_path / "agent.plist")
    login_item.enable(["/usr/bin/open", "-a", "CNCjs Watchdog"], path=p)
    assert login_item.is_enabled(p) is True
    text = open(p).read()
    assert "<string>/usr/bin/open</string>" in text
    assert "<string>CNCjs Watchdog</string>" in text
    assert "<key>RunAtLoad</key>" in text


def test_disable_removes_plist(tmp_path):
    p = str(tmp_path / "agent.plist")
    login_item.enable(["/usr/bin/open"], path=p)
    login_item.disable(path=p)
    assert login_item.is_enabled(p) is False
