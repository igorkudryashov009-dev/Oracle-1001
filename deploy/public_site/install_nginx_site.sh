#!/usr/bin/env bash
# Idempotent public vitrine for oracle1001.xyz on LD8 (NO gas secrets).
set -euo pipefail

WEB_ROOT="/var/www/oracle1001"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/site"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends nginx certbot python3-certbot-nginx

mkdir -p "${WEB_ROOT}"
if [[ -d "${SRC_DIR}" ]]; then
  find "${WEB_ROOT}" -mindepth 1 -delete 2>/dev/null || true
  mkdir -p "${WEB_ROOT}"
  cp -a "${SRC_DIR}/." "${WEB_ROOT}/"
fi

# Prefer files under output/ as docroot
if [[ -d "${WEB_ROOT}/output" ]]; then
  DOCROOT="${WEB_ROOT}/output"
else
  DOCROOT="${WEB_ROOT}"
fi

cat >/etc/nginx/sites-available/oracle1001.xyz <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name oracle1001.xyz www.oracle1001.xyz;

    root ${DOCROOT};
    index mission_control.html;

    location = / {
        try_files /mission_control.html =404;
    }

    location / {
        try_files \$uri \$uri/ /mission_control.html;
    }

    location ~* \.(env|py|sh|service|timer)$ {
        deny all;
        return 404;
    }
}
EOF

ln -sfn /etc/nginx/sites-available/oracle1001.xyz /etc/nginx/sites-enabled/oracle1001.xyz
rm -f /etc/nginx/sites-enabled/default

ufw allow OpenSSH || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true
ufw --force enable || true

nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "HTTP vitrine ready at ${DOCROOT} (TLS after DNS A-records + certbot)"
