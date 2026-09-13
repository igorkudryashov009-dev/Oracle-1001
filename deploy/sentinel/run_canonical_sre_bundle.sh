#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /opt/oracle1001/deploy/sentinel
for f in resolve_compose_dir.sh heal_node.sh fix_map_tiles_korolev.sh deploy_parallax_hud.sh; do
  sed -i 's/\r$//' "/tmp/${f}"
  install -m 0755 "/tmp/${f}" "/opt/oracle1001/deploy/sentinel/${f}"
done
bash /opt/oracle1001/deploy/sentinel/heal_node.sh --role korolev
bash /opt/oracle1001/deploy/sentinel/deploy_parallax_hud.sh
bash /opt/oracle1001/deploy/sentinel/fix_map_tiles_korolev.sh
echo '[OK] canonical_sre_bundle_done'
