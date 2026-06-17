#!/bin/bash
# ===========================================================================
# Simple NMS — Automated Installation Script
# Supports: Debian 12+, Ubuntu 22.04+, RHEL/Rocky 9+
# ===========================================================================

set -e

# ANSI Color Codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}====================================================${NC}"
echo -e "${BLUE}       Simple NMS Automated Installer v1.0         ${NC}"
echo -e "${BLUE}====================================================${NC}"

# 1. Check if run as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run as root. Try 'sudo ./install.sh'.${NC}"
    exit 1
fi

# 2. Detect Package Manager and Install Prerequisites
echo -e "\n${YELLOW}[1/6] Installing system prerequisites...${NC}"
if [ -f /etc/debian_version ]; then
    echo "Detected Debian/Ubuntu system."
    apt-get update
    apt-get install -y python3 python3-pip python3-venv sqlite3
elif [ -f /etc/redhat-release ]; then
    echo "Detected RHEL/Rocky Linux system."
    dnf install -y python3 python3-pip sqlite
else
    echo -e "${YELLOW}Warning: Unknown OS distribution. Please ensure python3, pip, and venv are installed.${NC}"
fi

# 3. Create System User and Group
echo -e "\n${YELLOW}[2/6] Configuring system user 'simplenms'...${NC}"
if ! id -u simplenms >/dev/null 2>&1; then
    useradd -r -m -d /opt/simple-nms -s /usr/sbin/nologin simplenms
    echo "Created user 'simplenms' with home directory '/opt/simple-nms'."
else
    echo "User 'simplenms' already exists."
fi

# 4. Copy Application Files
echo -e "\n${YELLOW}[3/6] Deploying application files to /opt/simple-nms...${NC}"
mkdir -p /opt/simple-nms/data

# Copy code files, collectors module, and static directory
cp -r database.py web_app.py main.py cleanup.py metrics.py config.json requirements.txt /opt/simple-nms/
cp -r collectors /opt/simple-nms/
cp -r static /opt/simple-nms/

# 5. Configure Python Virtual Environment & Install Requirements
echo -e "\n${YELLOW}[4/6] Creating Python virtual environment and installing dependencies...${NC}"
python3 -m venv /opt/simple-nms/venv
/opt/simple-nms/venv/bin/pip install --upgrade pip
/opt/simple-nms/venv/bin/pip install -r /opt/simple-nms/requirements.txt

# 6. Configure Systemd Service Unit
echo -e "\n${YELLOW}[5/6] Setting up systemd service unit...${NC}"
SERVICE_FILE="/etc/systemd/system/simple-nms.service"
if [ -f deploy/simple-nms.service ]; then
    # Copy template and replace system python interpreter with virtualenv interpreter
    sed 's|/usr/bin/python3|/opt/simple-nms/venv/bin/python3|g' deploy/simple-nms.service > "$SERVICE_FILE"
    echo "Configured systemd service unit at $SERVICE_FILE using virtualenv python."
else
    echo -e "${RED}Error: Service unit template deploy/simple-nms.service not found!${NC}"
    exit 1
fi

# Set proper ownership
chown -R simplenms:simplenms /opt/simple-nms
chmod -R 750 /opt/simple-nms
chmod -R 770 /opt/simple-nms/data

# 7. Enable and Start simple-nms Service
echo -e "\n${YELLOW}[6/6] Reloading systemd and starting service...${NC}"
systemctl daemon-reload
systemctl enable simple-nms
systemctl start simple-nms

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}      Installation Completed Successfully!          ${NC}"
echo -e "${GREEN}====================================================${NC}"
echo -e "Simple NMS has been installed to ${BLUE}/opt/simple-nms${NC}."
echo -e "Web UI is active and listening on port ${BLUE}80${NC} (configured in config.json)."
echo -e "\n${YELLOW}Useful commands:${NC}"
echo -e "  - Check status:  ${BLUE}sudo systemctl status simple-nms${NC}"
echo -e "  - View live logs: ${BLUE}sudo journalctl -u simple-nms -f${NC}"
echo -e "  - Restart:       ${BLUE}sudo systemctl restart simple-nms${NC}"
echo -e "  - Retention cron: ${BLUE}sudo crontab -u simplenms -e${NC}"
echo -e "    Add: ${YELLOW}0 3 * * * cd /opt/simple-nms && /opt/simple-nms/venv/bin/python3 cleanup.py --days 30 >> /var/log/simple-nms-cleanup.log 2>&1${NC}"
echo -e "===================================================="
