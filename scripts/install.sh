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
MIB_DIR="/opt/simple-nms/data/mibs"
mkdir -p "$MIB_DIR"
if [ -f "$PROJECT_ROOT/resources/mibs.tar.gz" ]; then
    tar -xzf "$PROJECT_ROOT/resources/mibs.tar.gz" --strip-components=1 -C "$MIB_DIR"
    echo "MIB files extracted to $MIB_DIR"
    
    # Ensure legacy SMIv1 dependency shims exist to prevent compilation errors for private MIBs
    if [ ! -f "$MIB_DIR/RFC-1212.txt" ] && [ ! -f "$MIB_DIR/RFC-1212" ]; then
        echo "Creating RFC-1212.txt shim..."
        printf "RFC-1212 DEFINITIONS ::= BEGIN\nEND\n" > "$MIB_DIR/RFC-1212.txt"
    fi
    if [ ! -f "$MIB_DIR/RFC-1215.txt" ] && [ ! -f "$MIB_DIR/RFC-1215" ]; then
        echo "Creating RFC-1215.txt shim..."
        printf "RFC-1215 DEFINITIONS ::= BEGIN\nEND\n" > "$MIB_DIR/RFC-1215.txt"
    fi
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

# Configure default SNMP Trap community if not already set in config.json
echo "Configuring SNMP Trap community..."
USER_COMMUNITY="simplenms"
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
if 'community' not in data['snmptrap']:
    data['snmptrap']['community'] = '$USER_COMMUNITY'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
"

echo "Installing Python dependencies..."
python3 -m venv /opt/simple-nms/venv
/opt/simple-nms/venv/bin/pip install --upgrade pip
/opt/simple-nms/venv/bin/pip install -r /opt/simple-nms/requirements.txt

test -f "$PROJECT_ROOT/deploy/simple-nms.service" || { echo "Error: deploy/simple-nms.service not found"; exit 1; }
sed 's|/usr/bin/python3|/opt/simple-nms/venv/bin/python3|g' "$PROJECT_ROOT/deploy/simple-nms.service" > /etc/systemd/system/simple-nms.service

chown root:root /etc/systemd/system/simple-nms.service
chmod 644 /etc/systemd/system/simple-nms.service

echo "Configuring secure permissions (least privilege)..."
# 1. Base directory and code files owned by root, group simplenms
chown -R root:simplenms /opt/simple-nms

# 2. Set directories to 750 (rwxr-x---) and files to 640 (rw-r-----) for the code
find /opt/simple-nms -type d -exec chmod 750 {} +
find /opt/simple-nms -type f -exec chmod 640 {} +

# 3. Ensure entrypoints and venv binaries are executable by the group
chmod 750 /opt/simple-nms/main.py /opt/simple-nms/cleanup.py
find /opt/simple-nms/venv/bin -type f -exec chmod 750 {} +

# 4. Make the persistent data directory fully owned and writable by the simplenms user
chown -R simplenms:simplenms /opt/simple-nms/data
find /opt/simple-nms/data -type d -exec chmod 770 {} +
find /opt/simple-nms/data -type f -exec chmod 660 {} +

# Detect port conflicts before starting the service
echo "Checking for port conflicts..."
CFG_FILE="/opt/simple-nms/config.json"
read -r WEB_PORT SYSLOG_PORT SNMP_PORT < <(python3 -c "import json; cfg = json.load(open('$CFG_FILE')); print(cfg.get('webhook', {}).get('port', 80), cfg.get('syslog', {}).get('port', 514), cfg.get('snmptrap', {}).get('port', 162))" 2>/dev/null || echo "80 514 162")

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
echo "   /opt/simple-nms/data/mibs"
echo "   Then restart the service to apply: sudo systemctl restart simple-nms"
echo "=================================================================="
