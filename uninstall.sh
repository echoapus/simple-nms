#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root with sudo ./uninstall.sh"
    exit 1
fi

echo "Stopping simple-nms..."
systemctl disable --now simple-nms 2>/dev/null || true
rm -f /etc/systemd/system/simple-nms.service
systemctl daemon-reload
userdel simplenms 2>/dev/null || true
rm -rf /opt/simple-nms
rm -f /var/log/simple-nms-cleanup.log

echo "Simple NMS removed."
