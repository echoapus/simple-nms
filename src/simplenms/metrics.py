"""Process-local runtime metrics for Simple NMS."""

import threading


class RuntimeMetrics:
    """Thread-safe counters for collector and writer health reporting."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dropped = dict.fromkeys(("syslog", "snmptrap", "webhook", "sse"), 0)

    def inc_dropped(self, event_type: str, count: int = 1) -> None:
        with self._lock:
            self._dropped[event_type] = self._dropped.get(event_type, 0) + count

    def snapshot(self) -> dict:
        with self._lock:
            return {"dropped_events": dict(self._dropped)}


runtime_metrics = RuntimeMetrics()
