#!/usr/bin/env python3
"""Seed fresh deploy_manifest.json into Node A host + output_artifacts volume."""
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parents[2]
APP = "/opt/oracle1001/sentinel"
LOCAL = REPO / "output" / "deploy_manifest.json"

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
sftp = ssh.open_sftp()
sftp.put(str(LOCAL), f"{APP}/output/deploy_manifest.json")
sftp.put(str(REPO / "deploy" / "sentinel" / "deploy_manifest.json"), f"{APP}/deploy/sentinel/deploy_manifest.json")
sftp.close()
_, o, e = ssh.exec_command(
    "VOL=$(docker volume inspect sentinel_output_artifacts -f '{{.Mountpoint}}'); "
    f"cp -a {APP}/output/deploy_manifest.json \"$VOL/deploy_manifest.json\"; "
    "docker cp "
    f"{APP}/output/deploy_manifest.json sentinel-web:/app/output/deploy_manifest.json; "
    "echo SEEDED_MANIFEST; ls -la \"$VOL/deploy_manifest.json\""
)
print(o.read().decode())
print(e.read().decode())
ssh.close()
