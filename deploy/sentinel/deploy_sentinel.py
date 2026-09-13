#!/usr/bin/env python3
"""
Sentinel multi-node Paramiko orchestrator (out-of-the-box).

Canonical production paths (do NOT use stale prompt traps):
  App:     /opt/oracle1001/sentinel
  Heal:    /opt/oracle1001/deploy/sentinel/heal_node.sh
  Health:  http://HOST:8765/output/api/v1/health
  Volume:  sentinel_data_sqlite
  AIS mode env: SENTINEL_AIS_MODE=on|off  (NOT "live_ais" — that is health source_mode)

Auth: SSH private key by default (BatchMode). Optional passwords via argv — never prompted
unless --ask-password (interactive). Prefer keys already authorized on both roots.

Usage (from repo root):
  .\\venv\\Scripts\\python.exe deploy\\sentinel\\deploy_sentinel.py
  .\\venv\\Scripts\\python.exe deploy\\sentinel\\deploy_sentinel.py --heal-only
  .\\venv\\Scripts\\python.exe deploy\\sentinel\\deploy_sentinel.py --skip-b
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("[-] paramiko missing — pip install paramiko", file=sys.stderr)
    sys.exit(2)

# ── topology ─────────────────────────────────────────────────────────────────
NODES = {
    "node_a": {
        "name": "Korolev Primary (RU)",
        "ip": "45.8.230.214",
        "role": "primary",
        "heal_role": "korolev",
        # Merged into /opt/oracle1001/sentinel/.env (never invent API keys)
        "env_lines": [
            "SENTINEL_AIS_MODE=on",
            "DASHBOARD_PORT=8765",
            "PORT=8765",
        ],
    },
    "node_b": {
        "name": "London Standby (UK)",
        "ip": "185.39.19.75",
        "role": "standby",
        "heal_role": "london",
        "env_lines": [
            "CONNECTOR_STATE=inactive",
            "PRIMARY_NODE_IP=45.8.230.214",
        ],
    },
}

APP_A = "/opt/oracle1001/sentinel"
DEPLOY = "/opt/oracle1001/deploy/sentinel"
HEALTH_LOCAL = "http://127.0.0.1:8765/output/api/v1/health"
HEALTH_EXT = "http://{ip}:8765/output/api/v1/health"
REPO = Path(__file__).resolve().parents[2]


def _default_keys() -> list[Path]:
    home = Path.home() / ".ssh"
    return [p for p in (home / "id_ed25519", home / "id_rsa", home / "id_ecdsa") if p.is_file()]


def connect(
    ip: str,
    *,
    password: str | None = None,
    key_filename: str | None = None,
    timeout: float = 25.0,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "hostname": ip,
        "port": 22,
        "username": "root",
        "timeout": timeout,
        "allow_agent": True,
        "look_for_keys": True,
    }
    if password:
        kwargs["password"] = password
    keys = [key_filename] if key_filename else [str(k) for k in _default_keys()]
    last_err: Exception | None = None
    if keys:
        for kf in keys:
            try:
                client.connect(**kwargs, key_filename=kf)
                return client
            except Exception as e:  # noqa: BLE001
                last_err = e
                client.close()
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if password:
        client.connect(**kwargs)
        return client
    raise RuntimeError(f"SSH failed to {ip}: {last_err}")


def _safe_print(text: str) -> None:
    """Windows cp125x consoles choke on remote UTF-8 glyphs (✔ →)."""
    if not text:
        return
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def run(ssh: paramiko.SSHClient, cmd: str, *, timeout: int = 600) -> tuple[int, str, str]:
    full = f"export DEBIAN_FRONTEND=noninteractive; export NEEDRESTART_MODE=a; {cmd}"
    stdin, stdout, stderr = ssh.exec_command(full, get_pty=True, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return code, out, err


def sftp_put(ssh: paramiko.SSHClient, local: Path, remote: str) -> None:
    sftp = ssh.open_sftp()
    try:
        # ensure remote dir
        remote_dir = os.path.dirname(remote)
        run(ssh, f"mkdir -p '{remote_dir}'")
        sftp.put(str(local), remote)
        run(ssh, f"sed -i 's/\\r$//' '{remote}'; chmod +x '{remote}' 2>/dev/null || true")
    finally:
        sftp.close()


def sync_heal_scripts(ssh: paramiko.SSHClient) -> None:
    local_deploy = REPO / "deploy" / "sentinel"
    for name in (
        "heal_node.sh",
        "hard_recover_ais_db.sh",
        "export_ais_db_to_host.sh",
        "recover_ais_db.sh",
    ):
        lp = local_deploy / name
        if lp.is_file():
            sftp_put(ssh, lp, f"{DEPLOY}/{name}")
    prod = REPO / "docker-compose.prod.yml"
    if prod.is_file():
        sftp_put(ssh, prod, f"{APP_A}/docker-compose.prod.yml")


def merge_env(ssh: paramiko.SSHClient, env_path: str, lines: list[str]) -> None:
    """Idempotent key=value upsert without clobbering AISSTREAM_API_KEY etc."""
    # Upload a tiny helper via stdin-less one-liners (avoid nested f-string braces).
    run(ssh, f"mkdir -p \"$(dirname '{env_path}')\"; touch '{env_path}'")
    for line in lines:
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # sed delete old key then append
        cmd = (
            f"grep -v '^{key}=' '{env_path}' > '{env_path}.tmp' 2>/dev/null || true; "
            f"mv '{env_path}.tmp' '{env_path}'; "
            f"echo '{key}={val}' >> '{env_path}'; "
            f"echo merged {key}"
        )
        code, out, err = run(ssh, cmd)
        print(out.strip() or err.strip())
        if code != 0:
            raise RuntimeError(f"env merge failed for {key}: {err or out}")


def provision_primary(ssh: paramiko.SSHClient, node: dict, *, heal_only: bool) -> None:
    print(f"[OK] {node['name']}: sync deploy helpers")
    if not heal_only:
        sync_heal_scripts(ssh)
        merge_env(ssh, f"{APP_A}/.env", node["env_lines"])
        # ensure crons
        run(
            ssh,
            f"""
