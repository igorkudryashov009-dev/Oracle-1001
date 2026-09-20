#!/usr/bin/env python3
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parents[1]
APP = "/opt/oracle1001/sentinel"
FILES = ("scripts/bootstrap_oob.py", "services/ais_worker.py", "docker-compose.yml")

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
for rel in FILES:
    rp = f"{APP}/{rel}"
    parent = str(Path(rp).parent).replace("\\", "/")
    _, o, _ = ssh.exec_command(f"mkdir -p '{parent}'")
    o.channel.recv_exit_status()
    sftp.put(str(REPO / rel), rp)
    print("PUT", rel)
sftp.close()
for cname in ("sentinel-web", "sentinel-core"):
    for rel in FILES[:2]:
        _, o, e = ssh.exec_command(f"docker cp '{APP}/{rel}' {cname}:/app/{rel}")
        code = o.channel.recv_exit_status()
        print(cname, rel, "rc", code)
_, o, e = ssh.exec_command("docker exec sentinel-web python scripts/bootstrap_oob.py")
print((o.read() + e.read()).decode("utf-8", errors="replace")[-500:])
ssh.close()
print("LEAN_BOOTSTRAP_SYNC_OK")
