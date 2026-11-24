#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root with sudo ./install.sh"
    exit 1
fi

RUST_BUILD_PACKAGES=()
RUST_PACKAGE_MANAGER=""

install_rust_toolchain() {
    if command -v cargo >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then
        return
    fi
    if [ -f /etc/debian_version ]; then
        for package in cargo rustc; do
            dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed || RUST_BUILD_PACKAGES+=("$package")
        done
        apt-get install -y cargo rustc
        RUST_PACKAGE_MANAGER="apt-get"
    elif [ -f /etc/redhat-release ]; then
        for package in cargo rust; do
            rpm -q "$package" >/dev/null 2>&1 || RUST_BUILD_PACKAGES+=("$package")
        done
        dnf install -y cargo rust
        RUST_PACKAGE_MANAGER="dnf"
    else
        echo "Error: Rust collector selected but cargo and rustc are not installed."
        exit 1
    fi
}

remove_rust_build_packages() {
    [ "${#RUST_BUILD_PACKAGES[@]}" -gt 0 ] || return
    read -r -p "Remove Rust build packages installed for this deployment (${RUST_BUILD_PACKAGES[*]})? [y/N]: " REMOVE_RUST || true
    case "$REMOVE_RUST" in
        [Yy]|[Yy][Ee][Ss])
            "$RUST_PACKAGE_MANAGER" remove -y "${RUST_BUILD_PACKAGES[@]}"
            ;;
        *)
            echo "Keeping Rust build packages."
            ;;
    esac
}

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

MIB_DIR="/opt/simple-nms/data/mibs"
TLS_DIR="/opt/simple-nms/data/tls"
mkdir -p "$MIB_DIR" "$TLS_DIR"
echo "Using $MIB_DIR for uploaded or system-installed MIB files."

