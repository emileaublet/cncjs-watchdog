from cncwatch.notifications import Notifier


def make():
    calls = []
    n = Notifier(notify=lambda t, s, m: calls.append((t, s, m)))
    return n, calls


def test_send_passes_through():
    n, calls = make()
    n.send("T", "S", "M")
    assert calls == [("T", "S", "M")]


def test_once_key_dedupes_until_reset():
    n, calls = make()
    n.send("Pi lost", "", "", key="disc", once=True)
    n.send("Pi lost", "", "", key="disc", once=True)
    assert len(calls) == 1
    n.reset("disc")
    n.send("Pi lost", "", "", key="disc", once=True)
    assert len(calls) == 2
