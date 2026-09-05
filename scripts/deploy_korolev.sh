#!/usr/bin/env bash
# ==============================================================================
# ORACLE-1001 · Analytical Engine, DB Replica & Web Server Deployment
# Target Host: Node Rucloud (Королёв, РФ)
# Public IP:   45.8.230.214
# Web Port:    8765
# Role:        High-Performance BI Analytics, Dashboards & Fleet DB Replica
# ==============================================================================
set -euo pipefail

NODE_NAME="Rucloud-Korolev"
NODE_IP="45.8.230.214"
HTTP_PORT="8765"
APP_ROOT="/opt/oracle1001/analytical_engine"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> [${NODE_NAME}] Initializing Analytical Engine & Web Server Deployment..."
echo "    Host IP   : ${NODE_IP}"
echo "    Web Port  : ${HTTP_PORT}"
echo "    App Root  : ${APP_ROOT}"

# 1. Directory Structure
mkdir -p "${APP_ROOT}"/{bin,data,output,output/js,web,logs,features}
mkdir -p /var/log/oracle1001

# 2. System Packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl jq ufw nginx rsync

# 3. Python Virtual Environment
if [[ ! -d "${APP_ROOT}/venv" ]]; then
    python3 -m venv "${APP_ROOT}/venv"
fi
# shellcheck disable=SC1091
source "${APP_ROOT}/venv/bin/activate"
pip install --upgrade pip wheel
pip install "pandas>=2.0.0" "openpyxl>=3.1.0" "PyYAML>=6.0" \
            "python-dotenv>=1.0.0" "requests>=2.31.0"

# 4. Copy Engine Files, Dashboards & Static Assets
cp -a "${SRC_DIR}/run_server.py" "${APP_ROOT}/run_server.py"
cp -a "${SRC_DIR}/run_all.py" "${APP_ROOT}/run_all.py"
cp -a "${SRC_DIR}/config.yaml" "${APP_ROOT}/config.yaml"
cp -a "${SRC_DIR}/output/"* "${APP_ROOT}/output/" 2>/dev/null || true
cp -a "${SRC_DIR}/web/"* "${APP_ROOT}/web/" 2>/dev/null || true
if [[ -d "${SRC_DIR}/pipeline" ]]; then
    cp -a "${SRC_DIR}/pipeline" "${APP_ROOT}/"
fi
if [[ -d "${SRC_DIR}/features" ]]; then
    cp -a "${SRC_DIR}/features" "${APP_ROOT}/"
fi
if [[ -f "${SRC_DIR}/features/budget_ledger.json" ]]; then
    cp -a "${SRC_DIR}/features/budget_ledger.json" "${APP_ROOT}/features/budget_ledger.json"
fi

# 5. Systemd Service for Oracle-1001 Web Server
cat > /etc/systemd/system/oracle1001-server.service << EOF
[Unit]
Description=Oracle-1001 Analytical Web Server (Port ${HTTP_PORT})
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_ROOT}
ExecStart=${APP_ROOT}/venv/bin/python ${APP_ROOT}/run_server.py --host 0.0.0.0 --port ${HTTP_PORT} --force
Restart=always
RestartSec=5s
StandardOutput=append:/var/log/oracle1001/server.log
StandardError=append:/var/log/oracle1001/server.err

[Install]
WantedBy=multi-user.target
EOF

# 6. Nginx Reverse Proxy Configuration (Port 80 -> 8765)
cat > /etc/nginx/sites-available/oracle1001 << EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${NODE_IP} oracle1001.xyz www.oracle1001.xyz;

    location / {
        proxy_pass http://127.0.0.1:${HTTP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_connect_timeout 15s;
        proxy_read_timeout 60s;
    }

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg)$ {
        root ${APP_ROOT}/output;
        expires 1d;
        add_header Cache-Control "public, no-transform";
        try_files \$uri @proxy;
    }

    location @proxy {
        proxy_pass http://127.0.0.1:${HTTP_PORT};
    }
}
EOF

ln -sfn /etc/nginx/sites-available/oracle1001 /etc/nginx/sites-enabled/oracle1001
rm -f /etc/nginx/sites-enabled/default || true

nginx -t && systemctl reload nginx || systemctl restart nginx

# 7. Enable and Start Oracle-1001 Server
systemctl daemon-reload
systemctl enable --now oracle1001-server.service
systemctl restart oracle1001-server.service

# 8. Firewall Configuration (UFW)
ufw allow 22/tcp || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true
ufw allow ${HTTP_PORT}/tcp || true
ufw default allow outgoing || true
ufw --force enable || true

# 9. Healthcheck Verification
echo "==> Verifying web endpoint availability on ${NODE_NAME}..."
sleep 2
HTTP_CODE="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${HTTP_PORT}/output/top500_analytics.html" || echo "000")"
if [[ "${HTTP_CODE}" == "200" ]]; then
    echo "    SUCCESS: Analytical dashboard is responding (HTTP 200 OK)."
else
    echo "    WARNING: Endpoint returned HTTP ${HTTP_CODE}. Check /var/log/oracle1001/server.err"
fi

echo "==> [${NODE_NAME}] Deployment complete! Oracle-1001 Engine active on port ${HTTP_PORT}."
