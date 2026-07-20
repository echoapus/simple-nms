#!/usr/bin/env python3
"""Simple NMS — main entry point.

Reads config.json, initialises the database, starts the DB writer thread
(with SSE broadcast callback), and launches all enabled collectors plus
the unified web server (webhook + REST API + SSE).
"""

import json
import logging
import queue
import signal
import sys
import threading
import time

from database import init_db, DBWriter
from collectors.syslog_listener import SyslogCollector, SyslogTLSCollector
from collectors.snmp_listener import SNMPTrapCollector
from web_app import create_app, sse_hub

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("simple-nms")


class TLSCollectorManager:
    """Owns the replaceable RFC 5425 listener used by the Web Settings API."""

    def __init__(self, write_queue):
        self.write_queue = write_queue
        self.collector = None
        self.config = None
        self._lock = threading.Lock()

    def _start(self, cfg: dict) -> tuple[bool, str | None]:
        collector = SyslogTLSCollector(
            self.write_queue,
            host=cfg.get("host", "0.0.0.0"),
            port=cfg.get("port", 6514),
            certfile=cfg.get("certfile", ""),
            keyfile=cfg.get("keyfile", ""),
            cafile=cfg.get("cafile") or None,
            require_client_cert=cfg.get("require_client_cert", False),
        )
        collector.start()
        if not collector.ready.wait(5):
            collector.stop()
            return False, "TLS listener start timed out"
        if collector.start_error:
            return False, collector.start_error
        self.collector = collector
        self.config = dict(cfg)
        return True, None

    def apply(self, cfg: dict) -> dict:
        """Replace the listener; restore the previous one if replacement fails."""
        with self._lock:
            enabled = cfg.get("enabled", False)
            previous, previous_cfg = self.collector, self.config
            if previous:
                previous.stop()
                previous.join(timeout=2)
                self.collector = None
            if not enabled:
                self.config = dict(cfg)
                return {"applied": True, "enabled": False}

            ok, error = self._start(cfg)
            if ok:
                return {"applied": True, "enabled": True}

            if previous_cfg and previous_cfg.get("enabled", False):
                restored, restore_error = self._start(previous_cfg)
                if not restored:
                    logger.error("Failed to restore previous TLS listener: %s", restore_error)
            return {"applied": False, "error": error}

    def stop(self) -> None:
        with self._lock:
            if self.collector:
                self.collector.stop()
                self.collector.join(timeout=2)
                self.collector = None


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    logger.info("Configuration loaded from %s", path)
    return cfg




def main() -> None:
    import os
    default_config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_config(default_config_path)
    config_path = default_config_path

    db_cfg = cfg.get("database", {})
    db_path = db_cfg.get("path", "data/events.db")
    data_dir = os.path.dirname(db_path) or "data"

    overlay_path = os.path.join(data_dir, "config.json")
    if os.path.exists(overlay_path) and overlay_path != default_config_path:
        logger.info("Found overlay configuration at %s, reloading...", overlay_path)
        try:
            cfg = load_config(overlay_path)
            config_path = overlay_path
            db_cfg = cfg.get("database", {})
            db_path = db_cfg.get("path", db_path)
            data_dir = os.path.dirname(db_path) or "data"
        except Exception as e:
            logger.error("Failed to load overlay config at %s: %s. Falling back to default.", overlay_path, e)

    # Ensure data and mibs directories exist
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "mibs"), exist_ok=True)

    wal = db_cfg.get("wal_mode", True)
    init_db(db_path, wal_mode=wal)

    # Central write queue
    write_queue: queue.Queue[dict] = queue.Queue(maxsize=50_000)

    # DB writer with SSE broadcast callback
    writer_cfg = cfg.get("writer", {})
    db_writer = DBWriter(
        db_path=db_path,
        write_queue=write_queue,
        batch_size=writer_cfg.get("batch_size", 100),
        flush_interval=writer_cfg.get("flush_interval_ms", 200) / 1000.0,
        sse_callback=sse_hub.publish,
    )
    db_writer.start()
    tls_manager = TLSCollectorManager(write_queue)

    threads = []
    _web_server = None

    # Syslog collector
    syslog_cfg = cfg.get("syslog", {})
    if syslog_cfg.get("enabled", False):
        sc = SyslogCollector(
            write_queue,
            host=syslog_cfg.get("host", "0.0.0.0"),
            port=syslog_cfg.get("port", 514),
        )
        sc.start()
        threads.append(sc)
        logger.info("Syslog collector started on :%d/udp", syslog_cfg.get("port", 514))

    # SNMP trap collector
    snmp_cfg = cfg.get("snmptrap", {})
    tc = None
    if snmp_cfg.get("enabled", False):
        tc = SNMPTrapCollector(
            write_queue,
            host=snmp_cfg.get("host", "0.0.0.0"),
            port=snmp_cfg.get("port", 162),
            community=snmp_cfg.get("community", "simplenms"),
            mib_dirs=snmp_cfg.get("mib_dirs"),
            mib_modules=snmp_cfg.get("mib_modules"),
        )
        tc.start()
        threads.append(tc)
        logger.info("SNMP trap collector started on :%d/udp", snmp_cfg.get("port", 162))

    tls_cfg = cfg.get("syslog_tls", {})
    if tls_cfg.get("enabled", False):
        result = tls_manager.apply(tls_cfg)
        if not result["applied"]:
            logger.error("TLS syslog collector was not started: %s", result["error"])

    # Unified web server (webhook + API + SSE)
    webhook_cfg = cfg.get("webhook", {})
    if webhook_cfg.get("enabled", False):
        from werkzeug.serving import make_server
        app = create_app(
            db_path=db_path,
            write_queue=write_queue,
            db_writer=db_writer,
            snmp_collector=tc,
            config_path=config_path,
            tls_reloader=tls_manager.apply,
        )
        host = webhook_cfg.get("host", "0.0.0.0")
        port = webhook_cfg.get("port", 5000)
        _web_server = make_server(host, port, app, threaded=True)
        logger.info("Web server listening on %s:%d/http (webhook + API + SSE)", host, port)
        ws = threading.Thread(target=_web_server.serve_forever, daemon=True, name="web-server")
        ws.start()
        threads.append(ws)

    if not threads:
        logger.warning("No collectors enabled — check config.json")

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        if not shutdown:
            shutdown = True
            logger.info("Shutdown signal received — stopping...")
            if _web_server:
                _web_server.shutdown()
            for t in threads:
                if hasattr(t, "stop"):
                    t.stop()
            tls_manager.stop()
            db_writer.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Simple NMS is running.  Press Ctrl+C to stop.")

    try:
        while not shutdown:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_signal(None, None)

    db_writer.join()
    logger.info("Simple NMS stopped.")


if __name__ == "__main__":
    main()
