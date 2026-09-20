#!/usr/bin/env python3
"""
Sentinel multi-node Paramiko orchestrator (out-of-the-box).

Canonical production paths (do NOT use stale prompt traps):
  App:     /opt/oracle1001/sentinel
  Heal:    /opt/oracle1001/deploy/sentinel/heal_node.sh
  Health:  http://HOST:8765/output/api/v1/health
  Volume:  sentinel_data_sqlite / sentinel_output_artifacts
  AIS mode env: SENTINEL_AIS_MODE=on|off  (NOT "live_ais" — that is health source_mode)

Usage (from repo root):
  .\\venv\\Scripts\\python.exe deploy_sentinel.py
  .\\venv\\Scripts\\python.exe deploy_sentinel.py --heal-only
  .\\venv\\Scripts\\python.exe deploy_sentinel.py --bake-image --force --skip-b
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import tempfile
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

INTEL_ENV_KEYS = (
    "NEWSAPI_KEY",
    "NEWS_API_KEY",
    "GIE_API_KEY",
    "AGSI_API_KEY",
    "GIE_KEY",
    "AISSTREAM_API_KEY",
    "AISSTREAM_KEY",
    "FIRMS_MAP_KEY",
    "NASA_FIRMS_MAP_KEY",
    "FIRMS_API_KEY",
    "EXCHANGERATE_KEY",
    "EXCHANGERATE_API_KEY",
    "EXCHANGE_RATE_API_KEY",
    "NASDAQ_DATA_KEY",
    "NASDAQ_API_KEY",
    "QUANDL_API_KEY",
    "BREVO_API_KEY",
    "SENDINBLUE_API_KEY",
    "BREVO_SENDER_EMAIL",
    "BREVO_SENDER_NAME",
)

BAKE_INCLUDE_PREFIXES = (
    "services/",
    "scripts/",
    "web/",
    "docker/",
    "deploy/sentinel/",
    "output/js/",
    "output/css/",
)

BAKE_INCLUDE_FILES = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "Dockerfile",
    ".dockerignore",
    "api_server.py",
    "compressor_stations.py",
    "config.yaml",
    "requirements.txt",
    "run_release.py",
    "build_sentinel_dashboard.py",
    "AGENTS.md",
    "CHANGELOG.md",
    "output/fleet_database.csv",
    "output/fleet_oil_tankers.csv",
    "output/fleet_database_full.csv",
    "output/sentinel_dashboard.html",
    "output/archive/api_status.json",
    "output/deploy_manifest.json",
    "output/qflex_fleet_cargo.json",
    "web/sentinel_engine.js",
)


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
    if not text:
        return
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def run(ssh: paramiko.SSHClient, cmd: str, *, timeout: int = 600) -> tuple[int, str, str]:
    full = f"export DEBIAN_FRONTEND=noninteractive; export NEEDRESTART_MODE=a; {cmd}"
    _stdin, stdout, stderr = ssh.exec_command(full, get_pty=True, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return code, out, err


def sftp_put(ssh: paramiko.SSHClient, local: Path, remote: str, *, binary: bool = False) -> None:
    sftp = ssh.open_sftp()
    try:
        remote_dir = os.path.dirname(remote)
        run(ssh, f"mkdir -p '{remote_dir}'")
        sftp.put(str(local), remote)
        if binary:
            # Never sed binary packs — CRLF rewrite corrupts gzip/tgz.
            run(ssh, f"chmod 644 '{remote}' 2>/dev/null || true")
        else:
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
        "deploy_korolev_sentinel.sh",
    ):
        lp = local_deploy / name
        if lp.is_file():
            sftp_put(ssh, lp, f"{DEPLOY}/{name}")
    prod = REPO / "docker-compose.prod.yml"
    if prod.is_file():
        sftp_put(ssh, prod, f"{APP_A}/docker-compose.prod.yml")


def merge_env(ssh: paramiko.SSHClient, env_path: str, lines: list[str]) -> None:
    run(ssh, f"mkdir -p \"$(dirname '{env_path}')\"; touch '{env_path}'")
    for line in lines:
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
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


def _parse_local_env() -> dict[str, str]:
    path = REPO / ".env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def seed_intel_env_from_local(ssh: paramiko.SSHClient) -> int:
    """Upsert intel keys from workstation .env into Node A .env (values never printed)."""
    local = _parse_local_env()
    lines: list[str] = []
    for k in INTEL_ENV_KEYS:
        if k in local and local[k]:
            lines.append(f"{k}={local[k]}")
    if not lines:
        print("[!] no intel keys found in local .env — container may stay 1/7")
        return 0
    remote_env = f"{APP_A}/.env"
    run(ssh, f"mkdir -p '{APP_A}'; touch '{remote_env}'")
    sftp = ssh.open_sftp()
    try:
        try:
            with sftp.open(remote_env, "r") as fh:
                existing = fh.read().decode("utf-8", errors="replace")
        except OSError:
            existing = ""
        keep: dict[str, str] = {}
        for raw in existing.splitlines():
            if not raw.strip() or raw.strip().startswith("#") or "=" not in raw:
                continue
            ek, ev = raw.split("=", 1)
            keep[ek.strip()] = ev.strip()
        for line in lines:
            k, v = line.split("=", 1)
            keep[k] = v
        keep.setdefault("SENTINEL_AIS_MODE", "on")
        keep.setdefault("DASHBOARD_PORT", "8765")
        keep.setdefault("PORT", "8765")
        body = "\n".join(f"{k}={v}" for k, v in sorted(keep.items())) + "\n"
        with sftp.open(remote_env, "w") as fh:
            fh.write(body)
    finally:
        sftp.close()
    print(f"[OK] seeded Node A .env intel keys (count={len(lines)}; values masked)")
    return len(lines)


def _should_exclude(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    skip_bits = (
        "/__pycache__/",
        "/.git/",
        "/venv/",
        "/.venv/",
        "/logs/",
        "/node_modules/",
        "/_probe/",
        "/screenshots/",
        "_source.mp4",
    )
    if any(b in f"/{rel}/" or b in rel for b in skip_bits):
        return True
    if rel.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm")):
        return True
    return False


def build_bake_tarball(dest: Path) -> Path:
    print(f"[OK] packing bake tarball -> {dest}", flush=True)
    count = 0
    with tarfile.open(dest, "w:gz") as tar:
        for name in BAKE_INCLUDE_FILES:
            p = REPO / name
            if p.is_file():
                tar.add(p, arcname=name.replace("\\", "/"))
                count += 1
        for prefix in BAKE_INCLUDE_PREFIXES:
            root = REPO / prefix.rstrip("/")
            if not root.exists():
                continue
            if root.is_file():
                tar.add(root, arcname=prefix.rstrip("/"))
                count += 1
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(REPO)).replace("\\", "/")
                if _should_exclude(rel):
                    continue
                if rel.endswith((".glb", ".mp4", ".bin", ".wasm")):
                    continue
                if "scripts/ops/" in rel or "/scripts/ops/" in f"/{rel}":
                    continue
                tar.add(path, arcname=rel)
                count += 1
                if count % 200 == 0:
                    print(f"  ... packed {count} files", flush=True)
    print(f"[OK] packed {count} files ({dest.stat().st_size / (1024 * 1024):.1f} MiB)", flush=True)
    return dest


def bake_node_a(ssh: paramiko.SSHClient, *, no_cache: bool = True) -> None:
    print("[OK] Node A FULL IMAGE BAKE starting")
    seed_intel_env_from_local(ssh)
    with tempfile.TemporaryDirectory(prefix="sentinel_bake_") as tmp:
        pack = Path(tmp) / "sentinel_deploy_bake.tgz"
        build_bake_tarball(pack)
        remote_pack = "/tmp/sentinel_deploy_bake.tgz"
        print("[OK] uploading pack to Node A ...", flush=True)
        sftp_put(ssh, pack, remote_pack, binary=True)
        # Integrity check before extract
        local_size = pack.stat().st_size
        code, out, err = run(ssh, f"wc -c < {remote_pack}")
        remote_size = int((out or "0").strip().split()[0] or "0")
        if remote_size != local_size:
            raise RuntimeError(f"pack size mismatch local={local_size} remote={remote_size}")
        print(f"[OK] pack uploaded intact ({local_size} bytes)", flush=True)
    local_deploy = REPO / "deploy" / "sentinel" / "deploy_korolev_sentinel.sh"
    if local_deploy.is_file():
        sftp_put(ssh, local_deploy, f"{APP_A}/deploy/sentinel/deploy_korolev_sentinel.sh")
        sftp_put(ssh, local_deploy, f"{DEPLOY}/deploy_korolev_sentinel.sh")

    nc = "1" if no_cache else "0"
    cmd = f"""
