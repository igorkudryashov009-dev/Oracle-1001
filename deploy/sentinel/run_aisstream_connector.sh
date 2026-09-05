#!/usr/bin/env bash
# systemd helper: run Sentinel AISStream connector as a service unit.
# Install: sudo cp deploy/sentinel/aisstream-connector.service /etc/systemd/system/
set -euo pipefail
ROOT="${ORACLE_ROOT:-/opt/oracle1001/analytical_engine}"
cd "${ROOT}"
# shellcheck disable=SC1091
source "${ROOT}/venv/bin/activate"
exec python -m services.aisstream_connector
