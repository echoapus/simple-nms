"""Syslog collector — listens on UDP and parses RFC 3164 and RFC 5424 messages."""

import json
import logging
import queue
import re
import socket
import ssl
import threading
from datetime import datetime, timezone

from metrics import runtime_metrics

logger = logging.getLogger(__name__)

# RFC 3164 PRI field: <priority>
_PRI_RE = re.compile(r"^<(\d{1,3})>(.*)$", re.DOTALL)

# RFC 5424 format pattern
_RFC5424_RE = re.compile(
    r"^(\d+)\s+"                 # VERSION (Group 1)
    r"(\S+)\s+"                  # TIMESTAMP (Group 2)
    r"(\S+)\s+"                  # HOSTNAME (Group 3)
    r"(\S+)\s+"                  # APP-NAME (Group 4)
    r"(\S+)\s+"                  # PROCID (Group 5)
    r"(\S+)\s+"                  # MSGID (Group 6)
    r"((?:\[.+?\])+|-)"          # STRUCTURED-DATA (Group 7)
    r"(?:\s+(.*))?$",            # MSG (Group 8)
    re.DOTALL
)

FACILITY_NAMES = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "ntp", "audit", "alert", "clock",
    "local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7",
]

SEVERITY_NAMES = [
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug",
]

MAX_TLS_MESSAGE_SIZE = 1024 * 1024


def _parse_pri(pri_val: int):
    """Return (facility_name, severity_name) from a PRI integer."""
    facility_idx = pri_val >> 3
    severity_idx = pri_val & 0x07
    facility = FACILITY_NAMES[facility_idx] if facility_idx < len(FACILITY_NAMES) else str(facility_idx)
    severity = SEVERITY_NAMES[severity_idx] if severity_idx < len(SEVERITY_NAMES) else str(severity_idx)
    return facility, severity


def _parse_structured_data(sd_str: str) -> dict:
    """Parse RFC 5424 structured data string into a dictionary."""
    if sd_str == "-":
        return {}
    elements = re.findall(r"\[([^\]]+)\]", sd_str)
    result = {}
    for elem in elements:
        parts = elem.split(None, 1)
        if not parts:
            continue
        sdid = parts[0]
        params = {}
        if len(parts) > 1:
            kv_pairs = re.findall(r'(\S+?)="([^"]*?)"', parts[1])
            for k, v in kv_pairs:
                params[k] = v
        result[sdid] = params
    return result


