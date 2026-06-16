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


def connect(eng):
    eng.handle_frame('0{}')


def test_workflow_running_enters_running_state():
    eng, _, _ = make_engine()
    connect(eng)
    transitions = []
    eng.on_state_change = lambda old, new: transitions.append((old, new))
    eng.handle_frame('42["workflow:state","running"]')
    assert eng.state == State.RUNNING
    assert eng.job_active is True
    assert eng.stalls_recovered == 0
    assert (State.IDLE, State.RUNNING) in transitions


def test_workflow_idle_after_complete_fires_job_finished():
    eng, _, _ = make_engine()
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":5000,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')
    done = []
    eng.on_job_finished = lambda reason, sent, total: done.append((reason, sent, total))
    eng.handle_frame('42["workflow:state","idle"]')
    assert done == [("complete", 5000, 5000)]
    assert eng.job_active is False
    assert eng.state == State.IDLE


def test_workflow_idle_when_unfinished_reports_stopped():
    eng, _, _ = make_engine()
    connect(eng)
    eng.handle_frame('42["sender:status",{"sent":1200,"total":5000}]')
    eng.handle_frame('42["workflow:state","running"]')
    done = []
    eng.on_job_finished = lambda reason, sent, total: done.append((reason, sent, total))
    eng.handle_frame('42["workflow:state","idle"]')
    assert done == [("stopped", 1200, 5000)]


def test_controller_state_records_movement():
    eng, _, clock = make_engine()
    connect(eng)
    clock.advance(3)
    frame = '42["controller:state","Grbl",{"status":{"activeState":"Run","mpos":{"x":1.0,"y":2.0,"z":0.0}}}]'
    eng.handle_frame(frame)
    assert eng.machine_state == "Run"
    assert eng.last_move == clock.t
    assert eng.last_pos == (1.0, 2.0, 0.0)
