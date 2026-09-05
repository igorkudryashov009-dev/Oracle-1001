#!/usr/bin/env bash
# Idempotent UFW hardening for Rucloud private runner.
# Incoming: SSH only. Outgoing: allow (HTTPS to provider). No public HTTP.
set -euo pipefail

SSH_PORT="${SSH_PORT:-22}"
SSH_ALLOW_CIDRS="${SSH_ALLOW_CIDRS:-}"

export DEBIAN_FRONTEND=noninteractive
if ! command -v ufw >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ufw
fi

ufw default deny incoming
ufw default allow outgoing

# Drop prior auto-managed comments then re-apply (keeps other admin rules intact).
while ufw status numbered | grep -qE 'ssh-allowlist|OpenSSH|oracle1001-ssh'; do
  num="$(ufw status numbered | sed -n 's/^\[\s*\([0-9]\+\)\].*\(ssh-allowlist\|OpenSSH\|oracle1001-ssh\).*/\1/p' | tail -1)"
  [[ -n "${num}" ]] || break
  yes | ufw delete "${num}" >/dev/null || break
done

if [[ -n "${SSH_ALLOW_CIDRS}" ]]; then
  for cidr in ${SSH_ALLOW_CIDRS}; do
    ufw allow from "${cidr}" to any port "${SSH_PORT}" proto tcp comment "oracle1001-ssh"
  done
else
  ufw allow "${SSH_PORT}/tcp" comment "oracle1001-ssh"
fi

# Deny common public web ports if previously opened
ufw delete allow 80/tcp >/dev/null 2>&1 || true
ufw delete allow 443/tcp >/dev/null 2>&1 || true

ufw --force enable
ufw status verbose
