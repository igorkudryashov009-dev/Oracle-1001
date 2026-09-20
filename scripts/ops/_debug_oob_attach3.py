#!/usr/bin/env python3
from pathlib import Path

import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(
    "45.8.230.214",
    username="root",
    key_filename=str(Path.home() / ".ssh" / "id_ed25519"),
    timeout=25,
    allow_agent=True,
    look_for_keys=True,
)

cmds = [
    "docker exec sentinel-web pwd",
    "docker exec sentinel-web ls -la /app/services/healthcheck.py /app/services/config_keys.py",
    "docker exec -e PYTHONPATH=/app -w /app sentinel-web python /tmp/p.py",
    "docker exec -e PYTHONPATH=/app -w /app sentinel-web python -c \"from services.healthcheck import attach_oob_plane; print(attach_oob_plane({}))\"",
    "docker exec -e PYTHONPATH=/app -w /app sentinel-web python -c \"from services.ais_health import build_health_document; d=build_health_document(); print(d.get('oob'))\"",
]
for cmd in cmds:
    _, o, e = ssh.exec_command(cmd, timeout=90)
    code = o.channel.recv_exit_status()
    out = (o.read() + e.read()).decode("utf-8", errors="replace")
    print("==== rc", code, cmd[:90])
    print(out[:1200])
ssh.close()
