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

# Terminate any remaining processes running as simplenms to prevent userdel from failing
echo "Terminating any orphaned processes running as simplenms..."
pkill -u simplenms 2>/dev/null || true
sleep 0.5
pkill -9 -u simplenms 2>/dev/null || true

userdel simplenms 2>/dev/null || true
rm -rf /opt/simple-nms
rm -f /var/log/simple-nms-cleanup.log

echo "=================================================================="
echo "🎉 Simple NMS has been successfully removed!"
echo "=================================================================="
echo "💡 Note: If you added a manual cron job for cleanup.py,"
echo "   please remember to remove it from your crontab (crontab -e)."
echo "=================================================================="
