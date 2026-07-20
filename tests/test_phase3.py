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


def test_static_contract():
    print("\n=== Static UI Contract ===")
    _, w, _, c = setup()
    html = c.get("/").data.decode("utf-8")

    required_ids = [
        "themeToggle", "globalSearch", "sseIndicator", "sidebarToggle",
        "appVersion",
        "kpiTotal", "kpiSyslog", "kpiSnmptrap", "kpiWebhook",
        "timeFrom", "timeTo", "srcIpFilter", "clearFilters",
        "openCleanupModal", "cleanupDate", "cleanupConfirm",
        "eventsBody", "pagination", "tabFeed", "tabAnalytics",
        "viewFeed", "viewAnalytics", "chartTimeline", "chartTypes",
        "chartSeverities", "chartSources", "sidebarOverlay",
        "btnSaveService", "btnApplyTls", "syslogTlsOverallStatus",
        "inputCommunity", "inputSyslogTlsEnabled", "mibListBody", "githubLink",
    ]
    missing_ids = [id_ for id_ in required_ids if f'id="{id_}"' not in html]
    check("Required UI ids present", not missing_ids, f"missing {missing_ids}")

    required_api_paths = ["/health", "/api/events", "/api/events/cleanup", "/api/kpi", "/api/sse"]
    missing_paths = [path for path in required_api_paths if path not in html]
    check("Required API paths referenced", not missing_paths, f"missing {missing_paths}")
    check("Theme follows browser preference", "prefers-color-scheme: dark" in html)

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
        "file": (BytesIO(mib_content), "vendor-upload.my")
    }, content_type="multipart/form-data")
    check("Upload MIB returns 201", r.status_code == 201)
    
    # Verify file was saved in the writable data MIB directory (test_dir/mibs)
    writable_mib_dir = os.path.join(test_dir, "mibs")
    uploaded_mib = os.path.join(writable_mib_dir, "vendor-upload.my")
    old_link = os.path.join(writable_mib_dir, "MY-TEST-MIB.my")
    new_link = os.path.join(writable_mib_dir, "MY-REPLACED-MIB.my")
    check("Writable MIB directory was created", os.path.exists(writable_mib_dir))
    check("MIB file was saved in writable directory", os.path.exists(uploaded_mib))
    check("Module-name symlink was created", os.path.islink(old_link))

    # Replacing the same uploaded filename with a different module should not leave a stale symlink.
    r = c.post("/api/mibs", data={
        "file": (BytesIO(b"MY-REPLACED-MIB DEFINITIONS ::= BEGIN\nEND\n"), "vendor-upload.my")
    }, content_type="multipart/form-data")
    check("Replace MIB returns 201", r.status_code == 201)
    check("Old module-name symlink was removed", not os.path.exists(old_link))
    check("New module-name symlink was created", os.path.islink(new_link))
    
    # 5. GET /api/mibs to list MIBs
    r = c.get("/api/mibs")
    check("GET mibs returns 200", r.status_code == 200)
    mibs_list = r.get_json()
    check("Uploaded MIB is in the list", any(
        m["filename"] == "vendor-upload.my" and m["module_name"] == "MY-REPLACED-MIB"
        for m in mibs_list
    ))
    
    # 6. DELETE /api/mibs/<filename>
    r = c.delete("/api/mibs/vendor-upload.my")
    check("DELETE MIB returns 200", r.status_code == 200)
    check("MIB file was deleted", not os.path.exists(uploaded_mib))
    check("Module-name symlink was deleted", not os.path.exists(new_link))
    
    # Cleanup
    os.chmod(os.path.join(test_dir, "mibs_readonly"), 0o777) # restore permission for clean deletion
    w.stop(); w.join(timeout=2)
    shutil.rmtree(test_dir)


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple NMS -- Phase 3 Validation Suite", [
        test_static_serving,
        test_static_contract,
        test_api_cors,
        test_config_and_mibs,
    ]))