def _parse_syslog(data: bytes, addr: tuple) -> dict:
    """Parse raw syslog datagram into an event dict."""
    text = data.decode("utf-8", errors="replace").rstrip("\n")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")

    facility = None
    severity = None
    message = text
    varbinds = None
    tags = None
    event_ts = now

    m = _PRI_RE.match(text)
    if m:
        pri = int(m.group(1))
        facility, severity = _parse_pri(pri)
        message = m.group(2).strip()

        # Check if the remaining message is in RFC 5424 format
        m5424 = _RFC5424_RE.match(message)
        if m5424:
            ts_str = m5424.group(2)
            hostname = m5424.group(3)
            app_name = m5424.group(4)
            procid = m5424.group(5)
            msgid = m5424.group(6)
            sd_str = m5424.group(7)
            msg = m5424.group(8)

            # Try parsing timestamp
            if ts_str and ts_str != "-":
                try:
                    # ISO 8601 formatting replacement (e.g. trailing 'Z' to '+00:00')
                    cutoff = ts_str
                    if cutoff.endswith("Z"):
                        cutoff = cutoff[:-1] + "+00:00"
                    parsed_dt = datetime.fromisoformat(cutoff)
                    event_ts = parsed_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
                except ValueError:
                    pass

            # Structured Data -> varbinds
            sd_dict = _parse_structured_data(sd_str)
            if sd_dict:
                varbinds = json.dumps(sd_dict, ensure_ascii=False)

            # Headers -> tags
            tag_list = []
            if app_name and app_name != "-":
                tag_list.append(f"app:{app_name}")
            if msgid and msgid != "-":
                tag_list.append(f"msgid:{msgid}")
            if hostname and hostname != "-":
                tag_list.append(f"host:{hostname}")
            if tag_list:
                tags = ",".join(tag_list)

            # Format payload with header details
            prefix_parts = []
            if app_name and app_name != "-":
                if procid and procid != "-":
                    prefix_parts.append(f"{app_name}[{procid}]")
                else:
                    prefix_parts.append(app_name)
            if msgid and msgid != "-":
                prefix_parts.append(msgid)

            prefix = ": ".join(prefix_parts)

            if msg:
                message = f"{prefix}: {msg}" if prefix else msg
            else:
                if sd_dict:
                    message = f"{prefix}: [Structured Data] {sd_str}" if prefix else f"[Structured Data] {sd_str}"
                else:
                    message = f"{prefix} (no message)" if prefix else "(no message)"

    return {
        "ts": event_ts,
        "src_ip": addr[0],
        "type": "syslog",
        "facility": facility,
        "severity": severity,
        "oid": None,
        "varbinds": varbinds,
        "payload": message,
        "tags": tags,
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


class SyslogTLSCollector(threading.Thread):
    """RFC 5425 Syslog-over-TLS listener using RFC 6587 octet-counting frames."""

    def __init__(self, write_queue: "queue.Queue[dict]", host: str, port: int,
                 certfile: str, keyfile: str, cafile: str | None = None,
                 require_client_cert: bool = False):
        super().__init__(daemon=True, name="syslog-tls-collector")
        self.q = write_queue
        self.host, self.port = host, port
        self.certfile, self.keyfile, self.cafile = certfile, keyfile, cafile
        self.require_client_cert = require_client_cert
        self.ready = threading.Event()
        self.start_error: str | None = None
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certfile, self.keyfile)
        if self.require_client_cert:
            if not self.cafile:
                raise ValueError("cafile is required when client certificates are enabled")
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(self.cafile)
        return context

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < size:
            data = conn.recv(size - len(chunks))
            if not data:
                return None
            chunks.extend(data)
        return bytes(chunks)

    def _read_frame(self, conn: socket.socket) -> bytes | None:
        first = conn.recv(1)
        if not first:
            return None
        if not first.isdigit():
            # Some appliances send RFC 5424 over TLS with newline framing.
            # Accept it for interoperability while preferring RFC 6587 below.
            message = bytearray(first)
            while len(message) <= MAX_TLS_MESSAGE_SIZE:
                char = conn.recv(1)
                if not char:
                    return bytes(message) if message else None
                if char == b"\n":
                    return bytes(message)
                message.extend(char)
            raise ValueError("newline-delimited syslog message exceeds maximum size")

        header = bytearray(first)
        while len(header) <= 10:
            char = conn.recv(1)
            if not char:
                return None
            if char == b" ":
                break
            if not char.isdigit():
                raise ValueError("RFC 6587 frame length must be decimal")
            header.extend(char)
        else:
            raise ValueError("RFC 6587 frame length is too long")
        if not header:
            raise ValueError("RFC 6587 frame has no length")
        length = int(header)
        if length > MAX_TLS_MESSAGE_SIZE:
            raise ValueError("RFC 6587 frame exceeds maximum message size")
        return self._recv_exact(conn, length)

    def _handle_client(self, conn: socket.socket, addr: tuple) -> None:
        with self._lock:
            self._clients.add(conn)
        try:
            while not self._stop_event.is_set():
                frame = self._read_frame(conn)
                if frame is None:
                    return
                evt = _parse_syslog(frame, addr)
                try:
                    self.q.put_nowait(evt)
                except queue.Full:
                    runtime_metrics.inc_dropped("syslog")
                    logger.warning("TLS syslog event from %s dropped because write queue is full", addr[0])
        except (OSError, ssl.SSLError, ValueError) as exc:
            if not self._stop_event.is_set():
                logger.warning("TLS syslog client %s disconnected: %s", addr[0], exc)
        finally:
            with self._lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def run(self) -> None:
        try:
            context = self._context()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.settimeout(1.0)
            self._sock.bind((self.host, self.port))
            self._sock.listen()
        except Exception as exc:
            self.start_error = str(exc)
            self.ready.set()
            logger.error("TLS syslog collector could not start: %s", exc)
            return

        self.ready.set()
        logger.info("TLS syslog collector listening on %s:%d/tcp", self.host, self.port)
        while not self._stop_event.is_set():
            try:
                raw_conn, addr = self._sock.accept()
                with self._lock:
                    self._clients.add(raw_conn)
                try:
                    conn = context.wrap_socket(raw_conn, server_side=True)
                except (OSError, ValueError, ssl.SSLError) as exc:
                    with self._lock:
                        self._clients.discard(raw_conn)
                    try:
                        raw_conn.close()
                    except OSError:
                        pass
                    if not self._stop_event.is_set():
                        logger.warning("TLS syslog handshake from %s failed: %s", addr[0], exc)
                    continue
                with self._lock:
                    self._clients.discard(raw_conn)
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock:
            self._sock.close()
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
