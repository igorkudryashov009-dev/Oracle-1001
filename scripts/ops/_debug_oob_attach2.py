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

py = (
    "import traceback\n"
    "try:\n"
    "  from services.healthcheck import attach_oob_plane, build_oob_health_overlay\n"
    "  print('overlay', build_oob_health_overlay())\n"
    "except Exception as e:\n"
    "  traceback.print_exc()\n"
    "try:\n"
    "  from services.config_keys import registry_status\n"
    "  print('registry', registry_status().get('configured'), registry_status().get('total'))\n"
    "except Exception:\n"
    "  traceback.print_exc()\n"
    "try:\n"
    "  from services.ais_health import build_health_document\n"
    "  d=build_health_document()\n"
    "  print('oob_in_doc', d.get('oob'))\n"
    "except Exception:\n"
    "  traceback.print_exc()\n"
)
cmd = "docker exec sentinel-web python -c " + repr(py)
_, o, e = ssh.exec_command(cmd, timeout=90)
print("rc", o.channel.recv_exit_status())
print((o.read() + e.read()).decode("utf-8", errors="replace"))
ssh.close()