set -euo pipefail
mkdir -p {APP_A}
tar -xzf {remote_pack} -C {APP_A}
rm -f {remote_pack}
find {APP_A}/deploy/sentinel -name '*.sh' -exec sed -i 's/\\r$//' {{}} +
find {APP_A}/docker -name '*.sh' -exec sed -i 's/\\r$//' {{}} + 2>/dev/null || true
chmod +x {APP_A}/deploy/sentinel/*.sh {APP_A}/docker/entrypoint.sh 2>/dev/null || true
mkdir -p {APP_A}/assets/arctic/videos {APP_A}/assets/1-10 {APP_A}/assets/7000
mkdir -p {APP_A}/data/cache {APP_A}/data/db {APP_A}/logs {APP_A}/output/archive
cd {APP_A}
NO_CACHE={nc} FORCE_RECREATE=1 bash deploy/sentinel/deploy_korolev_sentinel.sh
"""
    print(f"[OK] remote bake NO_CACHE={nc} FORCE_RECREATE=1 (long) ...")
    code, out, err = run(ssh, cmd, timeout=7200)
    _safe_print(out[-8000:] if len(out) > 8000 else out)
    if err.strip():
        _safe_print(err[-2000:])
    if code != 0:
        raise RuntimeError(f"bake failed exit={code}")
    if "BAKE_OK" not in out and "BAKE_OK" not in err:
        print("[!] BAKE_OK marker not seen in output — check remote logs")
    if "INTEL_ROUTES_OK" not in out and "INTEL_ROUTES_OK" not in err:
        print("[!] INTEL_ROUTES_OK marker not seen — intel smoke may have been skipped")
    print("[OK] Node A bake finished")


def provision_primary(ssh: paramiko.SSHClient, node: dict, *, heal_only: bool) -> None:
    print(f"[OK] {node['name']}: sync deploy helpers")
    if not heal_only:
        sync_heal_scripts(ssh)
        merge_env(ssh, f"{APP_A}/.env", node["env_lines"])
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
        run(ssh, "mkdir -p /opt/oracle1001/ais_ingest /opt/oracle1001/logs /opt/oracle1001/deploy/sentinel")
        merge_env(ssh, "/opt/oracle1001/ais_ingest/.sentinel_standby.env", node["env_lines"])
    code, out, err = run(ssh, f"bash {DEPLOY}/heal_node.sh --role {node['heal_role']}", timeout=300)
    _safe_print(out or err)
    if code != 0:
        raise RuntimeError(f"london heal failed exit={code}")
    run(
        ssh,
        "systemctl stop aisstream-connector.service 2>/dev/null || true; "
        "systemctl disable aisstream-connector.service 2>/dev/null || true; "
        "systemctl is-active aisstream-connector.service 2>/dev/null || echo inactive",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel dual-node Paramiko orchestrator")
    ap.add_argument("--heal-only", action="store_true", help="Skip SFTP sync / env merge")
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    ap.add_argument(
        "--bake-image",
        action="store_true",
        help="Full image bake on Node A: pack + .env seed + build --force-recreate + volume seed",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="With --bake-image: NO_CACHE=1 (default). CLI compatibility.",
    )
    ap.add_argument(
        "--use-cache",
        action="store_true",
        help="With --bake-image: allow docker layer cache (NO_CACHE=0)",
    )
    ap.add_argument("--key", default=None, help="SSH private key path")
    ap.add_argument("--password-a", default=None)
    ap.add_argument("--password-b", default=None)
    ap.add_argument("--ask-password", action="store_true")
    args = ap.parse_args()

    pwd_a = args.password_a
    pwd_b = args.password_b
    if args.ask_password:
        import getpass

        pwd_a = pwd_a or getpass.getpass("Korolev root password (empty=key): ") or None
        pwd_b = pwd_b or getpass.getpass("London root password (empty=key): ") or None

    rc = 0
    if not args.skip_a:
        node = NODES["node_a"]
        print(f"\n[OK] Processing {node['name']} ({node['ip']})...")
        try:
            ssh = connect(node["ip"], password=pwd_a, key_filename=args.key)
            try:
                if args.bake_image:
                    bake_node_a(ssh, no_cache=not args.use_cache)
                else:
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

    try:
        import json
        import urllib.request

        url = HEALTH_EXT.format(ip=NODES["node_a"]["ip"])
        with urllib.request.urlopen(url, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            cv = resp.headers.get("X-Contract-Version")
        print(f"\n[+] External health {url}:\n{body[:600]}")
        print(f"[+] X-Contract-Version={cv}")
        doc = json.loads(body)
        oob = doc.get("oob") or {}
        prov = oob.get("providers") or {}
        print(
            f"[+] pipeline={doc.get('pipeline_health_status')} "
            f"providers={prov.get('configured')}/{prov.get('total')}"
        )
    except Exception as err:  # noqa: BLE001
        print(f"[-] External health failed: {err}")
        rc = 1

    if args.bake_image:
        import urllib.request

        base = f"http://{NODES['node_a']['ip']}:8765"
        for path in (
            "/output/api/v1/news/latest?limit=3",
            "/output/api/v1/gis/firms/anomalies?days=1",
            "/output/api/v1/market/summary",
            "/api/v1/news/latest?limit=3",
        ):
            try:
                with urllib.request.urlopen(base + path, timeout=30) as resp:
                    cv = resp.headers.get("X-Contract-Version")
                    print(f"[+] {path.split('?')[0]} -> {resp.status} cv={cv}")
                    if resp.status != 200:
                        rc = 1
            except Exception as err:  # noqa: BLE001
                print(f"[-] {path.split('?')[0]} FAIL: {err}")
                rc = 1

    print("\nDONE" if rc == 0 else "\nDONE_WITH_ERRORS")
    return rc


if __name__ == "__main__":
    sys.exit(main())
