"""Rust UDP Syslog collector with a local datagram bridge to the Python writer."""

import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time

from metrics import runtime_metrics

logger = logging.getLogger(__name__)


class RustSyslogCollector:
    """Runs the Rust UDP receiver and feeds its normalized events into ``write_queue``."""

    def __init__(self, write_queue: "queue.Queue[dict]", host: str = "0.0.0.0", port: int = 514,
                 deny_ips=None, runtime_dir: str = "data", binary: str | None = None):
        self.q = write_queue
        self.host = host
        self.port = port
        self.deny_ips = set(deny_ips or [])
        self.runtime_dir = runtime_dir
        self.binary = binary or self._default_binary()
        self._event_path = os.path.join(runtime_dir, "rust-syslog-events.sock")
        self._control_path = os.path.join(runtime_dir, "rust-syslog-control.sock")
        self._receiver = None
        self._receiver_thread = None
        self._process = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self.start_error = None

    @staticmethod
    def _default_binary() -> str:
        installed = os.path.join(os.path.dirname(os.path.dirname(__file__)), "simplenms-syslog")
        if os.path.isfile(installed):
            return installed
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        return os.path.join(project_root, "rust-syslog", "target", "release", "simplenms-syslog")

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def start(self) -> None:
        os.makedirs(self.runtime_dir, exist_ok=True)
        self._unlink(self._event_path)
        self._unlink(self._control_path)
        self._receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._receiver.bind(self._event_path)
        self._receiver.settimeout(0.2)
        self._receiver_thread = threading.Thread(target=self._receive, daemon=True, name="rust-syslog-bridge")
        self._receiver_thread.start()
        command = [self.binary, "--host", self.host, "--port", str(self.port), "--socket", self._event_path,
                   "--control-socket", self._control_path]
        command.extend(value for ip in sorted(self.deny_ips) for value in ("--deny-ip", ip))
        try:
            self._process = subprocess.Popen(command)
        except OSError as exc:
            self.start_error = str(exc)
            logger.error("Rust syslog collector could not start: %s", exc)
            self.stop()
            return
        deadline = time.monotonic() + 2
        while not os.path.exists(self._control_path) and self._process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._process.poll() is not None:
            self.start_error = "Rust syslog collector exited during startup"
            logger.error("%s", self.start_error)
            self.stop()

    def _receive(self) -> None:
        while not self._stopping.is_set():
            try:
                payload = self._receiver.recv(262_144)
            except socket.timeout:
                continue
            except OSError:
                break
            self._enqueue(payload)

    def _enqueue(self, payload) -> None:
        required = {"ts", "src_ip", "type", "facility", "severity", "oid", "varbinds", "payload", "tags"}
        try:
            event = json.loads(payload)
            if not isinstance(event, dict) or event.get("type") != "syslog" or not required <= event.keys():
                raise ValueError("invalid event shape")
            bridge_dropped = event.pop("_bridge_dropped", 0)
            if isinstance(bridge_dropped, bool) or not isinstance(bridge_dropped, int) or bridge_dropped < 0:
                raise ValueError("invalid bridge drop count")
            if bridge_dropped:
                runtime_metrics.inc_dropped("syslog", bridge_dropped)
            self.q.put_nowait(event)
        except queue.Full:
            runtime_metrics.inc_dropped("syslog")
            logger.warning("Rust syslog event dropped because write queue is full")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Discarded invalid Rust syslog event: %s", exc)

    def update_deny_ips(self, deny_ips) -> None:
        self.deny_ips = set(deny_ips or [])
        if not os.path.exists(self._control_path):
            return
        try:
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sender.sendto("\n".join(sorted(self.deny_ips)).encode(), self._control_path)
            finally:
                sender.close()
        except OSError as exc:
            logger.warning("Failed to update Rust syslog denylist: %s", exc)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None
        if self._receiver:
            self._receiver.close()
        if self._receiver_thread:
            self._receiver_thread.join(timeout=1)
        self._unlink(self._event_path)
        self._unlink(self._control_path)
