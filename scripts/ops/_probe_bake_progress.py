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
for cmd in (
    "ps aux | grep -E 'docker|compose|build' | grep -v grep | head -20",
    "docker ps -a --format 'table {{.Names}}\t{{.Status}}' | head -10",
    "ls -la /tmp/sentinel_deploy_bake.tgz 2>/dev/null; ls -la /opt/oracle1001/sentinel/services/news_service.py 2>/dev/null | head -2",
    "tail -30 /var/log/syslog 2>/dev/null | grep -i docker || true",
    "pgrep -af 'deploy_korolev|docker compose' || true",
):
    _, o, e = ssh.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print("====", cmd[:70])
    print((o.read() + e.read()).decode("utf-8", errors="replace")[:1500])
ssh.close()
