"""Persistent state (saved node counts, leases, namespace owners, schedule progress, audit log) in a SQLite file.

The file lives on a PersistentVolume mounted only by the kube-siesta pod, which is the single
writer: the web UI and the in-process scheduler share this one State.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodepool_counts (  -- size to restore when an off window ends
    name  TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (           -- namespaces woken during off hours
    namespace TEXT PRIMARY KEY,
    until     TEXT NOT NULL,                  -- ISO timestamp
    by        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS namespace_owners (
    namespace  TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule (         -- start of the last off window scaled down / up
    key   TEXT PRIMARY KEY,                   -- 'down' | 'up'
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        path = Path(settings.state_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by request threads and the scheduler thread, serialised by a lock
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _query(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- node pool sizes ----------------------------------------------------

    def pool_counts(self) -> dict[str, int]:
        return {r["name"]: r["count"] for r in self._query("SELECT name, count FROM nodepool_counts")}

    def save_pool_count(self, name: str, count: int, overwrite: bool) -> None:
        sql = ("INSERT INTO nodepool_counts (name, count) VALUES (?, ?) "
               + ("ON CONFLICT(name) DO UPDATE SET count = excluded.count" if overwrite
                  else "ON CONFLICT(name) DO NOTHING"))
        with self._tx() as db:
            db.execute(sql, (name, count))

    # --- leases -------------------------------------------------------------

    def leases(self) -> dict[str, dict]:
        return {r["namespace"]: {"until": r["until"], "by": r["by"]}
                for r in self._query("SELECT namespace, until, by FROM leases")}

    def active_leases(self, now: datetime | None = None) -> dict[str, dict]:
        now = now or self.settings.now()
        return {ns: l for ns, l in self.leases().items() if datetime.fromisoformat(l["until"]) > now}

    def set_lease(self, namespace: str, until: str, by: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO leases (namespace, until, by) VALUES (?, ?, ?) "
                       "ON CONFLICT(namespace) DO UPDATE SET until = excluded.until, by = excluded.by",
                       (namespace, until, by))

    def delete_lease(self, namespace: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM leases WHERE namespace = ?", (namespace,))

    def clear_leases(self) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM leases")

    # --- namespace owners ---------------------------------------------------

    def owners(self) -> dict[str, str]:
        return {r["namespace"]: r["owner"] for r in self._query("SELECT namespace, owner FROM namespace_owners")}

    def set_owner(self, namespace: str, owner: str, by: str) -> None:
        """Store the owner; an empty owner clears it."""
        with self._tx() as db:
            if not owner:
                db.execute("DELETE FROM namespace_owners WHERE namespace = ?", (namespace,))
                return
            db.execute("INSERT INTO namespace_owners (namespace, owner, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(namespace) DO UPDATE SET owner = excluded.owner, "
                       "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                       (namespace, owner, self.settings.now().isoformat(timespec="seconds"), by))

    # --- schedule progress --------------------------------------------------

    def schedule_marks(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._query("SELECT key, value FROM schedule")}

    def set_schedule_mark(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO schedule (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- audit log ----------------------------------------------------------

    def audit(self, actor: str, action: str, target: str, detail: str = "") -> None:
        with self._tx() as db:
            db.execute("INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                       (self.settings.now().isoformat(timespec="seconds"), actor, action, target, detail))

    def recent_audit(self, limit: int = 50) -> list[dict]:
        """Newest first."""
        return [dict(r) for r in self._query(
            "SELECT at, actor, action, target, detail FROM audit ORDER BY id DESC LIMIT ?", limit)]
