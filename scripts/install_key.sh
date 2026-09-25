#!/usr/bin/env bash
# install_key.sh — single human action to install a provider key (local + Korolev).
# Usage:  bash scripts/install_key.sh <PROVIDER>
# PROVIDER: GFW | VF | ANTHROPIC | ALERT_WEBHOOK
# Value via read -s (no echo). Logs/stdout: masked ****last4 only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROVIDER_RAW="${1:-}"
if [[ -z "${PROVIDER_RAW}" ]]; then
  echo "usage: $0 <GFW|VF|ANTHROPIC|ALERT_WEBHOOK>" >&2
  exit 2
fi

PROVIDER="$(echo "${PROVIDER_RAW}" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
case "${PROVIDER}" in
  GFW|GFW_API_TOKEN)
    ENV_KEY="GFW_API_TOKEN"
    SIGNAL_PROVIDER="gfw"
    ;;
  VF|VESSELFINDER|VESSELFINDER_API_KEY)
    ENV_KEY="VESSELFINDER_API_KEY"
    SIGNAL_PROVIDER="vesselfinder"
    ;;
  ANTHROPIC|ANTHROPIC_API_KEY)
    ENV_KEY="ANTHROPIC_API_KEY"
    SIGNAL_PROVIDER="anthropic"
    ;;
  ALERT_WEBHOOK|ALERT_WEBHOOK_URL|WEBHOOK)
    ENV_KEY="ALERT_WEBHOOK_URL"
    SIGNAL_PROVIDER="alert_webhook"
    ;;
  *)
    echo "unsupported provider: ${PROVIDER_RAW}" >&2
    echo "supported: GFW VF ANTHROPIC ALERT_WEBHOOK" >&2
    exit 2
    ;;
esac

mask_last4() {
  local v="$1"
  local n=${#v}
  if (( n <= 4 )); then
    echo "****"
  else
    echo "****${v: -4}"
  fi
}

upsert_env_file() {
  local file="$1" key="$2" val="$3"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  chmod 600 "$file" 2>/dev/null || true
  local tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$file" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >>"$tmp"
  mv "$tmp" "$file"
  chmod 600 "$file"
}

echo "Provider: ${ENV_KEY}"
echo -n "Paste key/token (input hidden): "
IFS= read -r -s KEY_VALUE
echo
KEY_VALUE="$(printf '%s' "${KEY_VALUE}" | tr -d '\r\n')"
# GFW JWT must be exactly 3 segments; repair duplicated payload/sig paste
if [[ "${ENV_KEY}" == "GFW_API_TOKEN" ]]; then
  export INSTALL_KEY_VALUE="${KEY_VALUE}"
  KEY_VALUE="$(
    ./venv/Scripts/python.exe - <<'PY' 2>/dev/null || python - <<'PY'
import os
from services.gfw_events import sanitize_gfw_token
print(sanitize_gfw_token(os.environ.get("INSTALL_KEY_VALUE") or ""), end="")
PY
  )"
  unset INSTALL_KEY_VALUE
  segs="${KEY_VALUE//[^.]/}"
  # segs var = only dots; count = length of dots string + ... use python lens
  echo "GFW token normalized: len=${#KEY_VALUE} mask=$(mask_last4 "${KEY_VALUE}")"
fi
if [[ -z "${KEY_VALUE}" ]]; then
  echo "ERROR: empty key" >&2
  exit 3
fi

MASKED="$(mask_last4 "${KEY_VALUE}")"
echo "Installing ${ENV_KEY}=${MASKED}"

# 1) Local .env (persistence for next bake)
upsert_env_file "${ROOT}/.env" "${ENV_KEY}" "${KEY_VALUE}"
if [[ "${ENV_KEY}" == "GFW_API_TOKEN" ]]; then
  upsert_env_file "${ROOT}/.env" "GFW_API_KEY" "${KEY_VALUE}"
fi
if [[ "${ENV_KEY}" == "VESSELFINDER_API_KEY" ]]; then
  upsert_env_file "${ROOT}/.env" "VESSEL_FINDER_USERKEY" "${KEY_VALUE}"
fi

# 2) Local runtime overlay + signal (bind-mounted data/)
mkdir -p "${ROOT}/data/archive"
export INSTALL_ENV_KEY="${ENV_KEY}"
export INSTALL_SIGNAL="${SIGNAL_PROVIDER}"
# Pass value via env to python — not argv
export INSTALL_KEY_VALUE="${KEY_VALUE}"
./venv/Scripts/python.exe - <<'PY' 2>/dev/null || python - <<'PY'
import os
from services.runtime_env import write_runtime_key, write_install_signal
write_runtime_key(os.environ["INSTALL_ENV_KEY"], os.environ["INSTALL_KEY_VALUE"])
write_install_signal(os.environ["INSTALL_SIGNAL"])
print("local runtime_env + signal ok")
PY

