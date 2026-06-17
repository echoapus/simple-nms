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
import time
from datetime import datetime, timezone

import os

from flask import Flask, Response, request, jsonify, g, send_from_directory

from metrics import runtime_metrics

logger = logging.getLogger(__name__)

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

def create_app(db_path: str, write_queue: "queue.Queue[dict]", db_writer=None) -> Flask:
    """Create the unified Flask application."""

    app = Flask(__name__,
                static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
                static_url_path="/static")
    app.config["DB_PATH"] = db_path
    app.config["DB_WRITER"] = db_writer

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
            g.db.execute("PRAGMA journal_mode=WAL")
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

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
            "src_ip": request.remote_addr,
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

    def runtime_status() -> dict:
        writer = app.config.get("DB_WRITER")
        queue_max_size = write_queue.maxsize if write_queue.maxsize > 0 else None
        status = {
            "status": "healthy",
            "queue": {
                "size": write_queue.qsize(),
                "max_size": queue_max_size,
            },
            "sse_clients": sse_hub.client_count,
            "metrics": runtime_metrics.snapshot(),
            "db_writer": writer.health_snapshot() if writer else {"configured": False},
        }
        return status

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(runtime_status()), 200

    @app.route("/api/status", methods=["GET"])
    def api_status():
        return jsonify(runtime_status()), 200

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
        total_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        to_delete = db.execute(
            "SELECT COUNT(*) FROM events WHERE ts < ?",
            (cutoff,),
        ).fetchone()[0]
        db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        total_after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        logger.info("Deleted %d events older than %s via Web UI", to_delete, cutoff)
        return jsonify({
            "status": "ok",
            "before_ts": cutoff,
            "deleted": to_delete,
            "total_before": total_before,
            "total_after": total_after,
        }), 200

    # ------------------------------------------------------------------
    # REST API — GET /api/events
    # ------------------------------------------------------------------
    @app.route("/api/events", methods=["GET"])
    def api_events():
        db = get_db()

        # Pagination
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))

        # Build WHERE clauses
        conditions = []
        params = []

        # Time range
        time_from = request.args.get("time_from")
        time_to = request.args.get("time_to")
        if time_from:
            conditions.append("ts >= ?")
            params.append(time_from)
        if time_to:
            conditions.append("ts <= ?")
            params.append(time_to)

        # Event type filter (comma-separated)
        types = request.args.get("type")
        if types:
            type_list = [t.strip() for t in types.split(",") if t.strip()]
            if type_list:
                placeholders = ",".join("?" * len(type_list))
                conditions.append(f"type IN ({placeholders})")
                params.extend(type_list)

        # Source IP filter (prefix match)
        src_ip = request.args.get("src_ip")
        if src_ip:
            conditions.append("src_ip LIKE ?")
            params.append(f"{src_ip}%")

        # Severity filter
        severity = request.args.get("severity")
        if severity:
            sev_list = [s.strip() for s in severity.split(",") if s.strip()]
            if sev_list:
                placeholders = ",".join("?" * len(sev_list))
                conditions.append(f"severity IN ({placeholders})")
                params.extend(sev_list)

        # Global search (fuzzy match across multiple fields)
        q = request.args.get("q")
        if q:
            conditions.append(
                "(payload LIKE ? OR src_ip LIKE ? OR facility LIKE ? "
                "OR severity LIKE ? OR oid LIKE ? OR tags LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like] * 6)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        # Sorting
        allowed_sort = {"ts", "src_ip", "type", "severity", "facility", "id"}
        sort_col = request.args.get("sort", "ts")
        if sort_col not in allowed_sort:
            sort_col = "ts"
        order = "DESC" if request.args.get("order", "desc").upper() == "DESC" else "ASC"

        # Count total matching rows
        count_sql = f"SELECT COUNT(*) FROM events {where}"
        total = db.execute(count_sql, params).fetchone()[0]

        # Fetch page
        offset = (page - 1) * per_page
        data_sql = (
            f"SELECT id, ts, src_ip, type, facility, severity, oid, varbinds, payload, tags "
            f"FROM events {where} ORDER BY {sort_col} {order} "
            f"LIMIT ? OFFSET ?"
        )
        rows = db.execute(data_sql, params + [per_page, offset]).fetchall()

        events = []
        for r in rows:
            d = dict(r)
            # Truncate payload for list view
            if d.get("payload") and len(d["payload"]) > 200:
                d["payload_preview"] = d["payload"][:200] + "..."
            else:
                d["payload_preview"] = d.get("payload")
            events.append(d)

        return jsonify({
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, -(-total // per_page)),  # ceil division
            "sort": sort_col,
            "order": order.lower(),
            "events": events,
        })

    # ------------------------------------------------------------------
    # REST API — GET /api/kpi
    # ------------------------------------------------------------------
    @app.route("/api/kpi", methods=["GET"])
    def api_kpi():
        db = get_db()

        conditions = []
        params = []

        time_from = request.args.get("time_from")
        time_to = request.args.get("time_to")
        if time_from:
            conditions.append("ts >= ?")
            params.append(time_from)
        if time_to:
            conditions.append("ts <= ?")
            params.append(time_to)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        total = db.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()[0]

        counts = {}
        for etype in ("syslog", "snmptrap", "webhook"):
            cond = list(conditions) + ["type = ?"]
            p = list(params) + [etype]
            w = "WHERE " + " AND ".join(cond)
            counts[etype] = db.execute(f"SELECT COUNT(*) FROM events {w}", p).fetchone()[0]

        return jsonify({
            "total": total,
            "syslog": counts["syslog"],
            "snmptrap": counts["snmptrap"],
            "webhook": counts["webhook"],
        })

    # ------------------------------------------------------------------
    # REST API — GET /api/analytics
    # ------------------------------------------------------------------
    @app.route("/api/analytics", methods=["GET"])
    def api_analytics():
        db = get_db()

        # Build WHERE clauses
        conditions = []
        params = []

        # Time range
        time_from = request.args.get("time_from")
        time_to = request.args.get("time_to")
        if time_from:
            conditions.append("ts >= ?")
            params.append(time_from)
        if time_to:
            conditions.append("ts <= ?")
            params.append(time_to)

        # Event type filter (comma-separated)
        types = request.args.get("type")
        if types:
            type_list = [t.strip() for t in types.split(",") if t.strip()]
            if type_list:
                placeholders = ",".join("?" * len(type_list))
                conditions.append(f"type IN ({placeholders})")
                params.extend(type_list)

        # Source IP filter (prefix match)
        src_ip = request.args.get("src_ip")
        if src_ip:
            conditions.append("src_ip LIKE ?")
            params.append(f"{src_ip}%")

        # Severity filter
        severity = request.args.get("severity")
        if severity:
            sev_list = [s.strip() for s in severity.split(",") if s.strip()]
            if sev_list:
                placeholders = ",".join("?" * len(sev_list))
                conditions.append(f"severity IN ({placeholders})")
                params.extend(sev_list)

        # Global search
        q = request.args.get("q")
        if q:
            conditions.append(
                "(payload LIKE ? OR src_ip LIKE ? OR facility LIKE ? "
                "OR severity LIKE ? OR oid LIKE ? OR tags LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like] * 6)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        # 1. Group by type
        type_rows = db.execute(
            f"SELECT type, COUNT(*) as count FROM events {where} GROUP BY type",
            params
        ).fetchall()
        types_res = {r["type"]: r["count"] for r in type_rows}
        for t in ("syslog", "snmptrap", "webhook"):
            types_res.setdefault(t, 0)

        # 2. Group by severity
        sev_rows = db.execute(
            f"SELECT severity, COUNT(*) as count FROM events {where} GROUP BY severity",
            params
        ).fetchall()
        severities_res = {r["severity"] if r["severity"] else "none": r["count"] for r in sev_rows}

        # 3. Top source IPs
        ip_rows = db.execute(
            f"SELECT src_ip, COUNT(*) as count FROM events {where} GROUP BY src_ip ORDER BY count DESC LIMIT 10",
            params
        ).fetchall()
        top_ips_res = [{"ip": r["src_ip"] if r["src_ip"] else "unknown", "count": r["count"]} for r in ip_rows]

        # 4. Timeline buckets (daily vs hourly)
        span_row = db.execute(
            f"SELECT MIN(ts), MAX(ts) FROM events {where}",
            params
        ).fetchone()

        scale = "hour"
        if span_row and span_row[0] and span_row[1]:
            try:
                min_t = datetime.fromisoformat(span_row[0].rstrip("Z").replace(" ", "T"))
                max_t = datetime.fromisoformat(span_row[1].rstrip("Z").replace(" ", "T"))
                diff_days = (max_t - min_t).days
                if diff_days > 3:
                    scale = "day"
            except ValueError:
                pass

        if scale == "day":
            strftime_clause = "strftime('%Y-%m-%d', ts)"
        else:
            strftime_clause = "strftime('%Y-%m-%dT%H:00:00', ts)"

        timeline_rows = db.execute(
            f"SELECT {strftime_clause} as bucket, COUNT(*) as count FROM events {where} GROUP BY bucket ORDER BY bucket ASC",
            params
        ).fetchall()
        timeline_res = [{"time": r["bucket"], "count": r["count"]} for r in timeline_rows]

        return jsonify({
            "types": types_res,
            "severities": severities_res,
            "top_ips": top_ips_res,
            "timeline": timeline_res,
            "timeline_scale": scale
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

    return app
