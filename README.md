# Simple NMS

Lightweight Network Management System that collects **Syslog**, **SNMP Trap**, and **Webhook** events into a single SQLite database, with a real-time web dashboard.

## Features

- **Three event collectors** running in parallel threads:
  - Syslog (UDP 514) — RFC 3164 and RFC 5424 parsing (structured data to JSON, header metadata as tags)
  - SNMP Trap (UDP 162) — via pysnmp, varbinds stored as JSON
  - Webhook (HTTP POST `/webhook`) — JSON ingestion
- **SQLite storage** with WAL mode for concurrent writes, batched inserts (~5,000+ events/sec)
- **Real-time web dashboard** on port 80:
  - KPI cards (total / syslog / snmptrap / webhook counts)
  - Interactive Tabs: **Live Feed** and **Analytics Dashboard** (Event timeline, type distribution, severity breakdown, top source IPs)
  - Filterable event table with column sorting
  - Global search with 300ms debounce
  - Time range selector (5min / 1hr / today / custom)
  - Event type and source IP filters
  - Dark/light theme toggle
  - Responsive layout (mobile-friendly)
  - Server-Sent Events (SSE) for live updates
- **Single Python process** — no external web server, message broker, or database server required
- **Reliability** — write failures are logged and tracked via dropped metrics, with fallback JSONL file capability
- **Reverse-proxy aware webhooks** — direct clients use the socket peer IP; requests forwarded by a local proxy can use `X-Forwarded-For` / `X-Real-IP` for the original client IP

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start (uses config.json in current directory)
sudo python3 main.py

# Or with a custom config
sudo python3 main.py /path/to/config.json
```

Open `http://your-server` in a browser.

## Reverse Proxy / HAProxy

Simple NMS can run directly or behind a local reverse proxy such as HAProxy.
When HAProxy runs on the same host, configure it to forward requests to the Simple NMS web port and add `X-Forwarded-For`:

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

With this setup, set `webhook.host` to `127.0.0.1` and `webhook.port` to `5000`.
Webhook events posted to `/webhook` will record the first valid IP from `X-Forwarded-For`.
Forwarded client IP headers are trusted only when the immediate peer is loopback, so direct clients cannot spoof `src_ip` by sending their own forwarding headers.

## Architecture

```
┌─────────────┐  ┌──────────────┐  ┌──────────────┐
│ Syslog :514 │  │ SNMP Trap    │  │ Webhook :80  │
│ (UDP)       │  │ :162 (UDP)   │  │ (HTTP POST)  │
└──────┬──────┘  └──────┬───────┘  └──────┬───────┘
       │                │                  │
       └────────┬───────┴──────────────────┘
                │
         ┌──────▼──────┐
         │ Write Queue │  (thread-safe queue, 50k max)
         └──────┬──────┘
                │
         ┌──────▼──────┐
         │  DB Writer  │  (batch INSERT, WAL mode)
         │  + SSE push │
         └──────┬──────┘
                │
         ┌──────▼──────┐     ┌──────────────┐
         │   SQLite    │     │   Web UI     │
         │  events.db  │◄────│  REST API    │
         └─────────────┘     │  SSE stream  │
                             └──────────────┘
```

## Documentation

- [INSTALL.md](INSTALL.md) — Installation and deployment guide
- [USER.md](USER.md) — Usage guide with test examples
- [README.zh-TW.md](README.zh-TW.md) — Traditional Chinese project overview

## License

MIT
