#!/usr/bin/env python3
"""Abort hung no-cache bake; cached rebuild + force-recreate + intel smoke."""
from __future__ import annotations

import time
from pathlib import Path

import paramiko

APP = "/opt/oracle1001/sentinel"
HOST = "45.8.230.214"


def main() -> int:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        HOST,
        username="root",
        key_filename=str(Path.home() / ".ssh" / "id_ed25519"),
        timeout=25,
        allow_agent=True,
        look_for_keys=True,
    )

    def run(cmd: str, timeout: int = 600) -> tuple[int, str]:
        _, o, e = ssh.exec_command(cmd, timeout=timeout)
        code = o.channel.recv_exit_status()
        return code, (o.read() + e.read()).decode("utf-8", errors="replace")

    print("==> kill hung no-cache build")
    run("pkill -f 'deploy_korolev_sentinel.sh' || true")
    run("pkill -f 'docker compose .*build' || true")
    run("pkill -f 'docker-buildx bake' || true")
    time.sleep(3)

    # Verify host tree has intel after our earlier extract (may be stale)
    code, out = run(
        f"wc -c {APP}/services/news_service.py {APP}/scripts/serve_dashboard.py; "
        f"grep -c _serve_intel_get_apis {APP}/scripts/serve_dashboard.py || true"
    )
    print(out)

    print("==> cached image build + force-recreate (NO_CACHE=0)")
    code, out = run(
        f"""
set -euo pipefail
cd {APP}
NO_CACHE=0 FORCE_RECREATE=1 bash deploy/sentinel/deploy_korolev_sentinel.sh
""",
        timeout=3600,
    )
    print(out[-6000:] if len(out) > 6000 else out)
    print("exit", code)
    ssh.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
