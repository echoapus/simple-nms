"""UDP Syslog collector selection without starting network listeners."""

import json
import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "sieco"))

from collectors.rust_syslog import RustSyslogCollector
from collectors.syslog_factory import build_syslog_collector
from collectors.syslog_listener import SyslogCollector
from metrics import runtime_metrics


class SyslogCollectorChoiceTest(unittest.TestCase):
    def test_rust_and_python_choices(self):
        events = queue.Queue()
        self.assertIsInstance(build_syslog_collector("rust", events, "127.0.0.1", 514, [], "/tmp"), RustSyslogCollector)
        self.assertIsInstance(build_syslog_collector("python", events, "127.0.0.1", 514, [], "/tmp"), SyslogCollector)
        with self.assertRaises(ValueError):
            build_syslog_collector("other", events, "127.0.0.1", 514, [], "/tmp")

    def test_rust_bridge_reports_its_drops(self):
        events = queue.Queue()
        collector = RustSyslogCollector(events, "127.0.0.1", 514, runtime_dir="/tmp")
        before = runtime_metrics.snapshot()["dropped_events"]["syslog"]
        collector._enqueue(json.dumps({
            "ts": "2026-01-01T00:00:00.000000", "src_ip": "127.0.0.1", "type": "syslog",
            "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
            "payload": "test", "tags": None, "_bridge_dropped": 3,
        }))
        self.assertEqual(events.get_nowait()["payload"], "test")
        self.assertEqual(runtime_metrics.snapshot()["dropped_events"]["syslog"] - before, 3)


if __name__ == "__main__":
    unittest.main()
