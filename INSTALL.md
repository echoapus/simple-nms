# Installation Guide

## Prerequisites

- **Python 3.9+** (Debian 12 ships with 3.11)
- **pip** (Python package manager)
- No external database, web server, or message broker required

## Option A: Bare-Metal Install (Debian 12 / Ubuntu 22.04)

### 1. Create a dedicated user

```bash
sudo useradd -r -m -d /opt/simple-nms -s /usr/sbin/nologin simplenms
```

### 2. Deploy the application

```bash
sudo mkdir -p /opt/simple-nms
sudo cp -r ./* /opt/simple-nms/
sudo chown -R simplenms:simplenms /opt/simple-nms
```

### 3. Install Python dependencies

```bash
cd /opt/simple-nms
sudo -u simplenms pip install --user -r requirements.txt
```

Or system-wide:

```bash
sudo pip install -r requirements.txt --break-system-packages
```

Dependencies installed:
| Package | Purpose |
|---------|---------|
| `flask` | Web server (API + Webhook + UI) |
| `werkzeug` | WSGI server (Flask dependency) |
| `pysnmp` | SNMP Trap receiver |

Standard library (no install needed): `sqlite3`, `socket`, `queue`, `threading`, `json`.

### 4. Configure

Edit `/opt/simple-nms/config.json`:

```json
{
    "database": {
        "path": "data/events.db",
        "wal_mode": true
    },
    "writer": {
        "batch_size": 100,
        "flush_interval_ms": 200
    },
    "syslog": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 514
    },
    "snmptrap": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 162
    },
    "webhook": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 80
    }
}
```

If Simple NMS is behind HAProxy on the same host, bind the web server to loopback and use a non-public backend port:

```json
    "webhook": {
        "enabled": true,
        "host": "127.0.0.1",
        "port": 5000
    }
```

### 5. Create data directory

```bash
sudo -u simplenms mkdir -p /opt/simple-nms/data
```

### 6. Install systemd service

```bash
sudo cp deploy/simple-nms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable simple-nms
sudo systemctl start simple-nms
```

The systemd unit uses `AmbientCapabilities=CAP_NET_BIND_SERVICE` so the service can bind to ports 80, 162, and 514 without running as root.

### 7. Verify

```bash
# Check service status
sudo systemctl status simple-nms

# Check logs
sudo journalctl -u simple-nms -f

# Test web UI
curl -s http://localhost/health
```

Open `http://your-server` in a browser to see the dashboard.

### 8. Data retention (optional)

Set up a daily cron job to purge old events:

```bash
sudo crontab -u simplenms -e
```

Add:

```cron
0 3 * * * cd /opt/simple-nms && python3 cleanup.py --days 30 >> /var/log/simple-nms-cleanup.log 2>&1
```

### 9. Firewall

If using UFW:

```bash
sudo ufw allow 80/tcp     # Web UI + Webhook
sudo ufw allow 514/udp    # Syslog
sudo ufw allow 162/udp    # SNMP Trap
```

For production, restrict UDP ports to your internal network:

```bash
sudo ufw allow from 10.0.0.0/8 to any port 514 proto udp
sudo ufw allow from 10.0.0.0/8 to any port 162 proto udp
```

### 10. Local HAProxy reverse proxy (optional)

When HAProxy runs on the same host, expose HAProxy on port 80/443 and forward to the loopback Simple NMS backend:

```haproxy
frontend http_in
    bind *:80
    mode http
    default_backend simple_nms

backend simple_nms
    mode http
    option forwardfor
    http-request set-header X-Forwarded-Proto http
    server simple_nms_1 127.0.0.1:5000 check
```

Simple NMS trusts `X-Forwarded-For` and `X-Real-IP` only when the immediate peer is loopback. This supports local HAProxy while preventing direct clients from spoofing webhook `src_ip`.

---

## Option B: Docker

### 1. Build and run

```bash
# Edit config.json first if you need custom ports or MIB paths
docker compose up -d
```

### 2. Verify

```bash
docker compose ps
docker compose logs -f simple-nms
curl -s http://localhost/health
```

The Compose file grants `NET_BIND_SERVICE` so the non-root container user can bind to ports 80, 162, and 514 inside the container.

### 3. Data persistence

The SQLite database is stored in a Docker volume `nms-data`. To back up:

```bash
docker compose exec simple-nms cp /app/data/events.db /app/data/events.db.bak
docker cp simple-nms:/app/data/events.db.bak ./backup/
```

### 4. Data retention in Docker

```bash
docker compose exec simple-nms python3 cleanup.py --days 30
```

---

## Manual Start (development / testing)

For testing on non-privileged ports without root:

```bash
# Edit config.json: set ports to 8080 (webhook), 5514 (syslog), 1162 (snmptrap)
python3 main.py config.json
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Permission denied` on port 80/514/162 | Use systemd service (has `AmbientCapabilities`), or run `sudo setcap cap_net_bind_service=+ep $(which python3)` |
| `Address already in use` on port 80 | Stop nginx/apache: `sudo systemctl stop nginx` |
| `ModuleNotFoundError: pysnmp` | Run `pip install pysnmp --break-system-packages` |
| Events not appearing in UI | Check browser console for SSE errors; verify with `curl http://localhost/api/kpi` |
| Database locked errors | Ensure WAL mode is enabled in config (`"wal_mode": true`) |
| Fallback file created at `data/events_fallback.jsonl` | DB was temporarily unreachable; events saved to file — investigate disk space/permissions |
