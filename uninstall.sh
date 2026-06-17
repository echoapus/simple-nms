#!/bin/bash
# ===========================================================================
# Simple NMS — Automated Uninstallation Script
# Cleans up systemd services, users, and installation files
# ===========================================================================

set -e

# ANSI Color Codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${RED}====================================================${NC}"
echo -e "${RED}       Simple NMS Automated Uninstaller v1.0       ${NC}"
echo -e "${RED}====================================================${NC}"

# 1. Check if run as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run as root. Try 'sudo ./uninstall.sh'.${NC}"
    exit 1
fi

# 2. Stop and Disable Systemd Service
echo -e "\n${YELLOW}[1/4] Stopping and disabling simple-nms service...${NC}"
if systemctl is-active --quiet simple-nms; then
    systemctl stop simple-nms
    echo "Stopped simple-nms service."
fi
if systemctl is-enabled --quiet simple-nms 2>/dev/null; then
    systemctl disable simple-nms
    echo "Disabled simple-nms service."
fi

# 3. Remove Systemd Unit File
echo -e "\n${YELLOW}[2/4] Removing systemd configuration...${NC}"
if [ -f /etc/systemd/system/simple-nms.service ]; then
    rm -f /etc/systemd/system/simple-nms.service
    systemctl daemon-reload
    echo "Removed systemd service file."
fi

# 4. Delete System User
echo -e "\n${YELLOW}[3/4] Removing system user 'simplenms'...${NC}"
if id -u simplenms >/dev/null 2>&1; then
    userdel simplenms || true
    echo "Removed user 'simplenms'."
fi

# 5. Remove Application Directories & Logs
echo -e "\n${YELLOW}[4/4] Purging installation directory and logs...${NC}"
if [ -d /opt/simple-nms ]; then
    rm -rf /opt/simple-nms
    echo "Purged /opt/simple-nms directory."
fi
if [ -f /var/log/simple-nms-cleanup.log ]; then
    rm -f /var/log/simple-nms-cleanup.log
    echo "Removed cleanup cron logs."
fi

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}      Uninstallation Completed Successfully!        ${NC}"
echo -e "${GREEN}====================================================${NC}"
echo -e "Simple NMS has been completely removed from your system."
echo -e "===================================================="
