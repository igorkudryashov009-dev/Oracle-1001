#!/usr/bin/env python3
"""Re-apply OOB Python modules into running Node A containers (no entrypoint overwrite)."""
from __future__ import annotations

import json
import time
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
        rp = f"{APP}/{rel}"
        _, o, _ = ssh.exec_command(f"mkdir -p \"$(dirname '{rp}')\"")
        o.channel.recv_exit_status()
        sftp.put(str(lp), rp)
        print("PUT", rel)
    sftp.close()

    for cname in ("sentinel-web", "sentinel-core"):
        for rel in FILES:
            rp = f"{APP}/{rel}"
            cmd = f"docker cp '{rp}' {cname}:/app/{rel}"
            _, o, e = ssh.exec_command(cmd)
            code = o.channel.recv_exit_status()
            out = (o.read() + e.read()).decode("utf-8", errors="replace").strip()
            print(f"{cname} {rel} rc={code} {out[:80]}")

    # Soft reload: kill python so container restart policy respawns WITHOUT
    # replacing entrypoint binary. Prefer compose restart of web only.
    _, o, e = ssh.exec_command("docker restart sentinel-web")
    o.channel.recv_exit_status()
    print("restarted sentinel-web", (o.read() + e.read()).decode("utf-8", errors="replace"))
    time.sleep(15)

    _, o, e = ssh.exec_command(
        "curl -fsS --max-time 12 http://127.0.0.1:8765/output/api/v1/health"
    )
    code = o.channel.recv_exit_status()
    raw = (o.read() + e.read()).decode("utf-8", errors="replace")
    if code != 0:
        print("HEALTH_FAIL rc", code, raw[-400:])
        # diagnose entrypoint
        _, o2, e2 = ssh.exec_command("docker logs --tail 15 sentinel-web 2>&1")
        o2.channel.recv_exit_status()
        print((o2.read() + e2.read()).decode("utf-8", errors="replace"))
        ssh.close()
        return 1
    doc = json.loads(raw)
    oob = doc.get("oob") or {}
    print(
        "pipeline=",
        doc.get("pipeline_health_status"),
        "oob_keys=",
        list(oob.keys()),
        "providers=",
        (oob.get("providers") or {}).get("configured"),
        "/",
        (oob.get("providers") or {}).get("total"),
        "ais_live=",
        (oob.get("ais_live_cache") or {}).get("present"),
    )
    # prove module present
    _, o, e = ssh.exec_command(
        "docker exec sentinel-web python -c \"from services.healthcheck import attach_oob_plane; print('healthcheck_ok')\""
    )
    o.channel.recv_exit_status()
    print((o.read() + e.read()).decode("utf-8", errors="replace"))
    ssh.close()
    print("OOB_MODULES_LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
