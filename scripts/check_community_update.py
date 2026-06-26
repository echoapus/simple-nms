#!/usr/bin/env python3
"""Check whether WebUI SNMP community updates take effect without restart."""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1", help="Simple NMS WebUI base URL")
    parser.add_argument("--trap-host", default="127.0.0.1", help="SNMP trap receiver host")
    parser.add_argument("--trap-port", type=int, default=162, help="SNMP trap receiver UDP port")
    parser.add_argument("--community", default=f"check-{int(time.time())}", help="temporary community to set")
    parser.add_argument("--restore", action="store_true", help="restore the previous community before exit")
    parser.add_argument("--wait", type=float, default=1.0, help="seconds to wait after updating community")
    args = parser.parse_args()

    if not shutil.which("snmptrap"):
        print("FAIL: missing snmptrap CLI. Install net-snmp/snmp, then rerun.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")

    def api_json(path, data=None):
        body = None
        headers = {}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base_url + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    old_community = api_json("/api/config").get("community")
    marker = f"simplenms-community-check-{int(time.time())}"

    try:
        api_json("/api/config", {"community": args.community})
        time.sleep(args.wait)
        subprocess.run(
            [
                "snmptrap",
                "-v2c",
                "-c",
                args.community,
                f"{args.trap_host}:{args.trap_port}",
                "",
                "1.3.6.1.6.3.1.1.5.3",
                "1.3.6.1.2.1.1.1.0",
                "s",
                marker,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        q = urllib.parse.urlencode({
            "type": "snmptrap",
            "q": marker,
            "per_page": 5,
            "sort": "ts",
            "order": "desc",
        })
        for _ in range(10):
            data = api_json(f"/api/events?{q}")
            found = any(marker in (event.get("payload") or "") or marker in (event.get("varbinds") or "")
                        for event in data.get("events", []))
            if found:
                print(f"PASS: community update is active ({args.community})")
                return 0
            time.sleep(0.5)

        print("FAIL: trap sent with updated community was not found in /api/events", file=sys.stderr)
        return 1
    finally:
        if args.restore and old_community:
            api_json("/api/config", {"community": old_community})


if __name__ == "__main__":
    raise SystemExit(main())
