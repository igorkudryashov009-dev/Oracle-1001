#!/usr/bin/env bash
# Resolve Sentinel compose app directory (ASCII-only logs).
# Canonical: /opt/oracle1001/sentinel/docker-compose.yml
# NEVER assume /opt/sentinel or compose under deploy/sentinel only.
resolve_sentinel_app_dir() {
  local cand found
  for cand in \
    /opt/oracle1001/sentinel \
    /opt/oracle1001/analytical_engine \
    /opt/oracle1001/deploy/sentinel
  do
    if [[ -f "${cand}/docker-compose.yml" ]]; then
      echo "${cand}"
      return 0
    fi
  done
  found=$(find /opt/oracle1001 -maxdepth 4 -type f -name 'docker-compose.yml' 2>/dev/null | head -n 1 || true)
  if [[ -n "${found}" ]]; then
    dirname "${found}"
    return 0
  fi
  return 1
}
