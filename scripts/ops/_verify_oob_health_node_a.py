#!/usr/bin/env python3
import json
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
    "docker exec sentinel-web grep -n attach_oob_plane /app/services/ais_health.py | head",
    "docker exec sentinel-web python -c \"from services.ais_health import build_health_document; d=build_health_document(); print('oob', d.get('oob')); print('keys', list(d.keys())[-8:])\"",
    "curl -fsS --max-time 12 http://127.0.0.1:8765/output/api/v1/health > /tmp/h.json; python3 -c \"import json;d=json.load(open('/tmp/h.json')); print('oob' in d, list((d.get('oob') or {}).keys()), 'disk_free', d.get('disk_free_pct'))\"",
]
for cmd in cmds:
    _, o, e = ssh.exec_command(cmd, timeout=90)
    code = o.channel.recv_exit_status()
    out = (o.read() + e.read()).decode("utf-8", errors="replace")
    print("====", cmd[:90], "rc", code)
    print(out[-1500:] if len(out) > 1500 else out)
ssh.close()
