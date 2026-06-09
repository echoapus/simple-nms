"""Database layer — schema initialisation and batched writer thread."""

import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
    src_ip   TEXT,
    type     TEXT    NOT NULL CHECK(type IN ('syslog', 'snmptrap', 'webhook')),
    facility TEXT,
    severity TEXT,
    oid      TEXT,
    varbinds TEXT,
    payload  TEXT,
    tags     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_src_ip  ON events(src_ip);
CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity);
"""

INSERT_SQL = """
INSERT INTO events (ts, src_ip, type, facility, severity, oid, varbinds, payload, tags)
VALUES (:ts, :src_ip, :type, :facility, :severity, :oid, :varbinds, :payload, :tags)
"""


def init_db(db_path: str, wal_mode: bool = True) -> None:
    """Create database directory, tables and indexes."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    if wal_mode:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    logger.info("Database initialised: %s (WAL=%s)", db_path, wal_mode)


class DBWriter(threading.Thread):
    """Dedicated thread that drains the write queue and batch-inserts into SQLite.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file.
    write_queue : queue.Queue
        Shared queue from which event dicts are consumed.
    batch_size : int
        Max rows per INSERT transaction.
    flush_interval : float
        Seconds to wait before flushing a partial batch.
    sse_callback : callable or None
        Optional callback invoked with each successfully inserted event dict
        (used later for SSE push in Phase 2).
    """

    def __init__(
        self,
        db_path: str,
        write_queue: "queue.Queue[dict]",
        batch_size: int = 100,
        flush_interval: float = 0.2,
        sse_callback=None,
    ):
        super().__init__(daemon=True, name="db-writer")
        self.db_path = db_path
        self.q = write_queue
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.sse_callback = sse_callback
        self._stop_event = threading.Event()
        self._stats_lock = threading.Lock()
        self._started_at = None
        self._stopped_at = None
        self._last_flush_at = None
        self._last_error = None
        self._total_written = 0

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def health_snapshot(self) -> dict:
        with self._stats_lock:
            return {
                "configured": True,
                "alive": self.is_alive(),
                "stopping": self._stop_event.is_set(),
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_flush_at": self._last_flush_at,
                "last_error": self._last_error,
                "total_written": self._total_written,
                "batch_size": self.batch_size,
                "flush_interval_ms": int(self.flush_interval * 1000),
            }

    # ------------------------------------------------------------------
    def run(self) -> None:
        with self._stats_lock:
            self._started_at = self._utc_now()
            self._stopped_at = None
            self._last_error = None

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        logger.info("DBWriter started (batch=%d, flush=%.0fms)",
                     self.batch_size, self.flush_interval * 1000)

        while not self._stop_event.is_set() or not self.q.empty():
            batch: list[dict] = []
            deadline = time.monotonic() + self.flush_interval

            # During shutdown, drain immediately so queued events are not abandoned.
            while len(batch) < self.batch_size:
                try:
                    if self._stop_event.is_set():
                        item = self.q.get_nowait()
                    else:
                        timeout = max(0, deadline - time.monotonic())
                        item = self.q.get(timeout=timeout)
                    batch.append(item)
                except queue.Empty:
                    break

            if not batch:
                continue

            try:
                conn.executemany(INSERT_SQL, batch)
                conn.commit()
                with self._stats_lock:
                    self._total_written += len(batch)
                    self._last_flush_at = self._utc_now()
                    self._last_error = None
                logger.debug("Flushed %d events to DB", len(batch))
                if self.sse_callback:
                    for evt in batch:
                        try:
                            self.sse_callback(evt)
                        except Exception:
                            logger.exception("SSE callback error")
            except sqlite3.Error as exc:
                with self._stats_lock:
                    self._last_error = str(exc)
                logger.exception("DB write failed — re-queuing %d events", len(batch))
                for item in batch:
                    item.setdefault("_retries", 0)
                    item["_retries"] += 1
                    if item["_retries"] <= 5:
                        try:
                            self.q.put_nowait(item)
                        except queue.Full:
                            logger.exception("Write queue full during retry — dumping event to fallback")
                            self._dump_to_fallback(item)
                    else:
                        self._dump_to_fallback(item)
                time.sleep(min(2 ** batch[0].get("_retries", 1), 30))  # exponential back-off

        conn.close()
        with self._stats_lock:
            self._stopped_at = self._utc_now()
        logger.info("DBWriter stopped")

    def stop(self) -> None:
        self._stop_event.set()

    def _dump_to_fallback(self, event: dict) -> None:
        """Write a single event to a fallback JSONL file when DB is unreachable."""
        import json as _json
        fallback_path = os.path.join(os.path.dirname(self.db_path) or ".", "events_fallback.jsonl")
        try:
            evt = {k: v for k, v in event.items() if not k.startswith("_")}
            with open(fallback_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(evt, ensure_ascii=False) + "\n")
            logger.warning("Event dumped to fallback file: %s", fallback_path)
        except OSError:
            logger.exception("Failed to write fallback file")
