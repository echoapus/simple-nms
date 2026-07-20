#!/usr/bin/env python3
"""Minimal RFC 5425 listener check. Requires openssl.

Usage: python3 tests/test_syslog_tls.py
"""

import os
import queue
import socket
import ssl
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "simplenms"))

from collectors.syslog_listener import SyslogTLSCollector
from main import TLSCollectorManager
from test_support import check, run_suite


def test_tls_octet_counted_event():
    with tempfile.TemporaryDirectory(prefix="snms_tls_") as tmp:
        cert, key = os.path.join(tmp, "server.crt"), os.path.join(tmp, "server.key")
        result = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", key, "-out", cert,
        ], capture_output=True, text=True)
        check("test certificate created", result.returncode == 0, result.stderr)
        if result.returncode:
            return

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        events = queue.Queue()
        collector = SyslogTLSCollector(events, "127.0.0.1", port, cert, key)
        collector.start()
        check("TLS listener starts", collector.ready.wait(3) and not collector.start_error, collector.start_error or "timeout")
        if collector.start_error:
            return
        try:
            context = ssl._create_unverified_context()
            with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname="localhost") as conn:
                    message = b"<165>1 2026-07-20T12:00:00Z router app 1 ID47 - TLS syslog"
                    conn.sendall(str(len(message)).encode() + b" " + message)
            event = events.get(timeout=3)
            check("RFC 6587 frame is accepted", event["payload"] == "app[1]: ID47: TLS syslog")
            check("RFC 5424 metadata is parsed", event["facility"] == "local4" and event["severity"] == "notice")
        finally:
            collector.stop()
            collector.join(timeout=3)


def test_hot_reload_closes_clients():
    with tempfile.TemporaryDirectory(prefix="snms_tls_reload_") as tmp:
        cert, key = os.path.join(tmp, "server.crt"), os.path.join(tmp, "server.key")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", key, "-out", cert,
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ports = []
        for _ in range(2):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            ports.append(probe.getsockname()[1])
            probe.close()
        events = queue.Queue()
        manager = TLSCollectorManager(events)
        first = {"enabled": True, "host": "127.0.0.1", "port": ports[0], "certfile": cert, "keyfile": key}
        second = {**first, "port": ports[1]}
        check("initial TLS configuration applies", manager.apply(first)["applied"])
        context = ssl._create_unverified_context()
        raw = socket.create_connection(("127.0.0.1", ports[0]), timeout=3)
        client = context.wrap_socket(raw, server_hostname="localhost")
        try:
            check("TLS configuration hot-reloads", manager.apply(second)["applied"])
            client.settimeout(2)
            try:
                closed = client.recv(1) == b""
            except (OSError, ssl.SSLError):
                closed = True
            check("reload closes existing TLS clients", closed)
            with socket.create_connection(("127.0.0.1", ports[1]), timeout=3) as raw2:
                with context.wrap_socket(raw2, server_hostname="localhost") as client2:
                    message = b"<134>reloaded listener"
                    client2.sendall(str(len(message)).encode() + b" " + message)
            check("replacement listener receives events", events.get(timeout=3)["payload"] == "reloaded listener")
        finally:
            client.close()
            manager.stop()


def test_tls_newline_delimited_event():
    with tempfile.TemporaryDirectory(prefix="snms_tls_newline_") as tmp:
        cert, key = os.path.join(tmp, "server.crt"), os.path.join(tmp, "server.key")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", key, "-out", cert,
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        events = queue.Queue()
        collector = SyslogTLSCollector(events, "127.0.0.1", port, cert, key)
        collector.start()
        collector.ready.wait(3)
        try:
            context = ssl._create_unverified_context()
            with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname="localhost") as conn:
                    conn.sendall(b"<134>newline-framed TLS syslog\n")
            event = events.get(timeout=3)
            check("newline-framed TLS syslog is accepted", event["payload"] == "newline-framed TLS syslog")
        finally:
            collector.stop()
            collector.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple NMS -- RFC 5425 Validation", [
        test_tls_octet_counted_event, test_hot_reload_closes_clients, test_tls_newline_delimited_event,
    ]))
