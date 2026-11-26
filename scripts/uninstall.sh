#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root with sudo ./uninstall.sh"
    exit 1
fi

echo "Stopping sieco..."
systemctl disable --now sieco 2>/dev/null || true
rm -f /etc/systemd/system/sieco.service
systemctl daemon-reload

# Terminate any remaining processes running as sieco to prevent userdel from failing
echo "Terminating any orphaned processes running as sieco..."
pkill -u sieco 2>/dev/null || true
sleep 0.5
pkill -9 -u sieco 2>/dev/null || true

userdel sieco 2>/dev/null || true
rm -rf /opt/sieco

echo "=================================================================="
echo "Simple Event Collector has been successfully removed!"
echo "=================================================================="
