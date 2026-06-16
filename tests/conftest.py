class FakeClock:
    """Deterministic clock for engine tests. Call like time.time()."""
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t
