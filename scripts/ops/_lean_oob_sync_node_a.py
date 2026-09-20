#!/usr/bin/env python3
"""Lean SCP of OOB modules to Node A (Korolev). One-shot ops helper."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parents[1]
APP = "/opt/oracle1001/sentinel"
HOST = "45.8.230.214"
FILES = (
    "scripts/bootstrap_oob.py",
    "services/ais_worker.py",
    "services/cache_swr.py",
    "services/healthcheck.py",
    "services/ais_health.py",
    "services/firms_service.py",
    "services/market_data_service.py",
    "services/news_service.py",
    "api_server.py",
    "docker/entrypoint.sh",
    "docker-compose.yml",
    "AGENTS.md",
    "CHANGELOG.md",
)


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    keys = [
        str(Path.home() / ".ssh" / k)
        for k in ("id_ed25519", "id_rsa")
        if (Path.home() / ".ssh" / k).is_file()
    ]
    client.connect(
        HOST,
        username="root",
        key_filename=keys[0] if keys else None,
        timeout=25,
        allow_agent=True,
        look_for_keys=True,
    )
    return client


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str]:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
    return code, out


def main() -> int:
    ssh = connect()
    sftp = ssh.open_sftp()
    try:
        for rel in FILES:
            lp = REPO / rel
            rp = f"{APP}/{rel.replace(chr(92), '/')}"
            run(ssh, f"mkdir -p \"$(dirname '{rp}')\"")
            sftp.put(str(lp), rp)
            print("PUT", rel)
    finally:
        sftp.close()

    for cname in ("sentinel-web", "sentinel-core"):
        for rel in FILES:
            if not (
                rel.startswith("services/")
                or rel.startswith("scripts/")
                or rel.startswith("api_server")
                or rel.startswith("docker/")
            ):
                continue
            rp = f"{APP}/{rel}"
            code, out = run(ssh, f"docker cp '{rp}' {cname}:/app/{rel} 2>&1 || true")
            if out.strip():
                print(cname, rel, out.strip()[:160])

    for cmd in (
        f"cd {APP} && docker exec sentinel-web python scripts/bootstrap_oob.py 2>&1 | tail -12",
        "docker restart sentinel-web",
        "sleep 10",
    ):
        code, out = run(ssh, cmd, timeout=180)
        print("---", cmd[:72], "rc=", code)
        print(out[-600:] if len(out) > 600 else out)

    ssh.close()

    url = f"http://{HOST}:8765/output/api/v1/health"
    with urllib.request.urlopen(url, timeout=15) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    oob = doc.get("oob") or {}
    print(
        "HEALTH",
        "pipeline=",
        doc.get("pipeline_health_status"),
        "oob_keys=",
        list(oob.keys()),
        "providers=",
        (oob.get("providers") or {}).get("configured"),
        "/",
        (oob.get("providers") or {}).get("total"),
    )
    print("LEAN_OOB_SYNC_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
