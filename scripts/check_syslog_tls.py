#!/usr/bin/env python3
"""Send one RFC 5425 event to a running Simple NMS instance and verify it."""

import argparse
import json
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1", help="Simple NMS Web UI URL")
    parser.add_argument("--tls-host", default="127.0.0.1", help="TLS Syslog listener host")
    parser.add_argument("--tls-port", type=int, default=6514, help="TLS Syslog listener port")
    parser.add_argument("--cafile", help="CA certificate to verify the server (omit for self-signed test certificates)")
    args = parser.parse_args()

    marker = f"simplenms-tls-check-{int(time.time())}"
    message = f"<165>1 2026-07-20T12:00:00Z tls-check app 1 ID47 - {marker}".encode()
    context = ssl.create_default_context(cafile=args.cafile) if args.cafile else ssl._create_unverified_context()
    try:
        with socket.create_connection((args.tls_host, args.tls_port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=args.tls_host) as conn:
                conn.sendall(str(len(message)).encode() + b" " + message)
    except OSError as exc:
        print(f"FAIL: could not send TLS syslog event: {exc}", file=sys.stderr)
        return 1

    base_url = args.base_url.rstrip("/")
    query = urllib.parse.urlencode({"type": "syslog", "q": marker, "per_page": 5})
    for _ in range(10):
        try:
            with urllib.request.urlopen(f"{base_url}/api/events?{query}", timeout=5) as response:
                events = json.loads(response.read()).get("events", [])
            if any(marker in (event.get("payload") or "") for event in events):
                print(f"PASS: RFC 5425 event received on {args.tls_host}:{args.tls_port}")
                return 0
        except OSError:
            pass
        time.sleep(0.5)
    print("FAIL: TLS syslog event was not found in /api/events", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
