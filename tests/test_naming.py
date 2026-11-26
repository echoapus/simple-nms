#!/usr/bin/env python3
"""Naming validation — display name "Simple Event Collector", technical name "sieco"."""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_support import check, run_suite

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_NAMES = re.compile(r"simple[-_]nms|simplenms|Simple NMS", re.IGNORECASE)
# The GitHub repo keeps its old name until it is renamed there.
REPO_LINES = ("github.com/echoapus/simple-nms", "cd simple-nms", "~/simple-nms/")
# The installer's migration block names the old install on purpose.
SKIP_FILES = {"scripts/install.sh", "tests/test_naming.py"}


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=PROJECT,
                         capture_output=True, text=True, check=True).stdout.split("\n")
    return [f for f in out if f and os.path.exists(os.path.join(PROJECT, f))]


def read(rel):
    with open(os.path.join(PROJECT, rel), encoding="utf-8") as f:
        return f.read()


def test_no_old_names_left():
    print("\n=== No old names left ===")
    leftovers = []
    for rel in tracked_files():
        if rel in SKIP_FILES:
            continue
        try:
            lines = read(rel).splitlines()
        except UnicodeDecodeError:
            continue  # binary file
        for number, line in enumerate(lines, 1):
            if OLD_NAMES.search(line) and not any(marker in line for marker in REPO_LINES):
                leftovers.append(f"{rel}:{number}")
    check("no 'Simple NMS' / simple-nms / simplenms outside repo URLs", not leftovers, ", ".join(leftovers[:10]))


def test_display_name():
    print("\n=== Display name ===")
    for rel in ("README.md", "README.zh-TW.md", "src/sieco/static/index.html", "deploy/sieco.service"):
        check(f"{rel} uses 'Simple Event Collector'", "Simple Event Collector" in read(rel))
    check("page title", "<title>Simple Event Collector</title>" in read("src/sieco/static/index.html"))
    check("README no longer says 'Network Management System'",
          "Network Management System" not in read("README.md"))


def test_technical_name():
    print("\n=== Technical name ===")
    check("package folder is src/sieco", os.path.isfile(os.path.join(PROJECT, "src/sieco/main.py")))
    check("old package folder is gone", not os.path.exists(os.path.join(PROJECT, "src/simplenms/main.py")))
    check("service file is deploy/sieco.service", os.path.isfile(os.path.join(PROJECT, "deploy/sieco.service")))
    check("old service file is gone", not os.path.exists(os.path.join(PROJECT, "deploy/simple-nms.service")))

    service = read("deploy/sieco.service")
    for line in ("User=sieco", "Group=sieco", "WorkingDirectory=/opt/sieco", "SyslogIdentifier=sieco",
                 "ReadWritePaths=/opt/sieco/data"):
        check(f"service has {line}", line in service)

    cargo = read("rust-syslog/Cargo.toml")
    check("Cargo package and binary are sieco-syslog", cargo.count('name = "sieco-syslog"') == 2)
    check("Cargo.lock matches", 'name = "sieco-syslog"' in read("rust-syslog/Cargo.lock"))

    rust_py = read("src/sieco/collectors/rust_syslog.py")
    check("Python collector looks for the sieco-syslog binary", rust_py.count("sieco-syslog") >= 2)


def test_installer_and_uninstaller_agree():
    print("\n=== Installer / uninstaller ===")
    install, uninstall = read("scripts/install.sh"), read("scripts/uninstall.sh")
    for text in ("/opt/sieco", "useradd -r -m -d /opt/sieco -s /usr/sbin/nologin sieco",
                 "systemctl enable sieco", "systemctl restart sieco", "/etc/systemd/system/sieco.service",
                 "sieco-syslog"):
        check(f"install.sh has '{text}'", text in install)
    for text in ("systemctl disable --now sieco", "/etc/systemd/system/sieco.service", "userdel sieco",
                 "rm -rf /opt/sieco"):
        check(f"uninstall.sh has '{text}'", text in uninstall)


def test_default_community_is_consistent():
    print("\n=== Default SNMP community ===")
    config = read("config.example.json")
    check("config.example.json community is sieco", '"community": "sieco"' in config)
    check("snmp collector default is sieco", 'community: str = "sieco"' in read("src/sieco/collectors/snmp_listener.py"))
    check("web API default is sieco", '"community", "sieco"' in read("src/sieco/web_app.py"))
    check("installer default is sieco", "'community', 'sieco'" in read("scripts/install.sh"))


if __name__ == "__main__":
    raise SystemExit(run_suite("Simple Event Collector -- Naming Validation", [
        test_no_old_names_left,
        test_display_name,
        test_technical_name,
        test_installer_and_uninstaller_agree,
        test_default_community_is_consistent,
    ]))
