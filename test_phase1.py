#!/usr/bin/env python3
"""Phase 1 validation — unit + integration tests for all collectors and DB writer.

Usage:  python3 test_phase1.py
No network listeners required — uses Flask test client and direct function calls.
"""

import json
import os
import queue
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, DBWriter
from collectors.syslog_listener import _parse_syslog
from web_app import create_app

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  -- {detail}")


def test_syslog_parser():
    print("\n=== Syslog Parser ===")
    e = _parse_syslog(b"<134>router01 BGP: peer 10.0.0.1 down", ("10.0.0.1", 514))
    check("PRI 134 -> facility=local0", e["facility"] == "local0")
    check("PRI 134 -> severity=info", e["severity"] == "info")
    check("src_ip captured", e["src_ip"] == "10.0.0.1")
    check("type=syslog", e["type"] == "syslog")
    check("payload stripped of PRI", "router01 BGP:" in e["payload"])

    e2 = _parse_syslog(b"<131>auth fail", ("192.168.1.1", 514))
    check("PRI 131 -> severity=err", e2["severity"] == "err")

    e3 = _parse_syslog(b"<165>fw01 DROP", ("10.0.0.254", 514))
    check("PRI 165 -> facility=local4, severity=notice",
          e3["facility"] == "local4" and e3["severity"] == "notice")

    e4 = _parse_syslog(b"no PRI field here", ("172.16.0.1", 514))
    check("No PRI -> facility=None", e4["facility"] is None)
    check("No PRI -> severity=None", e4["severity"] is None)
    check("No PRI -> full message as payload", e4["payload"] == "no PRI field here")

    # RFC 5424 Syslog test case
    msg_5424 = b'<165>1 2003-10-11T22:14:15.003Z mymachine.example.com evtsys 1234 ID47 [exampleSDID@32473 iut="3" eventSource="Application" eventID="1011"] An application event log entry...'
    e5 = _parse_syslog(msg_5424, ("10.0.0.5", 514))
    check("RFC 5424: facility parsed", e5["facility"] == "local4")
    check("RFC 5424: severity parsed", e5["severity"] == "notice")
    check("RFC 5424: ts parsed from header", e5["ts"].startswith("2003-10-11T22:14:15"))
    check("RFC 5424: tags populated", e5["tags"] == "app:evtsys,msgid:ID47,host:mymachine.example.com")
    check("RFC 5424: varbinds parsed JSON", '"exampleSDID@32473"' in e5["varbinds"] and '"iut": "3"' in e5["varbinds"])
    check("RFC 5424: payload parsed with prefix", e5["payload"] == "evtsys[1234]: ID47: An application event log entry...")



def test_webhook():
    print("\n=== Webhook Collector ===")
    wq = queue.Queue()
    app = create_app(db_path="/tmp/snms_test_webhook_unused.db", write_queue=wq)
    c = app.test_client()

    r = c.post("/webhook",
               data=json.dumps({"event": "deploy", "severity": "info", "message": "v2.3.1"}),
               headers={"Content-Type": "application/json"})
    check("Valid webhook -> 202", r.status_code == 202)
    check("Event queued", wq.qsize() == 1)
    evt = wq.get()
    check("type=webhook", evt["type"] == "webhook")
    check("severity from body", evt["severity"] == "info")
    check("payload has JSON", "deploy" in evt["payload"])

    r = c.post("/webhook", data=b"not json",
               headers={"Content-Type": "application/json"})
    check("Invalid JSON -> 400", r.status_code == 400)

    r = c.get("/health")
    check("/health -> 200", r.status_code == 200)

    r = c.post("/webhook",
               data=json.dumps({"event": "test", "tags": "ops,critical"}),
               headers={"Content-Type": "application/json"})
    evt = wq.get()
    check("tags passthrough", evt["tags"] == "ops,critical")

    full_q = queue.Queue(maxsize=1)
    full_q.put({"already": "queued"})
    app = create_app(db_path="/tmp/snms_test_webhook_full_unused.db",
                     write_queue=full_q)
    c = app.test_client()
    r = c.post("/webhook",
               data=json.dumps({"event": "overflow"}),
               headers={"Content-Type": "application/json"})
    check("Full queue -> 503", r.status_code == 503)
    check("Full queue not mutated", full_q.qsize() == 1)


def test_snmp_module():
    print("\n=== SNMP Trap Collector (module load) ===")
    from collectors.snmp_listener import _PYSNMP_OK, SNMPTrapCollector
    check("pysnmp importable", _PYSNMP_OK)
    tc = SNMPTrapCollector(queue.Queue(), host="127.0.0.1", port=1162)
    check("SNMPTrapCollector instantiated", tc is not None)


