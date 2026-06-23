# User Guide

## Generating Test Events

### Syslog

Send test syslog messages using `logger` (installed by default on Linux):

```bash
# Send to local NMS
logger -n 127.0.0.1 -P 514 --udp -p local0.info "Test syslog: link up on eth0"
logger -n 127.0.0.1 -P 514 --udp -p local0.err "Test syslog: BGP peer 10.0.0.1 down"
logger -n 127.0.0.1 -P 514 --udp -p auth.warning "Failed SSH login from 203.0.113.42"
```

Or with raw UDP using Python:

```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(b'<134>router01 BGP: peer 10.0.0.1 established', ('127.0.0.1', 514))
s.sendto(b'<131>switch01 SNMP: auth failure from 192.168.1.100', ('127.0.0.1', 514))
s.close()
print('Sent 2 syslog messages')
"
```

Or send an RFC 5424 formatted message with structured data:

```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
msg = b'<165>1 2003-10-11T22:14:15.003Z mymachine.example.com evtsys 1234 ID47 [exampleSDID@32473 iut=\"3\" eventSource=\"Application\" eventID=\"1011\"] An application event log entry...'
s.sendto(msg, ('127.0.0.1', 514))
s.close()
print('Sent RFC 5424 syslog message')
"
```

### SNMP Trap

Using `snmptrap` (install: `sudo apt install snmp`):

```bash
# SNMPv2c linkDown trap
snmptrap -v2c -c public 127.0.0.1:162 '' \
    1.3.6.1.6.3.1.1.5.3 \
    1.3.6.1.2.1.2.2.1.1 i 3

# SNMPv2c custom trap
snmptrap -v2c -c public 127.0.0.1:162 '' \
    1.3.6.1.4.1.99999 \
    1.3.6.1.4.1.99999.1 s "CPU utilization 95%"
```

### Webhook

Using `curl`:

```bash
# Deployment event
curl -X POST http://localhost/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"deploy","service":"web-api","severity":"info","message":"v2.3.1 deployed"}'

# Alert event
curl -X POST http://localhost/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"alert","service":"db-primary","severity":"crit","message":"disk usage 95%","tags":"ops,critical"}'

# Resolution event
curl -X POST http://localhost/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"resolve","service":"db-primary","severity":"info","message":"disk cleaned up"}'
```

---

## Web UI

Open `http://your-server` in a browser.

### KPI Cards

Four cards at the top show total event counts, broken down by type (Syslog / SNMP Trap / Webhook). These update automatically via SSE when new events arrive.

### Event Table

Displays the most recent events with columns:
- **Time** — UTC timestamp
- **Source IP** — origin address of the event
- **Type** — color-coded badge (blue=syslog, amber=snmptrap, green=webhook)
- **Severity** — dot indicator with color (green=info, yellow=warning, orange=err, red=crit)
- **Message** — first 100 characters of payload

### View Switcher Tabs

Toggle the main page panel view:
- **Live Feed** — Displays the standard Event Table list.
- **Analytics** — Renders interactive charts powered by Chart.js, including:
  - **Event Volume Timeline**: Displays log frequency (auto-scales between hourly and daily buckets).
  - **Event Types**: Doughnut chart breaking down event ingestion shares.
  - **Severity Levels**: Horizontal bar chart outlining severity breakdowns.
  - **Top Sources**: Bar chart presenting the top 10 most active reporting source IPs.
  *Note: Charts automatically react to active sidebar filter parameters and update in real-time.*

### Sorting

Click any column header to sort. Click again to toggle ascending/descending. The sort indicator (▲/▼) shows the current direction. Your sort preference is saved in the browser and persists across sessions.

### Global Search

The search box in the header searches across payload, source IP, facility, severity, OID, and tags. Results update after a 300ms pause (debounce) to avoid excessive queries while typing.

### Sidebar Filters

- **Time Range** — Quick buttons (5 min / 1 hour / Today / All) or custom date range picker
- **Event Type** — Checkboxes to show/hide Syslog, SNMP Trap, Webhook
- **Source IP** — Text input for prefix matching (e.g., `10.0.0` matches all IPs starting with that prefix)
- **Clear All Filters** — Reset all filters to default
- **Clear Old Events** — Opens a confirmation dialog. Select a date to permanently delete events before that date.

On mobile (screen width < 768px), the sidebar collapses into a drawer menu — tap the ☰ button in the header to open it.

### Theme

Click the ☀/☽ button in the header to toggle between dark and light themes. Your preference is saved in the browser.

---

## REST API

### GET /api/events

Query events with filters and pagination.

