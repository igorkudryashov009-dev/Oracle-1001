"""Lean sync: push updated market_data_service.py to Node A container.
Read-only contract: only syncs TWO files + docker cp into sentinel-core.
AGENTS.md: docker cp + restart is validation; production path = full image bake.

Usage:
  python scripts/ops/_sync_market_service_node_a.py --password-a <root_password>
  python scripts/ops/_sync_market_service_node_a.py --key ~/.ssh/id_rsa
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_ap = argparse.ArgumentParser()
_ap.add_argument("--password-a", default=None)
_ap.add_argument("--key", default=None)
_ap.add_argument("--ask-password", action="store_true")
_ARGS, _ = _ap.parse_known_args()

# Reuse connect() + sftp_put() from the existing deploy module
import importlib.util, types
_spec = importlib.util.spec_from_file_location(
    "deploy_sentinel",
    ROOT / "deploy" / "sentinel" / "deploy_sentinel.py",
)
_mod: types.ModuleType = importlib.util.module_from_spec(_spec)  # type: ignore
_spec.loader.exec_module(_mod)  # type: ignore
connect  = _mod.connect
sftp_put = _mod.sftp_put

NODE_A_HOST = "45.8.230.214"
APP_PATH    = "/opt/oracle1001"
CONTAINER   = "sentinel-core"

FILES_TO_SYNC = [
    (ROOT / "services" / "market_data_service.py",
     f"{APP_PATH}/services/market_data_service.py"),
    (ROOT / "scripts" / "ops" / "_incident_analysis.py",
     f"{APP_PATH}/scripts/ops/_incident_analysis.py"),
]

def main() -> None:
    try:
        import paramiko  # noqa: F401
    except ImportError:
        print("[ERROR] paramiko not installed: pip install paramiko")
        sys.exit(1)

    pwd = _ARGS.password_a
    if _ARGS.ask_password:
        import getpass
        pwd = getpass.getpass("Korolev root password (Enter for key-auth): ") or None

    print(f"[SSH] Connecting to root@{NODE_A_HOST}:22 ...")
    try:
        ssh = connect(NODE_A_HOST, password=pwd, key_filename=_ARGS.key)
    except Exception as e:
        print(f"[ERROR] SSH failed: {e}")
        print("[HINT] Use: --password-a <pass>  OR  --key <path>  OR  --ask-password")
        sys.exit(1)

    sftp = ssh.open_sftp()
    print("[SFTP] Connected.")

    for local_path, remote_path in FILES_TO_SYNC:
        if not local_path.is_file():
            print(f"  [SKIP] {local_path.name}")
            continue
        remote_dir = str(Path(remote_path).parent)
        ssh.exec_command(f"mkdir -p {remote_dir}")
        time.sleep(0.2)
        sftp.put(str(local_path), remote_path)
        print(f"  [SFTP OK] {local_path.name} → {remote_path}")

    sftp.close()

    # docker cp into running container
    print(f"\n[DOCKER] Copying into {CONTAINER}...")
    for local_path, remote_path in FILES_TO_SYNC:
        rel = str(Path(remote_path).relative_to(APP_PATH))
        container_dst = f"/app/{rel}"
        cmd = f"docker cp {remote_path} {CONTAINER}:{container_dst}"
        _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        err = stderr.read().decode().strip()
        print(f"  {Path(remote_path).name:<40} {'OK' if not err else 'WARN: '+err[:80]}")

    # Restart sentinel-core only
    print(f"\n[DOCKER] Restarting {CONTAINER}...")
    _, stdout, stderr = ssh.exec_command(f"docker restart {CONTAINER}", timeout=90)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print(f"  {out or err or 'OK'}")
    time.sleep(12)

    # Verify via HTTP
    print("\n[VERIFY] Probing http://45.8.230.214:8765/api/v1/market/summary ...")
    import urllib.request, json as _json
    for attempt in range(4):
        try:
            with urllib.request.urlopen(
                f"http://{NODE_A_HOST}:8765/api/v1/market/summary", timeout=15
            ) as r:
                data = _json.loads(r.read())
            tier  = data.get("price_tier") or "?"
            comms = data.get("commodities") or {}
            brent = (comms.get("brent") or {}).get("latest") or {}
            ttf   = (comms.get("ttf_proxy") or {}).get("latest") or {}
            print(f"  HTTP 200 | price_tier={tier}")
            print(f"  Brent: {brent.get('Settle')} {brent.get('unit','')}")
            print(f"  TTF:   {ttf.get('Settle')} {ttf.get('unit','')}")
            break
        except Exception as e:
            print(f"  Attempt {attempt+1}/4 failed: {e}")
            time.sleep(6)

    ssh.close()
    print("\n[DONE] Lean sync complete.")
    print("[NOTE] Production delivery requires: git commit → full image bake (AGENTS.md).")


if __name__ == "__main__":
    main()