def test_db_writer():
    print("\n=== DB Writer ===")
    db = "/tmp/snms_test_writer.db"
    if os.path.exists(db):
        os.remove(db)
    init_db(db, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(db, wq, batch_size=10, flush_interval=0.1)
    w.start()

    events = [
        {"ts": "2026-04-13T09:00:00.000", "src_ip": "10.0.0.1", "type": "syslog",
         "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
         "payload": "BGP peer down", "tags": None},
        {"ts": "2026-04-13T09:00:01.000", "src_ip": "10.0.0.2", "type": "snmptrap",
         "facility": None, "severity": None, "oid": "1.3.6.1.4.1.99999",
         "varbinds": json.dumps({"1.3.6.1.4.1.99999.1": "linkDown"}),
         "payload": None, "tags": None},
        {"ts": "2026-04-13T09:00:02.000", "src_ip": "127.0.0.1", "type": "webhook",
         "facility": None, "severity": "warning", "oid": None, "varbinds": None,
         "payload": json.dumps({"event": "alert"}), "tags": "monitoring"},
    ]
    for e in events:
        wq.put(e)
    time.sleep(0.5)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    check("3 events inserted", len(rows) == 3)
    check("Row 1: syslog", dict(rows[0])["type"] == "syslog")
    check("Row 2: snmptrap with OID", dict(rows[1])["oid"] == "1.3.6.1.4.1.99999")
    check("Row 3: webhook with tags", dict(rows[2])["tags"] == "monitoring")

    wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL mode active", wal == "wal")

    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'").fetchall()]
    check("4 indexes created", len(idx) == 4, f"found {idx}")

    w.stop()
    w.join(timeout=2)
    conn.close()


def test_performance():
    print("\n=== Performance Benchmark ===")
    db = "/tmp/snms_test_perf.db"
    if os.path.exists(db):
        os.remove(db)
    init_db(db, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(db, wq, batch_size=100, flush_interval=0.2)
    w.start()

    N = 2000
    t0 = time.monotonic()
    for i in range(N):
        wq.put({"ts": f"2026-04-13T09:00:{i:06d}", "src_ip": f"10.0.{i // 256}.{i % 256}",
                 "type": "syslog", "facility": "local0", "severity": "info",
                 "oid": None, "varbinds": None, "payload": f"bench event {i}", "tags": None})

    while not wq.empty():
        time.sleep(0.05)
    time.sleep(0.3)
    elapsed = time.monotonic() - t0

    conn = sqlite3.connect(db)
    cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    eps = cnt / elapsed if elapsed > 0 else 0
    check(f"{cnt} events in {elapsed:.2f}s = {eps:.0f} evt/s (>= 500)", eps >= 500)
    print(f"         Throughput: {eps:,.0f} events/sec")

    w.stop()
    w.join(timeout=2)
    conn.close()


def test_end_to_end():
    print("\n=== End-to-End: Syslog + Webhook -> Queue -> DB ===")
    db = "/tmp/snms_test_e2e.db"
    if os.path.exists(db):
        os.remove(db)
    init_db(db, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(db, wq, batch_size=10, flush_interval=0.1)
    w.start()

    for raw, addr in [
        (b"<134>router01 BGP: peer down", ("10.0.0.1", 514)),
        (b"<131>switch01 auth fail", ("192.168.1.1", 514)),
    ]:
        wq.put(_parse_syslog(raw, addr))

    app = create_app(db_path=db, write_queue=wq)
    c = app.test_client()
    for p in [{"event": "deploy", "severity": "info"}, {"event": "alert", "severity": "crit"}]:
        c.post("/webhook", data=json.dumps(p),
               headers={"Content-Type": "application/json"})

    time.sleep(0.5)
    conn = sqlite3.connect(db)
    types = dict(conn.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall())
    check("2 syslog events", types.get("syslog") == 2)
    check("2 webhook events", types.get("webhook") == 2)
    check("4 total events", sum(types.values()) == 4)

    w.stop()
    w.join(timeout=2)
    conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("Simple NMS -- Phase 1 Validation Suite")
    print("=" * 60)

    test_syslog_parser()
    test_webhook()
    test_snmp_module()
    test_db_writer()
    test_performance()
    test_end_to_end()

    print("\n" + "=" * 60)
    print(f"Results:  {PASS} passed,  {FAIL} failed")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
