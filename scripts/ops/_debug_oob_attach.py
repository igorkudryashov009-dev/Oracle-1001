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

cmd = r"""docker exec sentinel-web python - <<'PY'
from services.healthcheck import attach_oob_plane, build_oob_health_overlay
print('overlay', build_oob_health_overlay())
doc = {}
attach_oob_plane(doc)
print('attached', doc.keys())
PY"""
_, o, e = ssh.exec_command(cmd, timeout=60)
code = o.channel.recv_exit_status()
print("rc", code)
print((o.read() + e.read()).decode("utf-8", errors="replace"))

cmd2 = r"""docker exec sentinel-web python - <<'PY'
import traceback
from services import ais_health
# re-run attach block manually
doc = {"x": 1}
try:
    from services.healthcheck import attach_oob_plane
    attach_oob_plane(doc)
    print("ok", doc.get("oob"))
except Exception:
    traceback.print_exc()
PY"""
_, o, e = ssh.exec_command(cmd2, timeout=60)
print("rc2", o.channel.recv_exit_status())
print((o.read() + e.read()).decode("utf-8", errors="replace"))

# Check if attach is inside the function before return - maybe indentation wrong / dead code?
_, o, e = ssh.exec_command("docker exec sentinel-web sed -n '500,540p' /app/services/ais_health.py")
o.channel.recv_exit_status()
print((o.read() + e.read()).decode("utf-8", errors="replace"))
ssh.close()
