#!/usr/bin/env bash
# Idempotent install of AIS archive collector on a private Linux host (1GB-aware).
# Usage (on server, after unpacking payload to /tmp/oracle1001_ais):
#   AISSTREAM_API_KEY=... bash install_ais_collector.sh
# Refuses to enable units if key is missing/placeholder (honest degradation).
set -euo pipefail

ROOT="/opt/oracle1001/ais_archive"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing AIS archive into ${ROOT}"
mkdir -p "${ROOT}"/{venv,logs,история1/daily,история1/by_vessel,bin}

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates

if [[ ! -d "${ROOT}/venv" ]] || [[ ! -x "${ROOT}/venv/bin/python" ]]; then
  python3 -m venv "${ROOT}/venv"
fi
# shellcheck disable=SC1091
source "${ROOT}/venv/bin/activate"
pip install --upgrade pip wheel
pip install "pandas>=2,<3" "python-dotenv>=1,<2" "PyYAML>=6" "websockets>=12" "pydantic>=2,<3"

for f in collector.py daily_snapshot.py forecast.py distance_calc.py prepare_targets.py \
         config.yaml targets.json targets_batches.json; do
  if [[ -f "${SRC_DIR}/${f}" ]]; then
    install -m 0644 "${SRC_DIR}/${f}" "${ROOT}/${f}"
  fi
done
if [[ -f "${SRC_DIR}/fleet_database.csv" ]]; then
  mkdir -p "${ROOT}/output"
  install -m 0644 "${SRC_DIR}/fleet_database.csv" "${ROOT}/output/fleet_database.csv"
fi

# .env: create if missing; never overwrite existing secrets
if [[ ! -f "${ROOT}/.env" ]]; then
  umask 077
  cat > "${ROOT}/.env" <<EOF
AISSTREAM_API_KEY=YOUR_AISSTREAM_API_KEY_HERE
EOF
  chmod 600 "${ROOT}/.env"
fi
chmod 600 "${ROOT}/.env"
sed -i 's/\r$//' "${ROOT}/.env" || true

# Optional injection from environment for first-time setup (not logged)
if [[ -n "${AISSTREAM_API_KEY:-}" && "${AISSTREAM_API_KEY}" != YOUR_* ]]; then
  if grep -q '^AISSTREAM_API_KEY=' "${ROOT}/.env"; then
    sed -i "s|^AISSTREAM_API_KEY=.*|AISSTREAM_API_KEY=${AISSTREAM_API_KEY}|" "${ROOT}/.env"
  else
    printf 'AISSTREAM_API_KEY=%s\n' "${AISSTREAM_API_KEY}" >> "${ROOT}/.env"
  fi
  chmod 600 "${ROOT}/.env"
fi

install -m 0644 "${SRC_DIR}/ais-collector.service" /etc/systemd/system/ais-collector.service
install -m 0644 "${SRC_DIR}/ais-daily-snapshot.service" /etc/systemd/system/ais-daily-snapshot.service
install -m 0644 "${SRC_DIR}/ais-daily-snapshot.timer" /etc/systemd/system/ais-daily-snapshot.timer
# Compatibility alias expected by acceptance wording
ln -sfn /etc/systemd/system/ais-collector.service /etc/systemd/system/collector.service
systemctl daemon-reload

KEY="$(grep -E '^AISSTREAM_API_KEY=' "${ROOT}/.env" | head -1 | cut -d= -f2- | tr -d '\r')"
if [[ -z "${KEY}" || "${KEY}" == YOUR_* ]]; then
  echo "BLOCKED: AISSTREAM_API_KEY missing/placeholder — units installed but NOT enabled."
  echo "Set key in ${ROOT}/.env then: systemctl enable --now ais-collector.service ais-daily-snapshot.timer"
  exit 2
fi

systemctl enable ais-collector.service collector.service ais-daily-snapshot.timer
systemctl restart ais-collector.service
systemctl start ais-daily-snapshot.timer
systemctl --no-pager --full status ais-collector.service || true
systemctl list-timers ais-daily-snapshot.timer --no-pager || true
echo "AIS archive start date (UTC): $(date -u +%Y-%m-%d). Expect Forecast depth ~30d, Causal ~60-90d."
