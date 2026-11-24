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
    init_db(DB)

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
    init_db(db_drain)

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

    installer = open(os.path.join(PROJECT, "scripts", "install.sh")).read()
    check("Installer: prompts for SNMP community", 'read -r -p "SNMP Trap community' in installer)
    check("Installer: prompts for Syslog collector", 'read -r -p "UDP Syslog collector' in installer)
    check("Installer: cleans Rust build artifacts", 'cargo clean --manifest-path' in installer)
    check("Installer: offers to remove Rust build packages", 'read -r -p "Remove Rust build packages' in installer)
    check("Installer: passes community safely", "os.environ['SNMP_COMMUNITY']" in installer)

    prompt = installer.split('echo "Configuring web portal port..."', 1)[1].split('# Configure SNMP Trap community', 1)[0]
    writer = installer.split('SYSLOG_COLLECTOR="$SYSLOG_COLLECTOR" python3 -c "\n', 1)[1].split('\n"', 1)[0]
    with tempfile.TemporaryDirectory() as directory:
        config_path = os.path.join(directory, "config.json")
        for saved, entered, expected in [(None, "\n", 80), (8080, "\n", 8080), (80, "00808\n", 808), (80, "65535\n", 65535), (80, "0\n", None), (80, "65536\n", None), (80, "abc\n", None), (80, "999999999999\n", None)]:
            original = {"webhook": {"host": "127.0.0.1"}, "snmptrap": {"community": "saved"}}
            if saved is not None:
                original["webhook"]["port"] = saved
            with open(config_path, "w") as config:
                json.dump(original, config)
            script = 'export CFG_FILE=' + repr(config_path) + '\n' + prompt
            script += '\nexport WEB_PORT SNMP_COMMUNITY=saved SYSLOG_COLLECTOR=python\n'
            script += 'python3 - <<\'PY\'\n' + writer.replace('/opt/simple-nms/config.json', config_path) + '\nPY\n'
            result = subprocess.run(["bash", "-ec", script], input=entered, text=True, capture_output=True)
            with open(config_path) as config:
                updated = json.load(config)
            check(f"Installer: web port {entered.strip()!r}, saved {saved}",
                  (result.returncode != 0 and updated == original) if expected is None else
                  (result.returncode == 0 and updated["webhook"] == {"host": "127.0.0.1", "port": expected}))

        selector = installer.split('# Match the service\'s data-directory configuration override.', 1)[1].split('echo "Configuring UDP Syslog collector..."', 1)[0]
        data_dir = os.path.join(directory, "custom-data")
        os.makedirs(data_dir)
        overlay_path = os.path.join(data_dir, "config.json")
        for database_path in ("custom-data/events.db", os.path.join(data_dir, "events.db")):
            base = {"database": {"path": database_path}, "webhook": {"port": 80}}
            for overlay in (None, {"webhook": {"port": 8080, "host": "127.0.0.1"}, "retention": {"days": 42}}):
                with open(config_path, "w") as config:
                    json.dump(base, config)
                if overlay is not None:
                    with open(overlay_path, "w") as config:
                        json.dump(overlay, config)
                elif os.path.exists(overlay_path):
                    os.remove(overlay_path)
                script = selector.replace('/opt/simple-nms', directory) + prompt
                script += '\nexport WEB_PORT SNMP_COMMUNITY=saved SYSLOG_COLLECTOR=python\n'
                script += 'python3 - <<\'PY\'\n' + writer + '\nPY\n'
                result = subprocess.run(["bash", "-ec", script], input="\n", text=True, capture_output=True)
                active_path = overlay_path if overlay is not None else config_path
                with open(active_path) as config:
                    updated = json.load(config)
                check(f"Installer: active port preserved for {database_path}, overlay={overlay is not None}",
                      result.returncode == 0 and updated["webhook"]["port"] == (8080 if overlay else 80))
                if overlay is not None:
                    with open(config_path) as config:
                        check("Installer: base config preserved", json.load(config) == base)
                    check("Installer: unrelated overlay settings preserved", updated["retention"] == overlay["retention"])
                    result = subprocess.run(["bash", "-ec", script], input="9090\n", text=True, capture_output=True)
                    with open(overlay_path) as config:
                        updated = json.load(config)
                    check("Installer: selected port written to overlay", result.returncode == 0 and updated["webhook"]["port"] == 9090)

    with tempfile.TemporaryDirectory() as directory:
        custom_dir = os.path.join(directory, 'custom data%')
        config_path = os.path.join(directory, 'config.json')
        overlay_path = os.path.join(directory, 'overlay.json')
        with open(config_path, 'w') as config:
            json.dump({"database": {"path": os.path.join(custom_dir, "events.db")}}, config)
        with open(overlay_path, 'w') as config:
            json.dump({"webhook": {"port": 8080}}, config)
        resolver = installer.split('# Resolve the effective database path,', 1)[1].split('echo "Configuring UDP Syslog collector..."', 1)[0]
        resolver = resolver[resolver.index('DATABASE_PATH='):].replace('/opt/simple-nms/config.json', config_path)
        result = subprocess.run(['bash', '-ec', resolver + '\nprintf "%s" "$DATA_DIR"'],
                                env={**os.environ, 'CFG_FILE': overlay_path}, capture_output=True, text=True)
        check("Installer: custom database directory resolved through overlay",
              result.returncode == 0 and result.stdout == custom_dir and os.path.isdir(os.path.join(custom_dir, 'mibs')))
        database_path = os.path.join(custom_dir, 'events.db')
        paths = [database_path, database_path + '-wal', database_path + '-shm', os.path.join(custom_dir, 'config.json')]
        for path in paths:
            with open(path, 'w') as file:
                file.write('existing data')
        permissions = installer.split('# Grant access to custom database storage without changing unrelated files.', 1)[1].split('# Detect port conflicts', 1)[0]
        result = subprocess.run(['bash', '-ec', 'chown() { printf "%s\\n" "$@"; }\n' + permissions],
                                env={**os.environ, 'DATA_DIR': custom_dir, 'DATABASE_PATH': database_path, 'CFG_FILE': paths[-1]}, capture_output=True, text=True)
        check("Installer: custom database, sidecars and overlay get writable permissions",
              result.returncode == 0 and all(os.stat(path).st_mode & 0o777 == 0o660 and path in result.stdout for path in paths)
              and os.stat(custom_dir).st_mode & 0o777 == 0o770)
        unit_path = os.path.join(directory, 'service')
        grant = installer.split('# systemd accepts C-style quoted paths;', 1)[1].split('chown root:root', 1)[0]
        grant = grant[grant.index('python3 -c'):].replace('/etc/systemd/system/simple-nms.service', unit_path)
        result = subprocess.run(['bash', '-ec', grant], env={**os.environ, 'DATA_DIR': custom_dir}, capture_output=True, text=True)
        with open(unit_path) as unit:
            check("Installer: systemd grants custom path with spaces and literal percent",
                  result.returncode == 0 and 'ReadWritePaths=' + json.dumps(custom_dir.replace('%', '%%')) in unit.read())

        binary_path = os.path.join(directory, 'sleep')
        shutil.copyfile(shutil.which('sleep'), binary_path)
        os.chmod(binary_path, 0o750)
        process = subprocess.Popen(['sleep', '30'], executable=binary_path)
        try:
            replacement = installer.split('        install -m 750 ', 1)[1].split('        cargo clean', 1)[0]
            replacement = 'install -m 750 ' + replacement
            replacement = replacement.replace('"$PROJECT_ROOT"/rust-syslog/target/release/simplenms-syslog', shutil.which('sleep')).replace('/opt/simple-nms/simplenms-syslog', binary_path)
            result = subprocess.run(['bash', '-ec', replacement], capture_output=True, text=True)
            check("Installer: replaces running collector and preserves executable permissions",
                  result.returncode == 0 and process.poll() is None and os.stat(binary_path).st_mode & 0o777 == 0o750)
        finally:
            process.terminate()
            process.wait()

    startup = installer.split('echo "Starting simple-nms..."', 1)[1].split('# Retrieve primary local IP address', 1)[0]
    result = subprocess.run(["bash", "-ec", 'systemctl() { echo "$*"; }\n' + startup], capture_output=True, text=True)
    check("Installer: enables and restarts service", result.returncode == 0 and result.stdout.splitlines() == ["daemon-reload", "enable simple-nms", "restart simple-nms"])