mkdir -p /opt/oracle1001/logs
tmp=/tmp/cron.$$
crontab -l 2>/dev/null | grep -vE 'heal_node|export_ais_db' >"$tmp" || true
echo '*/5 * * * * {DEPLOY}/heal_node.sh --role korolev >> /opt/oracle1001/logs/heal_korolev.log 2>&1' >>"$tmp"
echo '*/15 * * * * {DEPLOY}/export_ais_db_to_host.sh >> /opt/oracle1001/logs/export_ais.log 2>&1' >>"$tmp"
crontab "$tmp"; rm -f "$tmp"
""",
        )
    print(f"[OK] {node['name']}: heal")
    code, out, err = run(ssh, f"bash {DEPLOY}/heal_node.sh --role {node['heal_role']}", timeout=900)
    _safe_print(out)
    if err.strip():
        _safe_print(err)
    if code != 0:
        raise RuntimeError(f"heal failed exit={code}")
    time.sleep(3)
    code, out, err = run(ssh, f"curl -fsS --max-time 15 {HEALTH_LOCAL}")
    _safe_print(f"[OK] Health (local):\n{out[:800]}")
    if code != 0:
        raise RuntimeError(f"health curl failed: {err or out}")


def provision_standby(ssh: paramiko.SSHClient, node: dict, *, heal_only: bool) -> None:
    print(f"[OK] {node['name']}: standby harden (NO full docker compose)")
    if not heal_only:
        local = REPO / "deploy" / "sentinel" / "heal_node.sh"
        if local.is_file():
            sftp_put(ssh, local, f"{DEPLOY}/heal_node.sh")
        # marker env only — do not invent /opt/sentinel stack
        run(ssh, "mkdir -p /opt/oracle1001/ais_ingest /opt/oracle1001/logs /opt/oracle1001/deploy/sentinel")
        merge_env(ssh, "/opt/oracle1001/ais_ingest/.sentinel_standby.env", node["env_lines"])
    code, out, err = run(ssh, f"bash {DEPLOY}/heal_node.sh --role {node['heal_role']}", timeout=300)
    _safe_print(out or err)
    if code != 0:
        raise RuntimeError(f"london heal failed exit={code}")
    # connector must stay inactive while Korolev is SoT
    run(ssh, "systemctl stop aisstream-connector.service 2>/dev/null || true; systemctl disable aisstream-connector.service 2>/dev/null || true; systemctl is-active aisstream-connector.service 2>/dev/null || echo inactive")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel dual-node Paramiko orchestrator")
    ap.add_argument("--heal-only", action="store_true", help="Skip SFTP sync / env merge")
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    ap.add_argument("--key", default=None, help="SSH private key path")
    ap.add_argument("--password-a", default=None, help="Optional root password Node A")
    ap.add_argument("--password-b", default=None, help="Optional root password Node B")
    ap.add_argument(
        "--ask-password",
        action="store_true",
        help="Interactive passwords (discouraged; keys preferred)",
    )
    args = ap.parse_args()

    pwd_a = args.password_a
    pwd_b = args.password_b
    if args.ask_password:
        import getpass

        pwd_a = pwd_a or getpass.getpass("Korolev root password (empty=key): ") or None
        pwd_b = pwd_b or getpass.getpass("London root password (empty=key): ") or None

    # positional compat with draft: python deploy_sentinel.py [pwd_a] [pwd_b]
    # only if explicitly passed and flags unused
    argv_rest = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(argv_rest) >= 1 and not pwd_a and not args.key:
        # treat as passwords only when --ask-password or explicit --password-* not used
        # safer: ignore bare argv passwords unless --allow-password-argv
        pass

    rc = 0
    if not args.skip_a:
        node = NODES["node_a"]
        print(f"\n[OK] Processing {node['name']} ({node['ip']})...")
        try:
            ssh = connect(node["ip"], password=pwd_a, key_filename=args.key)
            try:
                provision_primary(ssh, node, heal_only=args.heal_only)
            finally:
                ssh.close()
        except Exception as err:  # noqa: BLE001
            print(f"[FAIL] {node['name']}: {err}")
            rc = 1

    if not args.skip_b:
        node = NODES["node_b"]
        print(f"\n[OK] Processing {node['name']} ({node['ip']})...")
        try:
            ssh = connect(node["ip"], password=pwd_b, key_filename=args.key)
            try:
                provision_standby(ssh, node, heal_only=args.heal_only)
            finally:
                ssh.close()
        except Exception as err:  # noqa: BLE001
            print(f"[FAIL] {node['name']}: {err}")
            rc = 1

    # external acceptance
    try:
        import urllib.request

        url = HEALTH_EXT.format(ip=NODES["node_a"]["ip"])
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        print(f"\n[+] External health {url}:\n{body[:600]}")
        if '"pipeline_health_status": "NOMINAL"' not in body and '"pipeline_health_status":"NOMINAL"' not in body:
            # still OK if FRESH warming — warn only
            print("[!] pipeline not NOMINAL yet (check fleet G3 is informational)")
            if rc == 0:
                rc = 0
    except Exception as err:  # noqa: BLE001
        print(f"[-] External health failed: {err}")
        rc = 1

    print("\nDONE" if rc == 0 else "\nDONE_WITH_ERRORS")
    return rc


if __name__ == "__main__":
    sys.exit(main())