Parameters:
| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | int | Page number (default: 1) |
| `per_page` | int | Results per page (default: 50, max: 200) |
| `sort` | string | Sort column: `ts`, `src_ip`, `type`, `severity`, `id` (default: `ts`) |
| `order` | string | `asc` or `desc` (default: `desc`) |
| `type` | string | Comma-separated: `syslog`, `snmptrap`, `webhook` |
| `src_ip` | string | Source IP prefix match |
| `severity` | string | Comma-separated severity names |
| `time_from` | string | ISO 8601 timestamp (inclusive) |
| `time_to` | string | ISO 8601 timestamp (inclusive) |
| `q` | string | Full-text search across payload, src_ip, facility, severity, oid, tags |

Example:

```bash
# Recent syslog errors
curl "http://localhost/api/events?type=syslog&severity=err,crit&sort=ts&order=desc"

# Search for BGP-related events
curl "http://localhost/api/events?q=BGP"

# Events from a specific IP in the last hour
curl "http://localhost/api/events?src_ip=10.0.0.1&time_from=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S)"
```

### GET /api/kpi

Aggregate event counts.

Parameters:
| Parameter | Type | Description |
|-----------|------|-------------|
| `time_from` | string | ISO 8601 timestamp |
| `time_to` | string | ISO 8601 timestamp |

Response:

```json
{
  "total": 1685,
  "syslog": 1284,
  "snmptrap": 312,
  "webhook": 89
}
```

### GET /api/analytics

Query aggregate event distributions and timelines for visualization charts. It accepts the same filter parameters as `GET /api/events` (e.g., `time_from`, `time_to`, `type`, `src_ip`, `severity`, and `q`).

Response structure:
```json
{
  "types": {
    "syslog": 3,
    "snmptrap": 2,
    "webhook": 2
  },
  "severities": {
    "info": 4,
    "err": 1,
    "warning": 1,
    "crit": 1
  },
  "top_ips": [
    { "ip": "10.0.0.1", "count": 2 },
    { "ip": "127.0.0.1", "count": 2 }
  ],
  "timeline": [
    { "time": "2026-06-17T12:00:00", "count": 3 },
    { "time": "2026-06-17T13:00:00", "count": 4 }
  ],
  "timeline_scale": "hour"
}
```

### GET /api/sse

Server-Sent Events stream. Connect from JavaScript:

```javascript
const es = new EventSource('/api/sse');
es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    console.log('New event:', event);
};
```

Each SSE message is a JSON object with the same fields as the events table.

### POST /webhook

Submit a webhook event as JSON.

```bash
curl -X POST http://localhost/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"test","severity":"info","message":"hello"}'
```

Response: `202 Accepted` with `{"status": "ok"}`

When Simple NMS is behind a local reverse proxy such as HAProxy, webhook `src_ip` is taken from the first valid IP in `X-Forwarded-For`, falling back to `X-Real-IP` and then the socket peer IP. Forwarded IP headers are trusted only when the immediate peer is loopback. Direct clients can still post to `/webhook`, but their forged forwarding headers are ignored.

### GET /health

Health check endpoint.

```bash
curl http://localhost/health
# {"status": "healthy", "sse_clients": 2}
```

### POST /api/events/cleanup

Delete events older than a selected cutoff timestamp. This is used by the Web UI cleanup confirmation dialog.

```bash
curl -X POST http://localhost/api/events/cleanup \
  -H "Content-Type: application/json" \
  -d '{"before_ts":"2026-01-01T00:00:00.000"}'
```

---

## Data Retention

Use the cleanup script to purge old events:

```bash
# Preview what would be deleted
python3 cleanup.py --days 30 --dry-run

# Delete events older than 30 days
python3 cleanup.py --days 30

# Delete events older than 7 days
python3 cleanup.py --days 7
```

---

## Configuring Network Devices

### Syslog Forwarding

**Cisco IOS:**
```
logging host 10.0.0.100 transport udp port 514
logging trap informational
```

**Juniper Junos:**
```
set system syslog host 10.0.0.100 any info
set system syslog host 10.0.0.100 port 514
```

**Linux rsyslog:**
```
# /etc/rsyslog.d/50-nms.conf
*.* @10.0.0.100:514
```

### SNMP Trap Destination

**Cisco IOS:**
```
snmp-server host 10.0.0.100 version 2c public
snmp-server enable traps
```

**Juniper Junos:**
```
set snmp trap-group nms-traps targets 10.0.0.100
set snmp trap-group nms-traps version v2
```

### Webhook Integration

Any system that supports outgoing webhooks can POST JSON to `http://your-server/webhook`. Compatible with Grafana, Prometheus Alertmanager, Zabbix, and custom scripts.

For local HAProxy deployments, enable forwarding headers:

```haproxy
backend simple_nms
    mode http
    option forwardfor
    http-request set-header X-Forwarded-Proto http
    server simple_nms_1 127.0.0.1:5000 check
```
