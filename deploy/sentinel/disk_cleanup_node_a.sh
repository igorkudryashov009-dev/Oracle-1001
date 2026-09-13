#!/bin/bash
set -euo pipefail
BEFORE=$(df -B1 / | awk 'NR==2{print $4}')
echo "BEFORE_FREE_BYTES=$BEFORE"
df -h /

# 1) Corrupt salvage dumps (live DB is healthy 3MB)
rm -fv /opt/oracle1001/ais_data/corrupt_backup/sentinel_ais.db.20260912T140929Z \
       /opt/oracle1001/ais_data/corrupt_backup/sentinel_ais.db.20260912T142430Z \
       /opt/oracle1001/ais_data/corrupt_backup/sentinel_ais.db-wal \
       /opt/oracle1001/ais_data/corrupt_backup/sentinel_ais.db-shm || true

# 2) Stale host copy (same size as corrupt dumps; live is docker volume)
rm -fv /opt/oracle1001/analytical_engine/история1/sentinel_ais.db || true

# 3) Host log bloat
truncate -s 0 /var/log/btmp || true
journalctl --vacuum-size=50M || true
apt-get clean || true

# 4) Docker careful prune (no -a on images until unused)
docker container prune -f || true
docker image prune -f || true
# eoil-monitor exited; remove unused image if no containers reference it
if ! docker ps -a --format '{{.Image}}' | grep -q 'eoil-monitor'; then
  docker rmi eoil-monitor:hybrid-v2 2>/dev/null || true
else
  docker rmi eoil-monitor:hybrid-v2 2>/dev/null || true
fi
docker volume prune -f || true

AFTER=$(df -B1 / | awk 'NR==2{print $4}')
echo "AFTER_FREE_BYTES=$AFTER"
FREED=$((AFTER - BEFORE))
echo "FREED_BYTES=$FREED"
awk -v f="$FREED" 'BEGIN{printf "FREED_GB=%.2f\n", f/1024/1024/1024}'
df -h /
echo "=== docker system df ==="
docker system df
echo "=== remaining ais/analytical ==="
du -sh /opt/oracle1001/ais_data /opt/oracle1001/ais_data/corrupt_backup /opt/oracle1001/analytical_engine 2>/dev/null || true
ls -lah /opt/oracle1001/ais_data/corrupt_backup/ 2>/dev/null || true
