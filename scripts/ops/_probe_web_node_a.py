#!/usr/bin/env python3
import time
from pathlib import Path

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    "45.8.230.214",
    username="root",
    key_filename=str(Path.home() / ".ssh" / "id_ed25519"),
    timeout=25,
    allow_agent=True,
    look_for_keys=True,
)

def run(cmd: str, timeout: int = 120) -> None:
    _, o, e = c.exec_command(cmd, timeout=timeout)
    code = o.channel.recv_exit_status()
    out = (o.read() + e.read()).decode("utf-8", errors="replace")
    print("====", cmd[:70], "rc", code)
    print(out[-2000:] if len(out) > 2000 else out)

run('docker ps -a --format "table {{.Names}}\t{{.Status}}"')
run("docker logs --tail 50 sentinel-web 2>&1")
run("cd /opt/oracle1001/sentinel && docker compose up -d sentinel-web 2>&1")
time.sleep(12)
run("curl -fsS --max-time 10 http://127.0.0.1:8765/output/api/v1/health | head -c 1200")
c.close()
