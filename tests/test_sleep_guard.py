from cncwatch.sleep_guard import SleepGuard


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def make():
    spawned = []

    def spawn():
        p = FakeProc()
        spawned.append(p)
        return p

    return SleepGuard(spawn=spawn), spawned


def test_acquire_spawns_once():
    g, spawned = make()
    assert g.active is False
    g.acquire()
    g.acquire()   # idempotent — no second caffeinate
    assert len(spawned) == 1
    assert g.active is True


def test_release_terminates():
    g, spawned = make()
    g.acquire()
    g.release()
    assert spawned[0].terminated is True
    assert g.active is False


def test_release_when_idle_is_noop():
    g, _ = make()
    g.release()
    assert g.active is False


def test_reacquire_after_release():
    g, spawned = make()
    g.acquire()
    g.release()
    g.acquire()
    assert len(spawned) == 2
    assert g.active is True
