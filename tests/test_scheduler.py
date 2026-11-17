import threading

from app.scheduler import Scheduler


class CountingScaler:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first
        self.ran_twice = threading.Event()

    def run_schedule(self):
        self.calls += 1
        if self.calls >= 2:
            self.ran_twice.set()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("azure hiccup")


def test_runs_immediately_and_repeats():
    scaler = CountingScaler()
    scheduler = Scheduler(scaler, interval_seconds=0.01)
    scheduler.start()
    assert scaler.ran_twice.wait(2)
    scheduler.stop()
    calls = scaler.calls
    assert not scheduler._thread.is_alive()
    assert scaler.calls == calls  # nothing after stop


def test_keeps_running_after_a_failure():
    scaler = CountingScaler(fail_first=True)
    scheduler = Scheduler(scaler, interval_seconds=0.01)
    scheduler.start()
    assert scaler.ran_twice.wait(2)
    scheduler.stop()
