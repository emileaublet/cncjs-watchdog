def _default_notify(title, subtitle, message):
    import rumps
    rumps.notification(title, subtitle, message)


class Notifier:
    """macOS banner notifications with optional once-per-key deduping, so a
    flurry of reconnects can't spam the same banner."""

    def __init__(self, notify=_default_notify):
        self._notify = notify
        self._sent_once = set()

    def send(self, title, subtitle, message, key=None, once=False):
        if once and key is not None:
            if key in self._sent_once:
                return
            self._sent_once.add(key)
        self._notify(title, subtitle, message)

    def reset(self, key):
        self._sent_once.discard(key)
