"""Unified web application — webhook receiver, REST API, and SSE stream.

Endpoints
---------
POST /webhook          — receive webhook events
GET  /health           — health check
GET  /api/events       — paginated event query with filters
GET  /api/kpi          — aggregate counts
GET  /api/sse          — Server-Sent Events stream for real-time updates
"""

import json
import logging
import queue
import sqlite3
import threading
from datetime import datetime, timezone

import os
from flask import Flask, Response, request, jsonify, g, send_from_directory

from mibs import (
    ensure_module_symlink,
    first_writable_dir,
    list_mib_files,
    parse_module_name_from_lines,
    remove_module_symlinks,
)
from metrics import runtime_metrics
from version import __version__

logger = logging.getLogger(__name__)


def get_client_ip() -> str:
    peer = request.remote_addr or ""
    if peer in ("127.0.0.1", "::1"):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        xri = request.headers.get("X-Real-IP")
        if xri:
            return xri.strip()
    return peer


def _build_where(args) -> tuple[list, list]:
    """Build SQL WHERE clauses and params from request query parameters."""
    conditions, params = [], []

    time_from = args.get("time_from")
    time_to = args.get("time_to")
    if time_from:
        conditions.append("ts >= ?")
        params.append(time_from)
    if time_to:
        conditions.append("ts <= ?")
        params.append(time_to)

    types = args.get("type")
    if types:
        type_list = [t.strip() for t in types.split(",") if t.strip()]
        if type_list:
            conditions.append(f"type IN ({','.join('?' * len(type_list))})")
            params.extend(type_list)

    src_ip = args.get("src_ip")
    if src_ip:
        conditions.append("src_ip LIKE ?")
        params.append(f"{src_ip}%")

    severity = args.get("severity")
    if severity:
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if sev_list:
            conditions.append(f"severity IN ({','.join('?' * len(sev_list))})")
            params.extend(sev_list)

    q = args.get("q")
    if q:
        like = f"%{q}%"
        conditions.append(
            "(payload LIKE ? OR src_ip LIKE ? OR facility LIKE ? "
            "OR severity LIKE ? OR oid LIKE ? OR tags LIKE ?)"
        )
        params.extend([like] * 6)

    return conditions, params

# ---------------------------------------------------------------------------
# SSE Hub — pub/sub for real-time push
# ---------------------------------------------------------------------------

