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

echo "Cleaning up downloaded MIB files..."
MIB_DIR="/usr/share/snmp/mibs"
COMMON_MIBS=(
    "IANA-ADDRESS-FAMILY-NUMBERS-MIB"
    "MTA-MIB"
    "SMUX-MIB"
    "TCP-MIB"
    "IANA-LANGUAGE-MIB"
    "NET-SNMP-AGENT-MIB"
    "SNMP-COMMUNITY-MIB"
    "TRANSPORT-ADDRESS-MIB"
    "AGENTX-MIB"
    "IANA-RTPROTO-MIB"
    "NET-SNMP-EXAMPLES-MIB"
    "SNMP-FRAMEWORK-MIB"
    "TUNNEL-MIB"
    "DISMAN-EVENT-MIB"
    "IANAifType-MIB"
    "NET-SNMP-EXTEND-MIB"
    "SNMP-MPD-MIB"
    "UCD-DEMO-MIB"
    "DISMAN-EXPRESSION-MIB"
    "IF-INVERTED-STACK-MIB"
    "NET-SNMP-MIB"
    "SNMP-NOTIFICATION-MIB"
    "UCD-DISKIO-MIB"
    "DISMAN-NSLOOKUP-MIB"
    "IF-MIB"
    "NET-SNMP-MONITOR-MIB"
    "SNMP-PROXY-MIB"
    "UCD-DLMOD-MIB"
    "DISMAN-PING-MIB"
    "INET-ADDRESS-MIB"
    "NET-SNMP-PASS-MIB"
    "SNMP-TARGET-MIB"
    "UCD-IPFILTER-MIB"
    "DISMAN-SCHEDULE-MIB"
    "IP-FORWARD-MIB"
    "NET-SNMP-SYSTEM-MIB"
    "SNMP-USER-BASED-SM-MIB"
    "UCD-IPFWACC-MIB"
    "DISMAN-SCRIPT-MIB"
    "IP-MIB"
    "NET-SNMP-TC"
    "SNMP-USM-AES-MIB"
    "UCD-SNMP-MIB-OLD"
    "DISMAN-TRACEROUTE-MIB"
    "IPV6-FLOW-LABEL-MIB"
    "NET-SNMP-VACM-MIB"
    "SNMP-USM-DH-OBJECTS-MIB"
    "UCD-SNMP-MIB"
    "EtherLike-MIB"
    "IPV6-ICMP-MIB"
    "NETWORK-SERVICES-MIB"
    "SNMP-VIEW-BASED-ACM-MIB"
    "UDP-MIB"
    "IPV6-MIB"
    "NOTIFICATION-LOG-MIB"
    "SNMPv2-CONF"
    "idsmib"
    "IPV6-TC"
    "RFC-1215"
    "SNMPv2-MIB"
    "radwaremib"
    "HCNUM-TC"
    "IPV6-TCP-MIB"
    "RFC1155-SMI"
    "SNMPv2-SMI"
    "HOST-RESOURCES-MIB"
    "IPV6-UDP-MIB"
    "RFC1213-MIB"
    "SNMPv2-TC"
    "HOST-RESOURCES-TYPES"
    "LM-SENSORS-MIB"
    "RMON-MIB"
    "SNMPv2-TM"
)
for mib in "${COMMON_MIBS[@]}"; do
    rm -f "${MIB_DIR}/${mib}.txt"
done
if [ -d "$MIB_DIR" ] && [ -z "$(ls -A "$MIB_DIR" 2>/dev/null)" ]; then
    rmdir "$MIB_DIR" 2>/dev/null || true
fi

echo "Simple NMS removed."
