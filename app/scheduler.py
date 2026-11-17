"""Background thread that runs the off-hours schedule inside the web process.

It replaces the CronJobs: the SQLite state file sits on a ReadWriteOnce volume that only this
pod can mount, so scheduled work has to run here too.
"""

import logging
import threading

from app.scaler import Scaler

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, scaler: Scaler, interval_seconds: int) -> None:
        self.scaler = scaler
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self) -> None:
        log.info("scheduler: checking off windows every %ss", self.interval)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        # Run immediately on start (catches up after a restart), then every interval
        while not self._stop.is_set():
            try:
                self.scaler.run_schedule()
            except Exception:
                log.exception("scheduled run failed; retrying in %ss", self.interval)
            self._stop.wait(self.interval)