def test_documentation():
    print("\n=== Documentation ===")

    for fname, checks in [
        ("README.md", ["Syslog", "SNMP Trap", "Webhook", "SQLite", "Quick Start", "README.zh-TW.md"]),
        ("README.zh-TW.md", ["簡易網路管理系統", "系統架構", "資料庫結構"]),
        ("docs/INSTALL.md", ["pip install", "systemd", "config.example.json", "CAP_NET_BIND_SERVICE"]),
        ("docs/USER.md", ["logger", "snmptrap", "curl", "/api/events", "/api/kpi", "/api/sse"]),
    ]:
        fpath = os.path.join(PROJECT, fname)
        check(f"{fname} exists", os.path.exists(fpath))
        if os.path.exists(fpath):
            content = open(fpath).read()
            for keyword in checks:
                check(f"{fname} mentions '{keyword}'", keyword in content)


def test_config_port80():
    print("\n=== Config Default Port ===")
    cfg_path = os.path.join(PROJECT, "config.example.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    check("Webhook port defaults to 80", cfg["webhook"]["port"] == 80)
    check("Syslog port is 514", cfg["syslog"]["port"] == 514)
    check("Syslog collector defaults to Rust", cfg["syslog"]["collector"] == "rust")
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
    with open(os.path.join(PROJECT, "config.example.json"), encoding="utf-8") as f:
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
        test_deployment_files,
        test_documentation,
        test_config_port80,
        test_snmp_community_update_script,
    ]))
