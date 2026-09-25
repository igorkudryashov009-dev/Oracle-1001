#!/usr/bin/env bash
# gen_api_key.sh <name> <tier> — generate Sentinel inbound API key (shown once).
# Writes API_KEYS_JSON into local + Korolev .env. Never re-prints after this run.
# Usage: bash scripts/gen_api_key.sh demo readonly
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NAME="${1:-}"
TIER_RAW="${2:-readonly}"
if [[ -z "${NAME}" ]]; then
  echo "usage: $0 <name> <admin|readonly>" >&2
  exit 2
fi
TIER="$(echo "${TIER_RAW}" | tr '[:upper:]' '[:lower:]')"
if [[ "${TIER}" != "admin" && "${TIER}" != "readonly" ]]; then
  echo "tier must be admin|readonly" >&2
  exit 2
fi

export GEN_NAME="${NAME}" GEN_TIER="${TIER}"
# python prints KEY on stdout line1; meta on stderr
API_KEY="$(
  ./venv/Scripts/python.exe - <<'PY'
import json, os, sys
from pathlib import Path
from services.api_auth import generate_api_key, upsert_key_into_env_map, fingerprint_key

name = os.environ["GEN_NAME"]
tier = os.environ["GEN_TIER"]
key = generate_api_key()
env_path = Path(".env")
raw = ""
lines = []
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("API_KEYS_JSON="):
            raw = line.split("=", 1)[1]
        else:
            lines.append(line)
new_json = upsert_key_into_env_map(raw, key=key, name=name, tier=tier)
lines.append(f"API_KEYS_JSON={new_json}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(key)
print(
    f"local_ok mask=****{key[-4:]} fp={fingerprint_key(key)} name={name} tier={tier}",
    file=sys.stderr,
)
PY
)"

MASK="****${API_KEY: -4}"
echo "GENERATED name=${NAME} tier=${TIER} mask=${MASK}"
echo "API_KEY (copy now — shown once):"
echo "${API_KEY}"
echo
echo "TIP: for investor demo run:  bash scripts/gen_api_key.sh demo readonly"

SSH_IDENTITY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
HOST="${SENTINEL_HOST:-root@45.8.230.214}"
APP="${SENTINEL_APP:-/opt/oracle1001/sentinel}"
if ssh -o BatchMode=yes -o ConnectTimeout=8 -i "${SSH_IDENTITY}" "${HOST}" "true" 2>/dev/null; then
  B64="$(printf '%s' "${API_KEY}" | base64 | tr -d '\n\r')"
  ssh -o BatchMode=yes -i "${SSH_IDENTITY}" "${HOST}" \
    env B64="${B64}" NAME="${NAME}" TIER="${TIER}" APP="${APP}" bash -s <<'REMOTE'
set -euo pipefail
KEY="$(printf '%s' "$B64" | base64 -d)"
export KEY NAME TIER APP
python3 - <<'PY'
import json, os
from pathlib import Path
app = Path(os.environ["APP"])
env_path = app / ".env"
key = os.environ["KEY"]
name = os.environ["NAME"]
tier = os.environ["TIER"]
raw = ""
lines = []
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("API_KEYS_JSON="):
            raw = line.split("=", 1)[1]
        else:
            lines.append(line)
try:
    data = json.loads(raw) if raw.strip() else {}
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data[key] = {"name": name, "tier": tier}
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
lines.append(f"API_KEYS_JSON={blob}")
env_path.parent.mkdir(parents=True, exist_ok=True)
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("KOROLEV_API_KEYS_OK n_keys=%s mask=****%s" % (len(data), key[-4:]))
PY
cd "$APP"
docker compose up -d --force-recreate --no-deps sentinel-web >/dev/null 2>&1 || docker restart sentinel-web >/dev/null
echo "KOROLEV_WEB_REFRESHED"
REMOTE
else
  echo "WARN: Korolev SSH unavailable — local .env only"
fi

unset API_KEY B64 KEY || true
echo "DONE — header: X-API-Key: <paste once>"
