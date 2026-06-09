"""Syslog collector — listens on UDP and parses RFC 3164 messages."""

import logging
import queue
import re
import socket
import threading
from datetime import datetime, timezone

from metrics import runtime_metrics

logger = logging.getLogger(__name__)

# RFC 3164 PRI field: <priority>
_PRI_RE = re.compile(r"^<(\d{1,3})>(.*)$", re.DOTALL)

FACILITY_NAMES = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "ntp", "audit", "alert", "clock",
    "local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7",
]

SEVERITY_NAMES = [
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug",
]


def _parse_pri(pri_val: int):
    """Return (facility_name, severity_name) from a PRI integer."""
    facility_idx = pri_val >> 3
    severity_idx = pri_val & 0x07
    facility = FACILITY_NAMES[facility_idx] if facility_idx < len(FACILITY_NAMES) else str(facility_idx)
    severity = SEVERITY_NAMES[severity_idx] if severity_idx < len(SEVERITY_NAMES) else str(severity_idx)
    return facility, severity


def _parse_syslog(data: bytes, addr: tuple) -> dict:
    """Parse raw syslog datagram into an event dict."""
    text = data.decode("utf-8", errors="replace").rstrip("\n")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")

    facility = None
    severity = None
    message = text

    m = _PRI_RE.match(text)
    if m:
        pri = int(m.group(1))
        facility, severity = _parse_pri(pri)
        message = m.group(2).strip()

    return {
        "ts": now,
        "src_ip": addr[0],
        "type": "syslog",
        "facility": facility,
        "severity": severity,
        "oid": None,
        "varbinds": None,
        "payload": message,
        "tags": None,
    }


class SyslogCollector(threading.Thread):
    """UDP syslog listener that pushes parsed events onto the write queue."""

    def __init__(self, write_queue: "queue.Queue[dict]", host: str = "0.0.0.0", port: int = 514):
        super().__init__(daemon=True, name="syslog-collector")
        self.q = write_queue
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None

    def run(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind((self.host, self.port))
        logger.info("Syslog collector listening on %s:%d/udp", self.host, self.port)

        while True:
            try:
                data, addr = self._sock.recvfrom(65535)
                if data:
                    evt = _parse_syslog(data, addr)
                    try:
                        self.q.put_nowait(evt)
                    except queue.Full:
                        runtime_metrics.inc_dropped("syslog")
                        logger.warning("Syslog event from %s dropped because write queue is full", addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self) -> None:
        if self._sock:
            self._sock.close()