# 3) Korolev: .env + runtime (docker bind) + immediate watchdog (base64, no plaintext argv)
SSH_IDENTITY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
HOST="${SENTINEL_HOST:-root@45.8.230.214}"
APP="${SENTINEL_APP:-/opt/oracle1001/sentinel}"
RUNNER="${SENTINEL_RUNNER:-/opt/oracle1001/deploy/sentinel/run_sentinel_job.sh}"

if ssh -o BatchMode=yes -o ConnectTimeout=10 -i "${SSH_IDENTITY}" "${HOST}" "true" 2>/dev/null; then
  B64="$(printf '%s' "${KEY_VALUE}" | base64 | tr -d '\n\r')"
  echo "Syncing to Korolev (masked=${MASKED}) ..."
  ssh -o BatchMode=yes -i "${SSH_IDENTITY}" "${HOST}" \
    "B64='${B64}' ENV_KEY='${ENV_KEY}' SIGNAL='${SIGNAL_PROVIDER}' APP='${APP}' RUNNER='${RUNNER}' bash -s" <<'REMOTE'
set -euo pipefail
KEY_VALUE="$(printf '%s' "$B64" | base64 -d)"
mkdir -p "$APP/data/archive"
touch "$APP/.env"; chmod 600 "$APP/.env"
tmp=$(mktemp)
grep -v "^${ENV_KEY}=" "$APP/.env" >"$tmp" 2>/dev/null || true
printf '%s=%s\n' "$ENV_KEY" "$KEY_VALUE" >>"$tmp"
mv "$tmp" "$APP/.env"
chmod 600 "$APP/.env"
case "$ENV_KEY" in
  GFW_API_TOKEN)
    tmp=$(mktemp); grep -v '^GFW_API_KEY=' "$APP/.env" >"$tmp" || true
    printf 'GFW_API_KEY=%s\n' "$KEY_VALUE" >>"$tmp"; mv "$tmp" "$APP/.env"; chmod 600 "$APP/.env"
    ;;
  VESSELFINDER_API_KEY)
    tmp=$(mktemp); grep -v '^VESSEL_FINDER_USERKEY=' "$APP/.env" >"$tmp" || true
    printf 'VESSEL_FINDER_USERKEY=%s\n' "$KEY_VALUE" >>"$tmp"; mv "$tmp" "$APP/.env"; chmod 600 "$APP/.env"
    ;;
esac
# Runtime overlay inside container (./data bind → /app/data)
docker exec -e B64="$B64" -e ENV_KEY="$ENV_KEY" -e SIGNAL="$SIGNAL" sentinel-web \
  python -c 'import os,base64; from services.runtime_env import write_runtime_key, write_install_signal
v=base64.b64decode(os.environ["B64"]).decode(); write_runtime_key(os.environ["ENV_KEY"], v); write_install_signal(os.environ["SIGNAL"]); print("runtime_ok")'
# Immediate activation (does not wait for */15 watchdog)
if [[ -x "$RUNNER" ]]; then
  bash "$RUNNER" pipeline_watchdog || true
else
  docker exec sentinel-web python -m services.scheduler --job pipeline_watchdog || true
fi
echo "KOROLEV_OK signal=$SIGNAL"
REMOTE
else
  echo "WARN: SSH to Korolev unavailable — local .env + runtime_env only"
fi

unset KEY_VALUE INSTALL_KEY_VALUE B64 || true
echo "DONE ${ENV_KEY}=${MASKED} — probe triggered. Check /api/v1/health (alerts, acceptance)."
if [[ "${ENV_KEY}" == "GFW_API_TOKEN" || "${ENV_KEY}" == "VESSELFINDER_API_KEY" ]]; then
  echo "TIP: optional — bash scripts/install_key.sh ALERT_WEBHOOK  # notify on GREEN commissioning"
fi
if [[ "${ENV_KEY}" == "ALERT_WEBHOOK_URL" ]]; then
  echo "TIP: webhook receives JSON POST {at,kind,severity,message,detail,status} — see services/alerts.py"
fi
if [[ "${ENV_KEY}" == "ANTHROPIC_API_KEY" ]]; then
  echo "TIP: daily_brief timer 02:30 UTC writes llm_daily_brief only (no scores/positions)"
fi