echo "Deploying to /opt/simple-nms..."
mkdir -p /opt/simple-nms/data
cp -r "$PROJECT_ROOT"/src/simplenms/* /opt/simple-nms/
cp "$PROJECT_ROOT"/requirements.txt /opt/simple-nms/

# Preserve config.json if it already exists to prevent losing user settings
if [ -f /opt/simple-nms/config.json ]; then
    echo "Existing config.json found — preserving user settings."
else
    cp "$PROJECT_ROOT"/config.example.json /opt/simple-nms/config.json
fi

# Match the service's data-directory configuration override.
CFG_FILE=$(python3 -c "
import json
import os
path = '/opt/simple-nms/config.json'
with open(path, encoding='utf-8') as f:
    cfg = json.load(f)
data_dir = os.path.dirname(cfg.get('database', {}).get('path', 'data/events.db')) or 'data'
overlay = os.path.join('/opt/simple-nms', data_dir, 'config.json')
if os.path.exists(overlay) and overlay != path:
    try:
        with open(overlay, encoding='utf-8') as f:
            json.load(f)
        path = overlay
    except Exception:
        pass
print(path)
")
export CFG_FILE

# Resolve the effective database path, including overlays without a database setting.
DATABASE_PATH=$(python3 -c "
import json
import os
with open('/opt/simple-nms/config.json', encoding='utf-8') as f:
    base = json.load(f)
with open(os.environ['CFG_FILE'], encoding='utf-8') as f:
    cfg = json.load(f)
path = cfg.get('database', {}).get('path', base.get('database', {}).get('path', 'data/events.db'))
print(os.path.abspath(os.path.join('/opt/simple-nms', path)))
")
DATA_DIR=$(dirname "$DATABASE_PATH")
case "$DATA_DIR" in
    /|/opt/simple-nms)
        echo "Error: database must be stored in a dedicated data directory."
        exit 1
        ;;
esac
export DATA_DIR
mkdir -p "$DATA_DIR/mibs"

echo "Configuring UDP Syslog collector..."
DEFAULT_COLLECTOR=$(python3 -c "import json, os; print(json.load(open(os.environ['CFG_FILE'])).get('syslog', {}).get('collector', 'rust'))" 2>/dev/null || echo "rust")
read -r -p "UDP Syslog collector (rust/python) [$DEFAULT_COLLECTOR]: " USER_COLLECTOR || true
SYSLOG_COLLECTOR="${USER_COLLECTOR:-$DEFAULT_COLLECTOR}"
SYSLOG_COLLECTOR="${SYSLOG_COLLECTOR,,}"
case "$SYSLOG_COLLECTOR" in
    rust)
        install_rust_toolchain
        cargo build --release --manifest-path "$PROJECT_ROOT/rust-syslog/Cargo.toml"
        install -m 750 "$PROJECT_ROOT"/rust-syslog/target/release/simplenms-syslog /opt/simple-nms/simplenms-syslog.new
        mv -f /opt/simple-nms/simplenms-syslog.new /opt/simple-nms/simplenms-syslog
        cargo clean --manifest-path "$PROJECT_ROOT/rust-syslog/Cargo.toml"
        ;;
    python) ;;
    *)
        echo "Error: UDP Syslog collector must be rust or python."
        exit 1
        ;;
esac

echo "Configuring web portal port..."
DEFAULT_WEB_PORT=$(python3 -c "import json, os; print(json.load(open(os.environ['CFG_FILE'])).get('webhook', {}).get('port', 80))")
while true; do
    read -r -p "Web portal TCP port [$DEFAULT_WEB_PORT]: " USER_WEB_PORT || true
    WEB_PORT="${USER_WEB_PORT:-$DEFAULT_WEB_PORT}"
    if [[ "$WEB_PORT" =~ ^[0-9]{1,5}$ ]] && (( 10#$WEB_PORT >= 1 && 10#$WEB_PORT <= 65535 )); then
        WEB_PORT=$((10#$WEB_PORT))
        break
    fi
    echo "Error: web portal port must be a number from 1 to 65535."
    if [ ! -t 0 ]; then
        exit 1
    fi
done

# Configure SNMP Trap community
echo "Configuring SNMP Trap community..."
DEFAULT_COMMUNITY=$(python3 -c "import json, os; print(json.load(open(os.environ['CFG_FILE'])).get('snmptrap', {}).get('community', 'simplenms'))" 2>/dev/null || echo "simplenms")
read -r -p "SNMP Trap community [$DEFAULT_COMMUNITY]: " USER_COMMUNITY || true
USER_COMMUNITY="${USER_COMMUNITY:-$DEFAULT_COMMUNITY}"
WEB_PORT="$WEB_PORT" SNMP_COMMUNITY="$USER_COMMUNITY" SYSLOG_COLLECTOR="$SYSLOG_COLLECTOR" python3 -c "
import json
import os
path = os.environ['CFG_FILE']
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
if 'snmptrap' not in data:
    data['snmptrap'] = {}
data['snmptrap']['community'] = os.environ['SNMP_COMMUNITY']
if 'syslog' not in data:
    data['syslog'] = {}
data['syslog']['collector'] = os.environ['SYSLOG_COLLECTOR']
data.setdefault('webhook', {})['port'] = int(os.environ['WEB_PORT'])
if 'syslog_tls' not in data:
    data['syslog_tls'] = {
        'enabled': False,
        'host': '0.0.0.0',
        'port': 6514,
        'certfile': 'data/tls/server.crt',
        'keyfile': 'data/tls/server.key',
        'require_client_cert': False,
    }
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
"

echo "Installing Python dependencies..."
python3 -m venv /opt/simple-nms/venv
/opt/simple-nms/venv/bin/pip install --upgrade pip
/opt/simple-nms/venv/bin/pip install -r /opt/simple-nms/requirements.txt

test -f "$PROJECT_ROOT/deploy/simple-nms.service" || { echo "Error: deploy/simple-nms.service not found"; exit 1; }
sed 's|/usr/bin/python3|/opt/simple-nms/venv/bin/python3|g' "$PROJECT_ROOT/deploy/simple-nms.service" > /etc/systemd/system/simple-nms.service

# systemd accepts C-style quoted paths; double percent signs to avoid specifiers.
python3 -c '
import json
import os
print("\n[Service]\nReadWritePaths=" + json.dumps(os.environ["DATA_DIR"].replace("%", "%%"), ensure_ascii=False))
' >> /etc/systemd/system/simple-nms.service

chown root:root /etc/systemd/system/simple-nms.service
chmod 644 /etc/systemd/system/simple-nms.service

echo "Configuring secure permissions (least privilege)..."
# 1. Base directory and code files owned by root, group simplenms
chown -R root:simplenms /opt/simple-nms

# 2. Set directories to 750 (rwxr-x---) and files to 640 (rw-r-----) for the code
find /opt/simple-nms -type d -exec chmod 750 {} +
find /opt/simple-nms -type f -exec chmod 640 {} +

# 3. Ensure entrypoints and venv binaries are executable by the group
chmod 750 /opt/simple-nms/main.py
if [ "$SYSLOG_COLLECTOR" = "rust" ]; then
    chmod 750 /opt/simple-nms/simplenms-syslog
fi
find /opt/simple-nms/venv/bin -type f -exec chmod 750 {} +

# 4. Make the persistent data directory fully owned and writable by the simplenms user
chown -R simplenms:simplenms /opt/simple-nms/data
find /opt/simple-nms/data -type d -exec chmod 770 {} +
find /opt/simple-nms/data -type f -exec chmod 660 {} +
chmod 700 /opt/simple-nms/data/tls

# Grant access to custom database storage without changing unrelated files.
chown simplenms:simplenms "$DATA_DIR" "$DATA_DIR/mibs"
chmod 770 "$DATA_DIR" "$DATA_DIR/mibs"
for file in "$DATABASE_PATH" "$DATABASE_PATH-wal" "$DATABASE_PATH-shm" "$CFG_FILE"; do
    if [ -f "$file" ] && [[ "$file" == "$DATA_DIR/"* ]]; then
        chown simplenms:simplenms "$file"
        chmod 660 "$file"
    fi
done

# Detect port conflicts before starting the service
echo "Checking for port conflicts..."
read -r WEB_PORT SYSLOG_PORT SNMP_PORT TLS_ENABLED TLS_PORT < <(python3 -c "import json, os; cfg = json.load(open(os.environ['CFG_FILE'])); tls = cfg.get('syslog_tls', {}); print(cfg.get('webhook', {}).get('port', 80), cfg.get('syslog', {}).get('port', 514), cfg.get('snmptrap', {}).get('port', 162), int(tls.get('enabled', False)), tls.get('port', 6514))" 2>/dev/null || echo "80 514 162 0 6514")

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
    if [ "$TLS_ENABLED" = "1" ] && ss -tln | grep -q ":$TLS_PORT "; then
        echo "Warning: TCP port $TLS_PORT (Syslog TLS) is already in use."
    fi
fi

echo "Starting simple-nms..."
systemctl daemon-reload
systemctl enable simple-nms
systemctl restart simple-nms

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

remove_rust_build_packages
