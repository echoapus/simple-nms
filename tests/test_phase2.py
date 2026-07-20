#!/usr/bin/env python3
"""Phase 2 validation — REST API (/api/events, /api/kpi) and SSE hub.

Usage:  python3 test_phase2.py
"""

import json
import os
import sys
import queue
import time

# Support running tests from any directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "simplenms"))

from database import init_db, DBWriter
from test_support import check, run_suite
from web_app import create_app, SSEHub, sse_hub

DB = "/tmp/snms_test_p2.db"


def seed_db():
    """Seed database with test events and return (write_queue, db_writer, app, client)."""
    if os.path.exists(DB):
        os.remove(DB)
    init_db(DB, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(DB, wq, batch_size=50, flush_interval=0.1)
    w.start()

    # Seed diverse events
    events = [
        # Syslog events
        {"ts": "2026-04-13T08:00:00.000", "src_ip": "10.0.0.1", "type": "syslog",
         "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
         "payload": "BGP peer 10.0.0.2 established", "tags": None},
        {"ts": "2026-04-13T08:05:00.000", "src_ip": "10.0.0.1", "type": "syslog",
         "facility": "local0", "severity": "err", "oid": None, "varbinds": None,
         "payload": "BGP peer 10.0.0.3 down", "tags": None},
        {"ts": "2026-04-13T09:00:00.000", "src_ip": "192.168.1.1", "type": "syslog",
         "facility": "auth", "severity": "warning", "oid": None, "varbinds": None,
         "payload": "Failed SSH login from 1.2.3.4", "tags": None},
        # SNMP trap events
        {"ts": "2026-04-13T08:30:00.000", "src_ip": "10.0.0.2", "type": "snmptrap",
         "facility": None, "severity": None, "oid": "1.3.6.1.6.3.1.1.5.3",
         "varbinds": json.dumps({"1.3.6.1.2.1.2.2.1.1": "3"}),
         "payload": None, "tags": None},
        {"ts": "2026-04-13T09:10:00.000", "src_ip": "10.0.0.3", "type": "snmptrap",
         "facility": None, "severity": None, "oid": "1.3.6.1.6.3.1.1.5.4",
         "varbinds": json.dumps({"1.3.6.1.2.1.2.2.1.1": "5"}),
         "payload": None, "tags": None},
        # Webhook events
        {"ts": "2026-04-13T08:45:00.000", "src_ip": "127.0.0.1", "type": "webhook",
         "facility": None, "severity": "info", "oid": None, "varbinds": None,
         "payload": json.dumps({"event": "deploy", "service": "api", "message": "v2.3.1"}),
         "tags": "ops"},
        {"ts": "2026-04-13T09:30:00.000", "src_ip": "127.0.0.1", "type": "webhook",
         "facility": None, "severity": "crit", "oid": None, "varbinds": None,
         "payload": json.dumps({"event": "alert", "service": "db", "message": "disk full"}),
         "tags": "ops,critical"},
    ]
    for e in events:
        wq.put(e)

    time.sleep(0.5)

    app = create_app(db_path=DB, write_queue=wq, db_writer=w)
    client = app.test_client()
    return wq, w, app, client


def test_api_kpi():
    print("\n=== GET /api/kpi ===")
    _, w, _, c = seed_db()

    r = c.get("/api/kpi")
    check("HTTP 200", r.status_code == 200)
    d = r.get_json()
    check("total=7", d["total"] == 7, f"got {d.get('total')}")
    check("syslog=3", d["syslog"] == 3, f"got {d.get('syslog')}")
    check("snmptrap=2", d["snmptrap"] == 2, f"got {d.get('snmptrap')}")
    check("webhook=2", d["webhook"] == 2, f"got {d.get('webhook')}")

    # KPI with time range
    r = c.get("/api/kpi?time_from=2026-04-13T09:00:00")
    d = r.get_json()
    check("time_from filter: total=3", d["total"] == 3, f"got {d.get('total')}")

    w.stop(); w.join(timeout=2)


def test_api_events_basic():
    print("\n=== GET /api/events — basic ===")
    _, w, _, c = seed_db()

    r = c.get("/api/events")
    check("HTTP 200", r.status_code == 200)
    d = r.get_json()
    check("total=7", d["total"] == 7, f"got {d.get('total')}")
    check("default per_page=50", d["per_page"] == 50)
    check("page=1", d["page"] == 1)
    check("7 events returned", len(d["events"]) == 7)

    # Default sort is ts DESC
    timestamps = [e["ts"] for e in d["events"]]
    check("default sort ts DESC", timestamps == sorted(timestamps, reverse=True),
          f"got {timestamps}")

    w.stop(); w.join(timeout=2)


def test_api_events_pagination():
    print("\n=== GET /api/events — pagination ===")
    _, w, _, c = seed_db()

    r = c.get("/api/events?per_page=3&page=1")
    d = r.get_json()
    check("page 1: 3 events", len(d["events"]) == 3)
    check("total_pages=3", d["total_pages"] == 3, f"got {d.get('total_pages')}")

    r = c.get("/api/events?per_page=3&page=3")
    d = r.get_json()
    check("page 3: 1 event", len(d["events"]) == 1)

    r = c.get("/api/events?per_page=3&page=4")
    d = r.get_json()
    check("page 4 (beyond): 0 events", len(d["events"]) == 0)

    w.stop(); w.join(timeout=2)


def test_api_events_filters():
    print("\n=== GET /api/events — filters ===")
    _, w, _, c = seed_db()

    # Type filter
    r = c.get("/api/events?type=syslog")
    d = r.get_json()
    check("type=syslog: 3 events", d["total"] == 3)
    check("all type=syslog", all(e["type"] == "syslog" for e in d["events"]))

    r = c.get("/api/events?type=snmptrap,webhook")
    d = r.get_json()
    check("type=snmptrap,webhook: 4 events", d["total"] == 4)

    # Source IP prefix filter
    r = c.get("/api/events?src_ip=10.0.0")
    d = r.get_json()
    check("src_ip=10.0.0*: 4 events", d["total"] == 4,
          f"got {d.get('total')}")

    r = c.get("/api/events?src_ip=192.168")
    d = r.get_json()
    check("src_ip=192.168*: 1 event", d["total"] == 1)

    # Severity filter
    r = c.get("/api/events?severity=err,crit")
    d = r.get_json()
    check("severity=err,crit: 2 events", d["total"] == 2)

    # Time range
    r = c.get("/api/events?time_from=2026-04-13T09:00:00&time_to=2026-04-13T09:15:00")
    d = r.get_json()
    check("time range 09:00-09:15: 2 events", d["total"] == 2,
          f"got {d.get('total')}")

    # Global search
    r = c.get("/api/events?q=BGP")
    d = r.get_json()
    check("q=BGP: 2 events", d["total"] == 2)

    r = c.get("/api/events?q=disk")
    d = r.get_json()
    check("q=disk: 1 event", d["total"] == 1)

    r = c.get("/api/events?q=ops")
    d = r.get_json()
    check("q=ops (in tags): 2 events", d["total"] == 2)

    w.stop(); w.join(timeout=2)


def test_api_events_sorting():
    print("\n=== GET /api/events — sorting ===")
    _, w, _, c = seed_db()

    r = c.get("/api/events?sort=ts&order=asc")
    d = r.get_json()
    ts = [e["ts"] for e in d["events"]]
    check("sort=ts order=asc", ts == sorted(ts))

    r = c.get("/api/events?sort=src_ip&order=asc")
    d = r.get_json()
    ips = [e["src_ip"] for e in d["events"]]
    check("sort=src_ip order=asc", ips == sorted(ips))

    # Invalid sort column should fall back to ts
    r = c.get("/api/events?sort=INVALID")
    d = r.get_json()
    check("invalid sort falls back to ts", d["sort"] == "ts")

    w.stop(); w.join(timeout=2)


def test_api_events_combined():
    print("\n=== GET /api/events — combined filters ===")
    _, w, _, c = seed_db()

    r = c.get("/api/events?type=syslog&severity=err&q=BGP")
    d = r.get_json()
    check("type=syslog + severity=err + q=BGP: 1 event", d["total"] == 1)
    if d["events"]:
        check("correct event: BGP peer down",
              "peer" in d["events"][0].get("payload", "") and "down" in d["events"][0].get("payload", ""))

    w.stop(); w.join(timeout=2)


def test_api_events_cleanup():
    print("\n=== POST /api/events/cleanup ===")
    _, w, _, c = seed_db()

    r = c.post("/api/events/cleanup",
               data=json.dumps({"before_ts": "2026-04-13T09:00:00.000"}),
               headers={"Content-Type": "application/json"})
    check("Cleanup HTTP 200", r.status_code == 200)
    d = r.get_json()
    check("Cleanup deleted 4 old events", d["deleted"] == 4, f"got {d.get('deleted')}")

    r = c.get("/api/events?sort=ts&order=asc")
    d = r.get_json()
    check("Remaining events are at/after cutoff",
          all(e["ts"] >= "2026-04-13T09:00:00.000" for e in d["events"]))

    r = c.post("/api/events/cleanup",
               data=json.dumps({}),
               headers={"Content-Type": "application/json"})
    check("Cleanup missing cutoff -> 400", r.status_code == 400)

    r = c.post("/api/events/cleanup",
               data=json.dumps({"before_ts": "not-a-date"}),
               headers={"Content-Type": "application/json"})
    check("Cleanup invalid cutoff -> 400", r.status_code == 400)

    w.stop(); w.join(timeout=2)


def test_sse_hub():
    print("\n=== SSE Hub ===")
    hub = SSEHub()

    # Subscribe
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    check("2 clients connected", hub.client_count == 2)

    # Publish
    test_evt = {"ts": "2026-04-13T10:00:00", "type": "syslog", "payload": "test"}
    hub.publish(test_evt)

    msg1 = q1.get(timeout=1)
    msg2 = q2.get(timeout=1)
    check("client 1 received", "syslog" in msg1)
    check("client 2 received", "syslog" in msg2)
    check("SSE format: data: prefix", msg1.startswith("data: "))
    check("SSE format: double newline", msg1.endswith("\n\n"))

    # Unsubscribe
    hub.unsubscribe(q1)
    check("1 client after unsub", hub.client_count == 1)

    hub.publish({"ts": "2026-04-13T10:01:00", "type": "webhook"})
    check("q1 empty after unsub", q1.empty())
    check("q2 still receives", not q2.empty())

    hub.unsubscribe(q2)
    check("0 clients", hub.client_count == 0)


def test_sse_db_integration():
    print("\n=== SSE + DB Writer Integration ===")
    if os.path.exists(DB):
        os.remove(DB)
    init_db(DB, wal_mode=True)

    received = []

    # Use the global sse_hub
    client_q = sse_hub.subscribe()

    wq = queue.Queue()
    w = DBWriter(DB, wq, batch_size=10, flush_interval=0.1, sse_callback=sse_hub.publish)
    w.start()

    # Send event through the pipeline
    wq.put({"ts": "2026-04-13T10:00:00", "src_ip": "10.0.0.1", "type": "syslog",
            "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
            "payload": "SSE integration test", "tags": None})

    time.sleep(0.5)

    try:
        msg = client_q.get(timeout=2)
        received.append(msg)
    except queue.Empty:
        pass

    check("SSE received event from DB writer", len(received) == 1)
    if received:
        data = json.loads(received[0].replace("data: ", "").strip())
        check("SSE payload matches", data.get("payload") == "SSE integration test")

    sse_hub.unsubscribe(client_q)
    w.stop(); w.join(timeout=2)


def test_api_status():
    print("\n=== GET /health + /api/status ===")
    wq, w, _, c = seed_db()

    for path in ("/health",):
        r = c.get(path)
        check(f"{path} -> 200", r.status_code == 200)
        d = r.get_json()
        check(f"{path}: queue status", "queue" in d and "size" in d["queue"] and "max_size" in d["queue"])
        check(f"{path}: dropped metrics", "metrics" in d and "dropped_events" in d["metrics"])
        check(f"{path}: DB writer configured", d.get("db_writer", {}).get("configured") is True)
        check(f"{path}: DB writer total_written", d.get("db_writer", {}).get("total_written", 0) >= 7)
        check(f"{path}: SSE client count", "sse_clients" in d)
        check(f"{path}: app version", d.get("version") == "26.7.20")

    w.stop(); w.join(timeout=2)


def test_webhook_to_api():
    print("\n=== Webhook -> DB -> API round-trip ===")
    if os.path.exists(DB):
        os.remove(DB)
    init_db(DB, wal_mode=True)

    wq = queue.Queue()
    w = DBWriter(DB, wq, batch_size=10, flush_interval=0.1)
    w.start()

    app = create_app(db_path=DB, write_queue=wq)
    c = app.test_client()

    # Send webhooks
    for i in range(5):
        c.post("/webhook",
               data=json.dumps({"event": f"test-{i}", "severity": "info"}),
               headers={"Content-Type": "application/json"})

    time.sleep(0.5)

    # Query via API
    r = c.get("/api/events?type=webhook")
    d = r.get_json()
    check("5 webhook events via API", d["total"] == 5)

    r = c.get("/api/kpi")
    d = r.get_json()
    check("KPI webhook=5", d["webhook"] == 5)
    check("KPI total=5", d["total"] == 5)

def test_api_analytics():
    print("\n=== GET /api/analytics ===")
    _, w, _, c = seed_db()

    r = c.get("/api/analytics")
    check("HTTP 200", r.status_code == 200)
    d = r.get_json()
    check("has types key", "types" in d)
    check("has severities key", "severities" in d)
    check("has top_ips key", "top_ips" in d)
    check("has timeline key", "timeline" in d)
    check("has timeline_scale key", "timeline_scale" in d)

    # Check content values
    check("types syslog count is 3", d["types"]["syslog"] == 3)
    check("types snmptrap count is 2", d["types"]["snmptrap"] == 2)
    check("types webhook count is 2", d["types"]["webhook"] == 2)
    check("top_ips has 127.0.0.1", any(item["ip"] == "127.0.0.1" for item in d["top_ips"]))

    # Test with type filter
    r2 = c.get("/api/analytics?type=syslog")
    d2 = r2.get_json()
    check("filtered types syslog is 3", d2["types"]["syslog"] == 3)
    check("filtered types webhook is 0", d2["types"]["webhook"] == 0)

    w.stop(); w.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple NMS -- Phase 2 Validation Suite", [
        test_api_kpi,
        test_api_events_basic,
        test_api_events_pagination,
        test_api_events_filters,
        test_api_events_sorting,
        test_api_events_combined,
        test_api_events_cleanup,
        test_sse_hub,
        test_api_status,
        test_sse_db_integration,
        test_webhook_to_api,
        test_api_analytics,
    ]))
