from cncwatch.config import Config
from cncwatch.engine import WatchdogEngine, State
from tests.conftest import FakeClock


def make_engine():
    sent = []
    clock = FakeClock()
    eng = WatchdogEngine(Config(), send=sent.append, clock=clock)
    return eng, sent, clock


def test_starts_disconnected():
    eng, _, _ = make_engine()
    assert eng.state == State.DISCONNECTED
    assert eng.connected is False


def test_engineio_open_marks_connected_and_idle():
    eng, _, _ = make_engine()
    events = []
    eng.on_connected = lambda: events.append("connected")
    eng.handle_frame('0{"pingInterval":25000,"pingTimeout":20000}')
    assert eng.connected is True
    assert eng.state == State.IDLE
    assert eng.ping_interval == 25.0
    assert events == ["connected"]


def test_server_ping_triggers_pong():
    eng, sent, _ = make_engine()
    eng.handle_frame("2")
    assert sent == ["3"]


def test_disconnect_fires_callback_once():
    eng, _, _ = make_engine()
    eng.handle_frame('0{"pingInterval":25000}')
    events = []
    eng.on_disconnected = lambda: events.append("lost")
    eng.mark_disconnected()
    eng.mark_disconnected()  # idempotent — no second callback
    assert events == ["lost"]
    assert eng.state == State.DISCONNECTED


def test_sender_status_updates_progress():
    eng, _, _ = make_engine()
    eng.handle_frame('0{}')
    eng.handle_frame('42["sender:status",{"sent":1240,"total":5000}]')
    assert eng.sent == 1240
    assert eng.total == 5000
