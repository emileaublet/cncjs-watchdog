from cncwatch.app import format_title
from cncwatch.engine import State


def test_running_shows_percent():
    assert format_title(State.RUNNING, 1240, 5000) == "🟢 24%"


def test_recovering_shows_percent():
    assert format_title(State.RECOVERING, 2500, 5000) == "🟠 50%"


def test_idle_shows_cnc():
    assert format_title(State.IDLE, 0, 0) == "⚪ CNC"


def test_disconnected_shows_cnc():
    assert format_title(State.DISCONNECTED, 0, 0) == "⚪ CNC"


def test_running_with_zero_total_avoids_div_by_zero():
    assert format_title(State.RUNNING, 0, 0) == "🟢 CNC"
