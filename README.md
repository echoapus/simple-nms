# Simple NMS

Lightweight Network Management System that collects **Syslog**, **SNMP Trap**, and **Webhook** events into a single SQLite database, with a real-time web dashboard.

## Features

- **Three event collectors** running in parallel threads:
  - Syslog (UDP 514) — RFC 3164 PRI parsing for facility/severity
  - SNMP Trap (UDP 162) — via pysnmp, varbinds stored as JSON
  - Webhook (HTTP POST `/webhook`) — JSON ingestion
- **SQLite storage** with WAL mode for concurrent writes, batched inserts (~5,000+ events/sec)
- **Real-time web dashboard** on port 80:
  - KPI cards (total / syslog / snmptrap / webhook counts)
  - Filterable event table with column sorting
  - Global search with 300ms debounce
  - Time range selector (5min / 1hr / today / custom)
  - Event type and source IP filters
  - Dark/light theme toggle
  - Responsive layout (mobile-friendly)
  - Server-Sent Events (SSE) for live updates
- **Single Python process** — no external web server, message broker, or database server required
- **Reliability** — write failures trigger exponential back-off retry with JSONL file fallback

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
