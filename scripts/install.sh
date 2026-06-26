#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root with sudo ./install.sh"
    exit 1
fi

echo "Installing system prerequisites..."
if [ -f /etc/debian_version ]; then
    apt-get update
    apt-get install -y python3 python3-pip python3-venv sqlite3
    # Attempt to install snmp-mibs-downloader (requires non-free/multiverse on some systems)
    apt-get install -y snmp-mibs-downloader || echo "Warning: snmp-mibs-downloader package not available or failed to install"
elif [ -f /etc/redhat-release ]; then
    dnf install -y python3 python3-pip sqlite net-snmp
else
    echo "Warning: unknown distribution; python3, pip, and venv must already be installed"
fi

# Resolve the project root directory relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Configuring simplenms user..."
if ! id -u simplenms >/dev/null 2>&1; then
    useradd -r -m -d /opt/simple-nms -s /usr/sbin/nologin simplenms
fi

echo "Extracting MIB files from mibs.tar.gz..."
MIB_DIR="/usr/share/snmp/mibs"
mkdir -p "$MIB_DIR"
if [ -f "$PROJECT_ROOT/resources/mibs.tar.gz" ]; then
    tar -xzf "$PROJECT_ROOT/resources/mibs.tar.gz" --strip-components=1 -C "$MIB_DIR"
    echo "MIB files extracted to $MIB_DIR"
else
    echo "Warning: mibs.tar.gz not found in resources directory. Skipping extraction."
fi

echo "Deploying to /opt/simple-nms..."
mkdir -p /opt/simple-nms/data
cp -r "$PROJECT_ROOT"/src/simplenms/* /opt/simple-nms/
cp "$PROJECT_ROOT"/cleanup.py /opt/simple-nms/
cp "$PROJECT_ROOT"/requirements.txt /opt/simple-nms/
test -f "$PROJECT_ROOT"/resources/mibs.tar.gz && cp "$PROJECT_ROOT"/resources/mibs.tar.gz /opt/simple-nms/ || true

# Preserve config.json if it already exists to prevent losing user settings
if [ -f /opt/simple-nms/config.json ]; then
    echo "Existing config.json found — preserving user settings."
else
    cp "$PROJECT_ROOT"/config.json /opt/simple-nms/
fi

# Prompt user for SNMP community
echo "Configuring SNMP Trap community..."
if [ -t 0 ]; then
    read -p "Enter SNMP community string for traps [simplenms]: " USER_COMMUNITY
    USER_COMMUNITY=${USER_COMMUNITY:-simplenms}
else
    USER_COMMUNITY="simplenms"
    echo "Non-interactive shell detected. Using default community: $USER_COMMUNITY"
fi

# Write community to config.json
python3 -c "
import json
path = '/opt/simple-nms/config.json'
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
if 'snmptrap' not in data:
    data['snmptrap'] = {}
data['snmptrap']['community'] = '$USER_COMMUNITY'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
"
echo "SNMP community configured as: $USER_COMMUNITY"

echo "Installing Python dependencies..."
python3 -m venv /opt/simple-nms/venv
/opt/simple-nms/venv/bin/pip install --upgrade pip
/opt/simple-nms/venv/bin/pip install -r /opt/simple-nms/requirements.txt

test -f "$PROJECT_ROOT/deploy/simple-nms.service" || { echo "Error: deploy/simple-nms.service not found"; exit 1; }
sed 's|/usr/bin/python3|/opt/simple-nms/venv/bin/python3|g' "$PROJECT_ROOT/deploy/simple-nms.service" > /etc/systemd/system/simple-nms.service

chown -R simplenms:simplenms /opt/simple-nms
chmod -R 750 /opt/simple-nms
chmod -R 770 /opt/simple-nms/data

# Detect port conflicts before starting the service
echo "Checking for port conflicts..."
CFG_FILE="/opt/simple-nms/config.json"
WEB_PORT=$(python3 -c "import json; cfg = json.load(open('$CFG_FILE')); print(cfg.get('webhook', {}).get('port', 80))" 2>/dev/null || echo 80)
SYSLOG_PORT=$(python3 -c "import json; cfg = json.load(open('$CFG_FILE')); print(cfg.get('syslog', {}).get('port', 514))" 2>/dev/null || echo 514)
SNMP_PORT=$(python3 -c "import json; cfg = json.load(open('$CFG_FILE')); print(cfg.get('snmptrap', {}).get('port', 162))" 2>/dev/null || echo 162)

if command -v ss >/dev/null 2>&1; then
    if ss -tln | grep -q ":$WEB_PORT "; then
        echo "Warning: TCP port $WEB_PORT (Web/Webhook) is already in use by another process."
    fi
    if ss -uln | grep -q ":$SYSLOG_PORT "; then
        echo "Warning: UDP port $SYSLOG_PORT (Syslog) is already in use. You may need to stop rsyslog/syslog-ng."
    fi
    if ss -uln | grep -q ":$SNMP_PORT "; then
        echo "Warning: UDP port $SNMP_PORT (SNMP Trap) is already in use. You may need to stop the system snmpd."
    fi
fi

echo "Starting simple-nms..."
systemctl daemon-reload
systemctl enable --now simple-nms

# Retrieve primary local IP address for user convenience
PRIMARY_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || ip route get 1.1.1.1 | awk '{print $7}' 2>/dev/null || echo "localhost")
if [ -z "$PRIMARY_IP" ]; then
    PRIMARY_IP="localhost"
fi

echo "=================================================================="
echo "🎉 Simple NMS installed successfully!"
echo "=================================================================="
echo "👉 Web UI URL:       http://$PRIMARY_IP:$WEB_PORT"
echo "👉 Check Status:     systemctl status simple-nms"
echo "👉 View Logs:        journalctl -u simple-nms -f"
echo "------------------------------------------------------------------"
echo "💡 Custom MIBs Guide:"
echo "   Place your custom MIB files (.txt/.mib/.my) in:"
echo "   /usr/share/snmp/mibs"
echo "   Then restart the service to apply: sudo systemctl restart simple-nms"
echo "=================================================================="
