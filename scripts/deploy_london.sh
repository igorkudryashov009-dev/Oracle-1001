#!/usr/bin/env bash
# ==============================================================================
# ORACLE-1001 · Primary AIS Ingestion Proxy & Scraper Node Deployment
# Target Host: Node LD8 (London, UK)
# Primary IP:   185.39.19.75
# Secondary IP: 185.39.19.231
# Role:         VesselFinder Premium Scraping, Session Auth & Fleet Ingestion
# ==============================================================================
set -euo pipefail

NODE_NAME="LD8-London"
PRIMARY_IP="185.39.19.75"
SECONDARY_IP="185.39.19.231"
KOROLEV_IP="45.8.230.214"
APP_ROOT="/opt/oracle1001/vesselfinder_scraper"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> [${NODE_NAME}] Initializing AIS Ingestion & Scraper Deployment..."
echo "    Primary IP   : ${PRIMARY_IP}"
echo "    Secondary IP : ${SECONDARY_IP}"
echo "    Target Dir   : ${APP_ROOT}"

# 1. Directory Structure
mkdir -p "${APP_ROOT}"/{bin,config,data,output,logs,state}
mkdir -p /var/log/oracle1001

# 2. System Packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl jq ufw rsync

# 3. Python Virtual Environment
if [[ ! -d "${APP_ROOT}/venv" ]]; then
    python3 -m venv "${APP_ROOT}/venv"
fi
# shellcheck disable=SC1091
source "${APP_ROOT}/venv/bin/activate"
pip install --upgrade pip wheel
pip install "pandas>=2.0.0" "requests>=2.31.0" "beautifulsoup4>=4.12.0" \
            "python-dotenv>=1.0.0" "PyYAML>=6.0"

# 4. Copy Ingestion Engine & Configurations
install -m 0755 "${SRC_DIR}/scripts/vesselfinder_ingest.py" "${APP_ROOT}/bin/vesselfinder_ingest.py"

if [[ -f "${SRC_DIR}/config/vesselfinder_cookies.json" ]]; then
    install -m 0600 "${SRC_DIR}/config/vesselfinder_cookies.json" "${APP_ROOT}/config/vesselfinder_cookies.json"
    echo "    Installed VesselFinder Premium session cookies (mode 600)."
else
    echo "    WARNING: config/vesselfinder_cookies.json not found in source repository."
fi

if [[ -f "${SRC_DIR}/output/fleet_database.csv" ]]; then
    install -m 0644 "${SRC_DIR}/output/fleet_database.csv" "${APP_ROOT}/data/fleet_database.csv"
fi
if [[ -f "${SRC_DIR}/features/budget_ledger.json" ]]; then
    mkdir -p "${APP_ROOT}/features"
    install -m 0644 "${SRC_DIR}/features/budget_ledger.json" "${APP_ROOT}/features/budget_ledger.json"
fi

# 5. Network Interface & Secondary IP Routing (if configured)
IFACE="$(ip -o route get 1.1.1.1 2>/dev/null | awk '{print $5}' || echo 'eth0')"
if ! ip addr show dev "${IFACE}" | grep -q "${SECONDARY_IP}"; then
    echo "    Attaching secondary egress IP ${SECONDARY_IP} to interface ${IFACE}..."
    ip addr add "${SECONDARY_IP}/32" dev "${IFACE}" label "${IFACE}:1" || true
fi

# 6. Egress Synchronization Script (London -> Korolyov)
cat > "${APP_ROOT}/bin/sync_to_korolev.sh" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="/opt/oracle1001/vesselfinder_scraper"
KOROLEV_IP="45.8.230.214"
KOROLEV_DIR="/opt/oracle1001/analytical_engine/data"

if [[ -f "${APP_ROOT}/output/fleet_database.csv" ]]; then
    echo "[$(date -u)] Syncing updated fleet database to Korolyov (${KOROLEV_IP})..."
    rsync -avz -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
        "${APP_ROOT}/output/fleet_database.csv" \
        "${APP_ROOT}/output/vesselfinder_myfleet_500."* \
        "root@${KOROLEV_IP}:${KOROLEV_DIR}/" || echo "Remote sync skipped or offline."
fi
EOF
chmod 0755 "${APP_ROOT}/bin/sync_to_korolev.sh"

# 7. Systemd Service & Timer
cat > /etc/systemd/system/vesselfinder-scraper.service << EOF
[Unit]
Description=Oracle-1001 VesselFinder Premium Scraper & Ingest (Node LD8)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=${APP_ROOT}
ExecStart=${APP_ROOT}/venv/bin/python ${APP_ROOT}/bin/vesselfinder_ingest.py --limit 500 --live --sync-my-fleet
ExecStartPost=-${APP_ROOT}/bin/sync_to_korolev.sh
StandardOutput=append:/var/log/oracle1001/vesselfinder_scraper.log
StandardError=append:/var/log/oracle1001/vesselfinder_scraper.err
Restart=no

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/vesselfinder-scraper.timer << 'EOF'
[Unit]
Description=Run VesselFinder Premium Scraper periodically
After=network.target

[Timer]
OnBootSec=5min
OnUnitActiveSec=3h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable vesselfinder-scraper.timer
systemctl restart vesselfinder-scraper.timer

# 8. Firewall Configuration (UFW)
ufw allow 22/tcp || true
ufw default allow outgoing || true
ufw --force enable || true

# 9. Verification Dry-Run
echo "==> Running verification check on ${NODE_NAME}..."
"${APP_ROOT}/venv/bin/python" "${APP_ROOT}/bin/vesselfinder_ingest.py" --limit 3 --dry-run --export-my-fleet

echo "==> [${NODE_NAME}] Deployment complete! Service and timer active."
systemctl list-timers 'vesselfinder-scraper.*' --no-pager || true
