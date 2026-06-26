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
cp "$PROJECT_ROOT"/config.json /opt/simple-nms/
cp "$PROJECT_ROOT"/requirements.txt /opt/simple-nms/
test -f "$PROJECT_ROOT"/resources/mibs.tar.gz && cp "$PROJECT_ROOT"/resources/mibs.tar.gz /opt/simple-nms/ || true

echo "Installing Python dependencies..."
python3 -m venv /opt/simple-nms/venv
/opt/simple-nms/venv/bin/pip install --upgrade pip
/opt/simple-nms/venv/bin/pip install -r /opt/simple-nms/requirements.txt

test -f "$PROJECT_ROOT/deploy/simple-nms.service" || { echo "Error: deploy/simple-nms.service not found"; exit 1; }
sed 's|/usr/bin/python3|/opt/simple-nms/venv/bin/python3|g' "$PROJECT_ROOT/deploy/simple-nms.service" > /etc/systemd/system/simple-nms.service

chown -R simplenms:simplenms /opt/simple-nms
chmod -R 750 /opt/simple-nms
chmod -R 770 /opt/simple-nms/data

echo "Starting simple-nms..."
systemctl daemon-reload
systemctl enable --now simple-nms

printf '%s\n' "Installed. Check with: systemctl status simple-nms" \
  "Logs: journalctl -u simple-nms -f" \
  "Retention is optional; see INSTALL.md."
