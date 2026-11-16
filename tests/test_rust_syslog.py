#!/usr/bin/env python3
"""Rust UDP Syslog collector handoff check. Usage: python3 tests/test_rust_syslog.py"""

import os
import queue
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "simplenms"))

from collectors.rust_syslog import RustSyslogCollector
from test_support import check, run_suite


PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(PROJECT, "rust-syslog", "target", "release", "simplenms-syslog")


def test_udp_handoff():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with tempfile.TemporaryDirectory(prefix="snms_rust_syslog_") as runtime_dir:
        events = queue.Queue()
        collector = RustSyslogCollector(events, "127.0.0.1", port, runtime_dir=runtime_dir, binary=BINARY)
        collector.start()
        check("Rust collector starts", collector.is_alive(), collector.start_error or "not running")
        if not collector.is_alive():
            collector.stop()
            return
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b'<165>1 2026-09-10T16:00:00+08:00 host app 1 ID47 [meta key="value"] test', ("127.0.0.1", port))
            sender.close()
            event = events.get(timeout=3)
            check("RFC 5424 event reaches Python writer queue", event["payload"] == "app[1]: ID47: test")
            check("Rust normalizes timestamp and metadata", event["ts"] == "2026-09-10T08:00:00.000000" and event["varbinds"] == '{"meta":{"key":"value"}}')
            collector.update_deny_ips(["127.0.0.1"])
            time.sleep(0.2)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"<134>blocked", ("127.0.0.1", port))
            sender.close()
            time.sleep(0.2)
            check("Rust denylist update drops new events", events.empty())
        finally:
            collector.stop()


def test_start_fails_when_udp_port_is_busy():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="snms_rust_syslog_") as runtime_dir:
        collector = RustSyslogCollector(queue.Queue(), "127.0.0.1", port, runtime_dir=runtime_dir, binary=BINARY)
        try:
            collector.start()
            check("Busy UDP port fails Rust collector startup", collector.start_error is not None and not collector.is_alive())
        finally:
            collector.stop()
            probe.close()


if __name__ == "__main__":
    subprocess.run(["cargo", "build", "--release", "--manifest-path", os.path.join(PROJECT, "rust-syslog", "Cargo.toml")], check=True)
    try:
        raise SystemExit(run_suite("Simple NMS -- Rust Syslog Collector", [test_udp_handoff, test_start_fails_when_udp_port_is_busy]))
    finally:
        subprocess.run(["cargo", "clean", "--manifest-path", os.path.join(PROJECT, "rust-syslog", "Cargo.toml")], check=False)
