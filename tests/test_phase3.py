#!/usr/bin/env python3
"""Phase 3 validation — Web UI static serving and HTML/CSS/JS integrity.

Usage:  python3 test_phase3.py
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
from web_app import create_app

DB = "/tmp/snms_test_p3.db"


def setup():
    if os.path.exists(DB):
        os.remove(DB)
    init_db(DB, wal_mode=True)
    wq = queue.Queue()
    w = DBWriter(DB, wq, batch_size=10, flush_interval=0.1)
    w.start()

    # Seed some events
    events = [
        {"ts": "2026-04-13T08:00:00.000", "src_ip": "10.0.0.1", "type": "syslog",
         "facility": "local0", "severity": "info", "oid": None, "varbinds": None,
         "payload": "BGP peer established", "tags": None},
        {"ts": "2026-04-13T08:30:00.000", "src_ip": "10.0.0.2", "type": "snmptrap",
         "facility": None, "severity": None, "oid": "1.3.6.1.6.3.1.1.5.3",
         "varbinds": json.dumps({"1.3.6.1.2.1.2.2.1.1": "3"}),
         "payload": None, "tags": None},
        {"ts": "2026-04-13T09:00:00.000", "src_ip": "127.0.0.1", "type": "webhook",
         "facility": None, "severity": "crit", "oid": None, "varbinds": None,
         "payload": json.dumps({"event": "alert", "message": "disk full"}),
         "tags": "ops"},
    ]
    for e in events:
        wq.put(e)
    time.sleep(0.5)

    app = create_app(db_path=DB, write_queue=wq)
    client = app.test_client()
    return wq, w, app, client


def test_static_serving():
    print("\n=== Static File Serving ===")
    _, w, _, c = setup()

    # Root serves index.html
    r = c.get("/")
    check("GET / returns 200", r.status_code == 200)
    html = r.data.decode("utf-8")
    check("HTML contains <!DOCTYPE html>", "<!DOCTYPE html>" in html)
    check("Title is Simple NMS", "<title>Simple NMS</title>" in html)
    w.stop(); w.join(timeout=2)


def test_html_structure():
    print("\n=== HTML Structure ===")
    _, w, _, c = setup()
    html = c.get("/").data.decode("utf-8")

    # Header elements
    check("Header logo: simple.nms", "simple<span" in html and "nms" in html)
    check("Theme toggle button", 'id="themeToggle"' in html)
    check("Global search input", 'id="globalSearch"' in html)
    check("SSE indicator", 'id="sseIndicator"' in html)
    check("Sidebar toggle (mobile)", 'id="sidebarToggle"' in html)

    # KPI cards
    check("KPI total card", 'id="kpiTotal"' in html)
    check("KPI syslog card", 'id="kpiSyslog"' in html)
    check("KPI snmptrap card", 'id="kpiSnmptrap"' in html)
    check("KPI webhook card", 'id="kpiWebhook"' in html)

    # Sidebar filters
    check("Time range buttons", 'data-range="5m"' in html)
    check("Time range: 1h", 'data-range="1h"' in html)
    check("Time range: today", 'data-range="today"' in html)
    check("Time range: all", 'data-range="all"' in html)
    check("Time from input", 'id="timeFrom"' in html)
    check("Time to input", 'id="timeTo"' in html)
    check("Type filter checkboxes", 'value="syslog"' in html)
    check("Source IP filter", 'id="srcIpFilter"' in html)
    check("Clear filters button", 'id="clearFilters"' in html)
    check("Cleanup button", 'id="openCleanupModal"' in html)
    check("Cleanup date picker", 'id="cleanupDate"' in html)
    check("Cleanup confirm button", 'id="cleanupConfirm"' in html)

    # Event table
    check("Events table body", 'id="eventsBody"' in html)
    check("Sortable th: ts", 'data-sort="ts"' in html)
    check("Sortable th: src_ip", 'data-sort="src_ip"' in html)
    check("Sortable th: type", 'data-sort="type"' in html)
    check("Sortable th: severity", 'data-sort="severity"' in html)
    check("Pagination container", 'id="pagination"' in html)

    # Tabs and Analytics Charts
    check("Tabs container element", 'class="tabs-container"' in html)
    check("Tab Feed button", 'id="tabFeed"' in html)
    check("Tab Analytics button", 'id="tabAnalytics"' in html)
    check("View Feed panel", 'id="viewFeed"' in html)
    check("View Analytics panel", 'id="viewAnalytics"' in html)
    check("Timeline chart canvas", 'id="chartTimeline"' in html)
    check("Types chart canvas", 'id="chartTypes"' in html)
    check("Severities chart canvas", 'id="chartSeverities"' in html)
    check("Sources chart canvas", 'id="chartSources"' in html)

    # Responsive
    check("Sidebar overlay", 'id="sidebarOverlay"' in html)

    w.stop(); w.join(timeout=2)


def test_css_features():
    print("\n=== CSS Features ===")
    _, w, _, c = setup()
    html = c.get("/").data.decode("utf-8")

    # CSS variables
    check("CSS var: --bg-primary", "--bg-primary:" in html)
    check("CSS var: --accent", "--accent:" in html)
    check("CSS var: --font-mono", "--font-mono:" in html)

    # Dark theme
    check("body.dark CSS block", "body.dark {" in html or "body.dark{" in html)
    check("Dark theme overrides vars", "body.dark" in html)

    # Severity colors
    check("Severity CSS classes", ".severity-dot.emerg" in html)
    check("Type badge styles", ".type-badge.syslog" in html)

    # Responsive breakpoint
    check("768px breakpoint", "768px" in html)

    # Animations
    check("Pulse animation", "@keyframes pulse" in html)

    # Google Fonts
    check("IBM Plex Mono font", "IBM+Plex+Mono" in html or "IBM Plex Mono" in html)

    w.stop(); w.join(timeout=2)


def test_js_features():
    print("\n=== JavaScript Features ===")
    _, w, _, c = setup()
    html = c.get("/").data.decode("utf-8")

    # API integration
    check("JS fetches /api/events", "/api/events" in html)
    check("JS posts /api/events/cleanup", "/api/events/cleanup" in html)
    check("JS fetches /api/kpi", "/api/kpi" in html)
    check("JS connects to /api/sse", "/api/sse" in html)
    check("JS displays UTC timestamp in detail", "UTC Time" in html)
    check("JS has local timezone label", "LOCAL_TIMEZONE" in html)

    # Debounce
    check("Debounce function defined", "function debounce" in html or "debounce" in html)
    check("300ms debounce on search", "300" in html)

    # Sort with localStorage
    check("localStorage sort save", "localStorage.setItem" in html and "nms_sort" in html)
    check("localStorage sort restore", "localStorage.getItem" in html)

    # Theme toggle
    check("Theme toggle: nms_theme", "nms_theme" in html)
    check("classList.toggle dark", "classList.toggle" in html and "dark" in html)

    # SSE EventSource
    check("EventSource constructor", "new EventSource" in html)
    check("SSE onmessage handler", "onmessage" in html)
    check("SSE reconnection handling", "onerror" in html)

    # Pagination
    check("goPage function", "goPage" in html)

    w.stop(); w.join(timeout=2)


def test_api_cors():
    print("\n=== API + Static Integration ===")
    _, w, _, c = setup()

    # Verify API still works alongside static serving
    r = c.get("/api/kpi")
    check("API /api/kpi still works", r.status_code == 200)
    d = r.get_json()
    check("KPI returns correct data", d["total"] == 3)

    r = c.get("/api/events")
    check("API /api/events still works", r.status_code == 200)
    d = r.get_json()
    check("Events returns data", len(d["events"]) == 3)

    # Webhook still works
    r = c.post("/webhook",
               data=json.dumps({"event": "test"}),
               headers={"Content-Type": "application/json"})
    check("Webhook still accepts events", r.status_code == 202)

    r = c.get("/health")
    d = r.get_json()
    check("Health includes SSE client count", "sse_clients" in d)

    w.stop(); w.join(timeout=2)


def test_config_and_mibs():
    print("\n=== Config and MIBs API (Overlay) ===")
    import shutil
    
    # Setup temporary config.json
    test_dir = "/tmp/snms_test_p3_config"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)
    
    default_cfg_path = os.path.join(test_dir, "config.json")
    default_cfg = {
        "database": {
            "path": os.path.join(test_dir, "events.db"),
            "wal_mode": True
        },
        "snmptrap": {
            "enabled": True,
            "community": "initial_community",
            "mib_dirs": [os.path.join(test_dir, "mibs_readonly"), "/usr/share/snmp/mibs"]
        },
        "webhook": {
            "enabled": True,
            "port": 80
        }
    }
    with open(default_cfg_path, "w") as f:
        json.dump(default_cfg, f)
        
    # Also create the read-only directory
    os.makedirs(os.path.join(test_dir, "mibs_readonly"))
    # Make it read-only (mimicking the production environment)
    os.chmod(os.path.join(test_dir, "mibs_readonly"), 0o555)
    
    init_db(default_cfg["database"]["path"], wal_mode=True)
    wq = queue.Queue()
    w = DBWriter(default_cfg["database"]["path"], wq, batch_size=10, flush_interval=0.1)
    w.start()
    
    class FakeCollector:
        def __init__(self):
            self.community = "initial_community"
        def update_community(self, comm):
            self.community = comm
            
    fake_collector = FakeCollector()
    
    app = create_app(
        db_path=default_cfg["database"]["path"],
        write_queue=wq,
        db_writer=w,
        snmp_collector=fake_collector,
        config_path=default_cfg_path
    )
    c = app.test_client()
    
    # 1. GET /api/config
    r = c.get("/api/config")
    check("GET config returns 200", r.status_code == 200)
    data = r.get_json()
    check("GET config returns initial community", data["community"] == "initial_community")
    check("GET config returns default webhook port", data["webhook_port"] == 80)
    
    # 2. POST /api/config to update settings (this must write to overlay)
    r = c.post("/api/config", data=json.dumps({
        "community": "updated_community",
        "webhook_port": 8080
    }), content_type="application/json")
    check("POST config returns 200", r.status_code == 200)
    data = r.get_json()
    check("POST config returns updated community", data["community"] == "updated_community")
    check("POST config returns updated webhook port", data["webhook_port"] == 8080)
    check("Collector community dynamically updated", fake_collector.community == "updated_community")
    
    # Verify overlay config file was created in data directory (which is test_dir)
    overlay_path = os.path.join(test_dir, "config.json")
    check("Overlay config file exists", os.path.exists(overlay_path))
    with open(overlay_path) as f:
        overlay_data = json.load(f)
    check("Overlay config has updated community", overlay_data["snmptrap"]["community"] == "updated_community")
    check("Overlay config has updated webhook port", overlay_data["webhook"]["port"] == 8080)
    
    # 3. GET /api/config should now load from overlay
    r = c.get("/api/config")
    check("GET config after update returns 200", r.status_code == 200)
    data = r.get_json()
    check("GET config returns updated community", data["community"] == "updated_community")
    check("GET config returns updated webhook port", data["webhook_port"] == 8080)
    
    # 4. POST /api/mibs: Upload a valid MIB file.
    # Since mibs_readonly is read-only, it should automatically fall back to the writable test_dir/mibs!
    from io import BytesIO
    mib_content = b"MY-TEST-MIB DEFINITIONS ::= BEGIN\nEND\n"
    r = c.post("/api/mibs", data={
        "file": (BytesIO(mib_content), "MY-TEST-MIB.my")
    }, content_type="multipart/form-data")
    check("Upload MIB returns 201", r.status_code == 201)
    
    # Verify file was saved in the writable data MIB directory (test_dir/mibs)
    writable_mib_dir = os.path.join(test_dir, "mibs")
    check("Writable MIB directory was created", os.path.exists(writable_mib_dir))
    check("MIB file was saved in writable directory", os.path.exists(os.path.join(writable_mib_dir, "MY-TEST-MIB.my")))
    
    # 5. GET /api/mibs to list MIBs
    r = c.get("/api/mibs")
    check("GET mibs returns 200", r.status_code == 200)
    mibs_list = r.get_json()
    check("Uploaded MIB is in the list", any(m["filename"] == "MY-TEST-MIB.my" for m in mibs_list))
    
    # 6. DELETE /api/mibs/<filename>
    r = c.delete("/api/mibs/MY-TEST-MIB.my")
    check("DELETE MIB returns 200", r.status_code == 200)
    check("MIB file was deleted", not os.path.exists(os.path.join(writable_mib_dir, "MY-TEST-MIB.my")))
    
    # Cleanup
    os.chmod(os.path.join(test_dir, "mibs_readonly"), 0o777) # restore permission for clean deletion
    w.stop(); w.join(timeout=2)
    shutil.rmtree(test_dir)


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple NMS -- Phase 3 Validation Suite", [
        test_static_serving,
        test_html_structure,
        test_css_features,
        test_js_features,
        test_api_cors,
        test_config_and_mibs,
    ]))
