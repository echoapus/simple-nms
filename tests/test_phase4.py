#!/usr/bin/env python3
"""Phase 4 validation — reliability, cleanup, deployment files.

Usage:  python3 test_phase4.py
"""

import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

# Support running tests from any directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "simplenms"))

from database import init_db, DBWriter
from test_support import check, run_suite

DB = "/tmp/snms_test_p4.db"
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_retry_mechanism():
    print("\n=== DB Writer Write Path ===")
    if os.path.exists(DB):
        os.remove(DB)
    init_db(DB, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(DB, wq, batch_size=10, flush_interval=0.1)
    w.start()

    # Normal write should work
    wq.put({"ts": "2026-04-13T10:00:00", "src_ip": "10.0.0.1", "type": "syslog",
            "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
            "payload": "retry test", "tags": None})
    time.sleep(0.5)

    conn = sqlite3.connect(DB)
    cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("Normal write succeeds", cnt == 1)
    conn.close()

    w.stop()
    w.join(timeout=2)


def test_shutdown_drain():
    print("\n=== DB Writer Shutdown Drain ===")
    db_drain = "/tmp/snms_test_shutdown_drain.db"
    if os.path.exists(db_drain):
        os.remove(db_drain)
    init_db(db_drain, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(db_drain, wq, batch_size=100, flush_interval=5.0)
    w.start()

    for i in range(25):
        wq.put({"ts": f"2026-04-13T10:00:{i:02d}", "src_ip": "10.0.0.1",
                "type": "syslog", "facility": "local0", "severity": "info",
                "oid": None, "varbinds": None, "payload": f"drain test {i}", "tags": None})

    w.stop()
    w.join(timeout=3)
    check("Writer stopped after drain", not w.is_alive())

    conn = sqlite3.connect(db_drain)
    cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("Queued events flushed on stop", cnt == 25, f"got {cnt}")
    conn.close()
    os.remove(db_drain)


def test_cleanup_script():
    print("\n=== Cleanup Script ===")
    db_clean = "/tmp/snms_test_cleanup.db"
    if os.path.exists(db_clean):
        os.remove(db_clean)
    init_db(db_clean, wal_mode=True)

    # Seed events: some old, some recent
    conn = sqlite3.connect(db_clean)
    conn.execute("INSERT INTO events (ts, src_ip, type, payload) VALUES "
                 "('2025-01-01T00:00:00', '10.0.0.1', 'syslog', 'old event 1')")
    conn.execute("INSERT INTO events (ts, src_ip, type, payload) VALUES "
                 "('2025-01-02T00:00:00', '10.0.0.2', 'syslog', 'old event 2')")
    conn.execute("INSERT INTO events (ts, src_ip, type, payload) VALUES "
                 "(strftime('%Y-%m-%dT%H:%M:%S','now'), '10.0.0.3', 'syslog', 'recent event')")
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("Seeded 3 events", total == 3)
    conn.close()

    # Dry run
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT, "cleanup.py"), "--db", db_clean, "--days", "30", "--dry-run"],
        capture_output=True, text=True)
    check("Dry run exits 0", result.returncode == 0)
    check("Dry run reports 2 events", "2" in result.stdout and "DRY RUN" in result.stdout,
          f"output: {result.stdout.strip()}")

    # Actual cleanup
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT, "cleanup.py"), "--db", db_clean, "--days", "30"],
        capture_output=True, text=True)
    check("Cleanup exits 0", result.returncode == 0)
    check("Cleanup reports deletion", "Deleted 2" in result.stdout,
          f"output: {result.stdout.strip()}")

    conn = sqlite3.connect(db_clean)
    remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("1 event remains after cleanup", remaining == 1)
    payload = conn.execute("SELECT payload FROM events").fetchone()[0]
    check("Remaining event is the recent one", payload == "recent event")
    conn.close()
    os.remove(db_clean)