class SSEHub:
    """Manages per-client queues for Server-Sent Events."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[str]":
        q: queue.Queue[str] = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
        logger.debug("SSE client connected (%d total)", len(self._clients))
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass
        logger.debug("SSE client disconnected (%d remaining)", len(self._clients))

    def publish(self, event_dict: dict) -> None:
        """Broadcast an event to all connected SSE clients."""
        data = json.dumps(event_dict, ensure_ascii=False)
        msg = f"data: {data}\n\n"
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            if dead:
                runtime_metrics.inc_dropped("sse", len(dead))
            for q in dead:
                self._clients.remove(q)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# Global SSE hub instance
sse_hub = SSEHub()


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app(db_path: str, write_queue: "queue.Queue[dict]", db_writer=None, snmp_collector=None, config_path: str = "config.json") -> Flask:
    """Create the unified Flask application."""

    app = Flask(__name__,
                static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
                static_url_path="/static")
    app.config["DB_PATH"] = db_path
    app.config["DB_WRITER"] = db_writer
    app.config["SNMP_COLLECTOR"] = snmp_collector
    app.config["CONFIG_PATH"] = config_path

    # Suppress default Flask request logging
    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Serve Web UI
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ------------------------------------------------------------------
    # DB helper — per-request connection
    # ------------------------------------------------------------------
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def event_type_counts(where: str, params: list) -> tuple[int, dict]:
        db = get_db()
        total = db.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT type, COUNT(*) as count FROM events {where} GROUP BY type", params
        ).fetchall()
        counts = {r["type"]: r["count"] for r in rows}
        for event_type in ("syslog", "snmptrap", "webhook"):
            counts.setdefault(event_type, 0)
        return total, counts

    # ------------------------------------------------------------------
    # Webhook endpoint (from Phase 1)
    # ------------------------------------------------------------------
    @app.route("/webhook", methods=["POST"])
    def webhook():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "invalid JSON"}), 400

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        evt = {
            "ts": now,
            "src_ip": get_client_ip(),
            "type": "webhook",
            "facility": body.get("facility"),
            "severity": body.get("severity"),
            "oid": None,
            "varbinds": None,
            "payload": json.dumps(body, ensure_ascii=False),
            "tags": body.get("tags"),
        }
        try:
            write_queue.put_nowait(evt)
        except queue.Full:
            runtime_metrics.inc_dropped("webhook")
            logger.warning("Webhook event dropped because write queue is full")
            return jsonify({"error": "write queue full"}), 503
        return jsonify({"status": "ok"}), 202

    @app.route("/health", methods=["GET"])
    def health():
        writer = app.config.get("DB_WRITER")
        return jsonify({
            "status": "healthy",
            "version": __version__,
            "queue": {
                "size": write_queue.qsize(),
                "max_size": write_queue.maxsize or None,
            },
            "sse_clients": sse_hub.client_count,
            "metrics": runtime_metrics.snapshot(),
            "db_writer": writer.health_snapshot() if writer else {"configured": False},
        }), 200


    @app.route("/api/events/cleanup", methods=["POST"])
    def api_events_cleanup():
        body = request.get_json(silent=True) or {}
        before_ts = body.get("before_ts")
        if not before_ts or not isinstance(before_ts, str):
            return jsonify({"error": "before_ts is required"}), 400

        try:
            cutoff = before_ts.rstrip("Z")
            datetime.fromisoformat(cutoff)
        except ValueError:
            return jsonify({"error": "before_ts must be an ISO 8601 timestamp"}), 400

        db = get_db()
        cur = db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("Deleted %d events older than %s via Web UI", cur.rowcount, cutoff)
        return jsonify({"status": "ok", "before_ts": cutoff, "deleted": cur.rowcount}), 200

    # ------------------------------------------------------------------
    # REST API — GET /api/events
    # ------------------------------------------------------------------
    @app.route("/api/events", methods=["GET"])
    def api_events():
        db = get_db()

        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))

        conditions, params = _build_where(request.args)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        allowed_sort = {"ts", "src_ip", "type", "severity", "facility", "id"}
        sort_col = request.args.get("sort", "ts")
        if sort_col not in allowed_sort:
            sort_col = "ts"
        order = "DESC" if request.args.get("order", "desc").upper() == "DESC" else "ASC"

        total = db.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.execute(
            f"SELECT id, ts, src_ip, type, facility, severity, oid, varbinds, payload, tags "
            f"FROM events {where} ORDER BY {sort_col} {order} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

        events = []
        for r in rows:
            d = dict(r)
            if d.get("payload") and len(d["payload"]) > 200:
                d["payload_preview"] = d["payload"][:200] + "..."
            else:
                d["payload_preview"] = d.get("payload")
            events.append(d)

        return jsonify({
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),
            "sort": sort_col,
            "order": order.lower(),
            "events": events,
        })

    # ------------------------------------------------------------------
    # REST API — GET /api/kpi
    # ------------------------------------------------------------------
    @app.route("/api/kpi", methods=["GET"])
    def api_kpi():
        conditions, params = _build_where(request.args)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        total, counts = event_type_counts(where, params)
        return jsonify({"total": total, **counts})

    # ------------------------------------------------------------------
    # REST API — GET /api/analytics
    # ------------------------------------------------------------------
    @app.route("/api/analytics", methods=["GET"])
    def api_analytics():
        db = get_db()
        conditions, params = _build_where(request.args)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        _, types_res = event_type_counts(where, params)

        sev_rows = db.execute(
            f"SELECT severity, COUNT(*) as count FROM events {where} GROUP BY severity", params
        ).fetchall()
        severities_res = {r["severity"] if r["severity"] else "none": r["count"] for r in sev_rows}

        ip_rows = db.execute(
            f"SELECT src_ip, COUNT(*) as count FROM events {where} "
            f"GROUP BY src_ip ORDER BY count DESC LIMIT 10",
            params,
        ).fetchall()
        top_ips_res = [{"ip": r["src_ip"] or "unknown", "count": r["count"]} for r in ip_rows]

        span_row = db.execute(f"SELECT MIN(ts), MAX(ts) FROM events {where}", params).fetchone()
        scale = "hour"
        if span_row and span_row[0] and span_row[1]:
            try:
                min_t = datetime.fromisoformat(span_row[0].rstrip("Z").replace(" ", "T"))
                max_t = datetime.fromisoformat(span_row[1].rstrip("Z").replace(" ", "T"))
                if (max_t - min_t).days > 3:
                    scale = "day"
            except ValueError:
                pass

        strftime_clause = "strftime('%Y-%m-%d', ts)" if scale == "day" else "strftime('%Y-%m-%dT%H:00:00', ts)"
        timeline_rows = db.execute(
            f"SELECT {strftime_clause} as bucket, COUNT(*) as count "
            f"FROM events {where} GROUP BY bucket ORDER BY bucket ASC",
            params,
        ).fetchall()
        timeline_res = [{"time": r["bucket"], "count": r["count"]} for r in timeline_rows]

        return jsonify({
            "types": types_res,
            "severities": severities_res,
            "top_ips": top_ips_res,
            "timeline": timeline_res,
            "timeline_scale": scale,
        })

    # ------------------------------------------------------------------
    # SSE — GET /api/sse
    # ------------------------------------------------------------------
    @app.route("/api/sse", methods=["GET"])
    def api_sse():
        def stream():
            client_q = sse_hub.subscribe()
            try:
                # Send initial keepalive
                yield ": connected\n\n"
                while True:
                    try:
                        msg = client_q.get(timeout=15)
                        yield msg
                    except queue.Empty:
                        # Send keepalive comment to prevent timeout
                        yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            finally:
                sse_hub.unsubscribe(client_q)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------
    # REST API — GET /api/config & POST /api/config
    # ------------------------------------------------------------------
    def get_active_config_path() -> str:
        db_path = app.config.get("DB_PATH", "data/events.db")
        overlay_path = os.path.join(os.path.dirname(db_path) or "data", "config.json")
        return overlay_path if os.path.exists(overlay_path) else app.config.get("CONFIG_PATH", "config.json")

    def load_active_config() -> tuple[dict, str]:
        path = get_active_config_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), path
        except Exception:
            return {}, path

    def get_mib_dirs(cfg: dict) -> tuple[str, list[str]]:
        db_path = app.config.get("DB_PATH", "data/events.db")
        data_dir = os.path.dirname(db_path) or "data"
        default_mib_dir = os.path.join(data_dir, "mibs")
        mib_dirs = list(cfg.get("snmptrap", {}).get("mib_dirs", [default_mib_dir, "/usr/share/snmp/mibs"]))
        if default_mib_dir not in mib_dirs:
            mib_dirs = [default_mib_dir] + mib_dirs
        return default_mib_dir, mib_dirs

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        try:
            cfg, path = load_active_config()
            if not os.path.exists(path):
                return jsonify({"error": "Config file not found"}), 404
            return jsonify({
                "community": cfg.get("snmptrap", {}).get("community", "simplenms"),
                "webhook_port": cfg.get("webhook", {}).get("port", 80)
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config", methods=["POST"])
    def api_config_post():
        body = request.get_json(silent=True) or {}
        community = body.get("community")
        webhook_port = body.get("webhook_port")
        
        cfg, path = load_active_config()
        if not cfg and not os.path.exists(path):
            return jsonify({"error": "Failed to read config: Config file not found"}), 500
            
        updated = False
        port_changed = False
        
        # 1. Update community if provided
        if community is not None:
            if not isinstance(community, str) or not community.strip():
                return jsonify({"error": "community string cannot be empty"}), 400
            community = community.strip()
            if "snmptrap" not in cfg:
                cfg["snmptrap"] = {}
            if cfg["snmptrap"].get("community") != community:
                cfg["snmptrap"]["community"] = community
                updated = True
                # Dynamically update running collector
                collector = app.config.get("SNMP_COLLECTOR")
                if collector and hasattr(collector, "update_community"):
                    collector.update_community(community)

        # 2. Update webhook port if provided
        if webhook_port is not None:
            try:
                port_val = int(webhook_port)
                if port_val < 1 or port_val > 65535:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "webhook_port must be a valid integer between 1 and 65535"}), 400
                
            if "webhook" not in cfg:
                cfg["webhook"] = {}
            if cfg["webhook"].get("port") != port_val:
                cfg["webhook"]["port"] = port_val
                updated = True
                port_changed = True

        if updated:
            db_path = app.config.get("DB_PATH", "data/events.db")
            data_dir = os.path.dirname(db_path) or "data"
            overlay_path = os.path.join(data_dir, "config.json")
            try:
                os.makedirs(data_dir, exist_ok=True)
                with open(overlay_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4)
                app.config["CONFIG_PATH"] = overlay_path
            except Exception as e:
                return jsonify({"error": f"Failed to write config: {e}"}), 500
                
        return jsonify({
            "status": "ok",
            "community": cfg.get("snmptrap", {}).get("community", "simplenms"),
            "webhook_port": cfg.get("webhook", {}).get("port", 80),
            "port_changed": port_changed
        })

    # ------------------------------------------------------------------
    # REST API — MIB Management
    # ------------------------------------------------------------------
    @app.route("/api/mibs", methods=["GET"])
    def api_mibs_get():
        cfg, _ = load_active_config()
        _, mib_dirs = get_mib_dirs(cfg)
        return jsonify(list_mib_files(mib_dirs))

    @app.route("/api/mibs", methods=["POST"])
    def api_mibs_upload():
        if "file" not in request.files:
            return jsonify({"error": "No file part"}), 400
        f = request.files["file"]
        if f.filename == "":
             return jsonify({"error": "No selected file"}), 400
             
        content_bytes = f.read()
        try:
            content = content_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            return jsonify({"error": f"Failed to read file content: {e}"}), 400
            
        real_name = parse_module_name_from_lines(content.splitlines()[:1000])
                 
        if not real_name:
            return jsonify({
                "error": "Invalid MIB format. File must contain a valid ASN.1 module header: 'ModuleName DEFINITIONS ::= BEGIN'"
            }), 400
             
        cfg, _ = load_active_config()
        default_mib_dir, mib_dirs = get_mib_dirs(cfg)
        save_dir = first_writable_dir(mib_dirs, default_mib_dir)
        
        if not os.path.exists(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except Exception as e:
                return jsonify({"error": f"Failed to create MIB directory: {e}"}), 500
                 
        from werkzeug.utils import secure_filename
        filename = secure_filename(f.filename)
        if not filename:
            filename = f"{real_name}.txt"
        dest_path = os.path.join(save_dir, filename)
         
        try:
            with open(dest_path, "wb") as out_f:
                out_f.write(content_bytes)
        except Exception as e:
            return jsonify({"error": f"Failed to save MIB file: {e}"}), 500
             
        try:
            ensure_module_symlink(save_dir, filename, real_name)
        except Exception as e:
            logger.warning("Failed to create module-name symlink for %s: %s", filename, e)
                     
        collector = app.config.get("SNMP_COLLECTOR")
        if collector and hasattr(collector, "_resolver") and collector._resolver:
            try:
                collector._resolver.reload_discovered_mibs(save_dir)
            except Exception as e:
                logger.warning("Failed to dynamically reload MIB resolver: %s", e)
                 
        return jsonify({
            "status": "ok",
            "filename": filename,
            "module_name": real_name,
            "message": "MIB file uploaded and validated successfully"
        }), 201

    @app.route("/api/mibs/<filename>", methods=["DELETE"])
    def api_mibs_delete(filename):
        from werkzeug.utils import secure_filename
        filename = secure_filename(filename)
        
        cfg, _ = load_active_config()
        _, mib_dirs = get_mib_dirs(cfg)
        
        found = next(((os.path.join(d, filename), d) for d in mib_dirs
                      if os.path.exists(os.path.join(d, filename))), None)
        path, save_dir = found if found else (None, None)
                
        if not path or not save_dir:
            return jsonify({"error": "MIB file not found"}), 404
            
        try:
            os.remove(path)
            remove_module_symlinks(save_dir, filename)
        except Exception as e:
            return jsonify({"error": f"Failed to delete MIB file: {e}"}), 500
             
        return jsonify({"status": "ok", "message": f"MIB file {filename} deleted successfully"})

    return app
