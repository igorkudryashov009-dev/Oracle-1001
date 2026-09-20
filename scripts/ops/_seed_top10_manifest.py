#!/usr/bin/env python3
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parents[2]
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
for rel in ("output/js/top10_vessels_manifest.js",):
    lp = REPO / rel
    sftp.put(str(lp), f"/opt/oracle1001/sentinel/{rel}")
    print("PUT", rel)
sftp.close()

cmds = [
    "docker volume ls | grep -i output",
    "docker inspect sentinel-web --format '{{range .Mounts}}{{.Name}} {{.Destination}}{{\"\\n\"}}{{end}}'",
    (
        "VOL=$(docker inspect sentinel-web --format '{{range .Mounts}}"
        "{{if eq .Destination \"/app/output\"}}{{.Name}}{{end}}{{end}}'); "
        "MP=$(docker volume inspect \"$VOL\" -f '{{.Mountpoint}}'); "
        "echo VOL=$VOL MP=$MP; "
        "cp -a /opt/oracle1001/sentinel/output/js/top10_vessels_manifest.js \"$MP/js/top10_vessels_manifest.js\"; "
        "docker cp /opt/oracle1001/sentinel/output/js/top10_vessels_manifest.js "
        "sentinel-web:/app/output/js/top10_vessels_manifest.js; "
        "echo SEEDED"
    ),
]
for cmd in cmds:
    _, o, e = ssh.exec_command(cmd)
    o.channel.recv_exit_status()
    print((o.read() + e.read()).decode("utf-8", errors="replace"))
ssh.close()