def test_deployment_files():
    print("\n=== Deployment Files ===")

    # systemd service
    svc = os.path.join(PROJECT, "deploy", "simple-nms.service")
    check("systemd service file exists", os.path.exists(svc))
    if os.path.exists(svc):
        content = open(svc).read()
        check("Service: AmbientCapabilities for port binding",
              "CAP_NET_BIND_SERVICE" in content)
        check("Service: runs as simplenms user", "User=simplenms" in content)
        check("Service: ReadWritePaths for data dir", "ReadWritePaths" in content)
        check("Service: restart on failure", "Restart=on-failure" in content)

    # Dockerfile
    df = os.path.join(PROJECT, "Dockerfile")
    check("Dockerfile exists", os.path.exists(df))
    if os.path.exists(df):
        content = open(df).read()
        check("Dockerfile: python base image", "python:" in content)
        check("Dockerfile: EXPOSE 80", "EXPOSE 80" in content)
        check("Dockerfile: HEALTHCHECK", "HEALTHCHECK" in content)
        check("Dockerfile: non-root user", "USER simplenms" in content)
        check("Dockerfile: copies metrics module", "metrics.py" in content)

    # docker-compose.yml
    dc = os.path.join(PROJECT, "docker-compose.yml")
    check("docker-compose.yml exists", os.path.exists(dc))
    if os.path.exists(dc):
        content = open(dc).read()
        check("Compose: port 80 mapping", '"80:80"' in content)
        check("Compose: syslog UDP mapping", "514:514/udp" in content)
        check("Compose: SNMP trap UDP mapping", "162:162/udp" in content)
        check("Compose: NET_BIND_SERVICE capability", "NET_BIND_SERVICE" in content)
        check("Compose: volume mount", "nms-data" in content)


def test_documentation():
    print("\n=== Documentation ===")

    for fname, checks in [
        ("README.md", ["Syslog", "SNMP Trap", "Webhook", "SQLite", "Quick Start", "README.zh-TW.md"]),
        ("README.zh-TW.md", ["簡易網路管理系統", "系統架構", "資料庫結構"]),
        ("INSTALL.md", ["pip install", "systemd", "config.json", "CAP_NET_BIND_SERVICE", "Docker"]),
        ("USER.md", ["logger", "snmptrap", "curl", "/api/events", "/api/kpi", "/api/sse"]),
    ]:
        fpath = os.path.join(PROJECT, fname)
        check(f"{fname} exists", os.path.exists(fpath))
        if os.path.exists(fpath):
            content = open(fpath).read()
            for keyword in checks:
                check(f"{fname} mentions '{keyword}'", keyword in content)


def test_config_port80():
    print("\n=== Config Default Port ===")
    cfg_path = os.path.join(PROJECT, "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    check("Webhook port defaults to 80", cfg["webhook"]["port"] == 80)
    check("Syslog port is 514", cfg["syslog"]["port"] == 514)
    check("SNMP trap port is 162", cfg["snmptrap"]["port"] == 162)


def test_snmp_community_update_script():
    print("\n=== SNMP Community Update Script ===")
    check_script = os.path.join(PROJECT, "scripts", "check_community_update.py")
    check("Community check script exists", os.path.exists(check_script))
    check("snmptrap CLI available", shutil.which("snmptrap") is not None,
          "install net-snmp-utils/snmp")
    if not os.path.exists(check_script) or not shutil.which("snmptrap"):
        return

    ports = []
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
        finally:
            sock.close()
    web_port, trap_port = ports

    test_dir = tempfile.mkdtemp(prefix="snms_community_check_")
    cfg_path = os.path.join(test_dir, "config.json")
    with open(os.path.join(PROJECT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["database"]["path"] = os.path.join(test_dir, "events.db")
    cfg["writer"] = {"batch_size": 10, "flush_interval_ms": 100}
    cfg["syslog"]["enabled"] = False
    cfg["snmptrap"].update({
        "enabled": True,
        "host": "127.0.0.1",
        "port": trap_port,
        "community": "initial-check",
        "mib_dirs": [],
    })
    cfg["webhook"].update({"enabled": True, "host": "127.0.0.1", "port": web_port})
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    proc = subprocess.Popen(
        [sys.executable, "main.py", cfg_path],
        cwd=os.path.join(PROJECT, "src", "simplenms"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{web_port}"
        deadline = time.time() + 10
        ready = False
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{base_url}/health", timeout=1).read()
                ready = True
                break
            except Exception:
                time.sleep(0.2)
        check("Temporary Simple NMS starts", ready)
        if not ready:
            return

        result = subprocess.run(
            [
                sys.executable, check_script,
                "--base-url", base_url,
                "--trap-host", "127.0.0.1",
                "--trap-port", str(trap_port),
                "--restore",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        check("Community update check passes", result.returncode == 0,
              (result.stdout + result.stderr).strip())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple NMS -- Phase 4 Validation Suite", [
        test_retry_mechanism,
        test_shutdown_drain,
        test_cleanup_script,
        test_deployment_files,
        test_documentation,
        test_config_port80,
        test_snmp_community_update_script,
    ]))
