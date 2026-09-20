#!/usr/bin/env python3
"""Push missing OOB deps (config_keys + healthcheck hardening) to Node A."""
from __future__ import annotations

import json
import time
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parents[1]
APP = "/opt/oracle1001/sentinel"
HOST = "45.8.230.214"
FILES = (
    "services/config_keys.py",
    "services/healthcheck.py",
    "services/cache_swr.py",
    "services/ais_health.py",
    "services/ais_worker.py",
    "services/news_service.py",
    "services/firms_service.py",
    "services/market_data_service.py",
    "services/notify_service.py",
    "scripts/bootstrap_oob.py",
    "api_server.py",
)


def main() -> int:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        HOST,
        username="root",
        key_filename=str(Path.home() / ".ssh" / "id_ed25519"),
        timeout=25,
        allow_agent=True,
        look_for_keys=True,
    )
    sftp = ssh.open_sftp()
    for rel in FILES:
        lp = REPO / rel
        if not lp.is_file():
            print("SKIP missing", rel)
            continue
        rp = f"{APP}/{rel}"
        _, o, _ = ssh.exec_command(f"mkdir -p \"$(dirname '{rp}')\"")
        o.channel.recv_exit_status()
        sftp.put(str(lp), rp)
        print("PUT", rel)
    sftp.close()

    for cname in ("sentinel-web", "sentinel-core"):
        for rel in FILES:
            if not (REPO / rel).is_file():
                continue
            rp = f"{APP}/{rel}"
            _, o, e = ssh.exec_command(f"docker cp '{rp}' {cname}:/app/{rel}")
            code = o.channel.recv_exit_status()
            err = (o.read() + e.read()).decode("utf-8", errors="replace").strip()
            print(f"{cname} {rel} rc={code} {err[:60]}")

    _, o, e = ssh.exec_command("docker restart sentinel-web")
    o.channel.recv_exit_status()
    print("restart", (o.read() + e.read()).decode("utf-8", errors="replace").strip())
    time.sleep(15)

    _, o, e = ssh.exec_command(
        "curl -fsS --max-time 12 http://127.0.0.1:8765/output/api/v1/health"
    )
    code = o.channel.recv_exit_status()
    raw = (o.read() + e.read()).decode("utf-8", errors="replace")
    if code != 0:
        print("HEALTH_FAIL", raw[-500:])
        ssh.close()
        return 1
    doc = json.loads(raw)
    oob = doc.get("oob") or {}
    providers = oob.get("providers") or {}
    print(
        "pipeline=",
        doc.get("pipeline_health_status"),
        "oob_keys=",
        list(oob.keys()),
        "providers=",
        providers.get("configured"),
        "/",
        providers.get("total"),
    )
    ssh.close()
    return 0 if oob else 2


if __name__ == "__main__":
    raise SystemExit(main())
