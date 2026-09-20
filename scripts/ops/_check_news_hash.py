#!/usr/bin/env python3
import hashlib
from pathlib import Path

import paramiko

local = Path("services/news_service.py").read_bytes()
print("local", hashlib.sha256(local).hexdigest()[:16], len(local))

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
_, o, e = ssh.exec_command(
    "sha256sum /opt/oracle1001/sentinel/services/news_service.py; "
    "docker exec sentinel-web sha256sum /app/services/news_service.py; "
    "docker exec sentinel-web python -c \"from services.news_service import TOPIC_TERMS; print(TOPIC_TERMS)\"; "
    "docker exec sentinel-web python -c \"from services.config_keys import registry_status; print(registry_status()['configured'], registry_status()['total'])\""
)
print(o.read().decode())
print(e.read().decode())
ssh.close()
