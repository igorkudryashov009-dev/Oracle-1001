# Sentinel two-node deploy (1 GB VPS)

| Node | IP | Role (current SoT) |
|---|---|---|
| A Korolev | `45.8.230.214` | **Primary AIS** + HUD `:8765` (`SENTINEL_AIS_MODE=on`) |
| B London | `185.39.19.75` | **Standby AIS edge** (systemd installed, service **disabled**) + DB sync tooling |

## Why this topology
- AISstream allows **≤3 WS / account**. Running ingest on both nodes fights for slots and RAM.
- Korolev is proven `pipeline=NOMINAL` with G3 `single_persistent` rotation.
- London has lean venv + `aisstream-connector.service` ready for cutover; do **not** enable until Korolev AIS is set `off`.

## Cutover (London becomes edge writer)
```bash
# On Korolev
cd /opt/oracle1001/sentinel
sed -i 's/^SENTINEL_AIS_MODE=.*/SENTINEL_AIS_MODE=off/' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker cp scripts/run_sentinel_core.py sentinel-core:/app/scripts/run_sentinel_core.py
docker restart sentinel-core

# On London
systemctl enable --now aisstream-connector.service
# cron: */5 sync_ais_db_to_korolev.sh → /opt/oracle1001/ais_data
```

## Hardening
- Swap 2G `/swapfile` on both nodes
- UFW: 22 / 80 / 443 / 8765
- Prod mem caps: core 512m / web 384m (`docker-compose.prod.yml`)
- Redis **not** deployed (unused by Sentinel code; 1 GB budget)

## Scripts
- `provision_vps.sh` — swap/ufw/docker
- `heal_node.sh` — self-heal (dpkg, docker, compose / London standby)
- `deploy_korolev_sentinel.sh` — compose up
- `install_london_ais_relay.sh` — lean venv edge
- `switch_korolev_analytics_mode.sh` — bind-mount AIS DB + AIS off
- `Deploy-TwoNode.ps1` — Windows orchestrator

## Self-heal (Node A)
Cron every 5 min: `heal_node.sh --role korolev`  
Restarts Docker if down, `compose up -d`, keeps UFW/swap. Soft integrity check → `hard_recover_ais_db.sh` if malformed.

**Live AIS DB = Docker named volume `data_sqlite`** (not `/opt/oracle1001/ais_data` bind). Bind-mount + WAL caused recurring `malformed`. Host path is staging only via `export_ais_db_to_host.sh` (cron `*/15`).

DB recovery: `recover_ais_db.sh` / `hard_recover_ais_db.sh`.

## Paramiko orchestrator (Windows / any host with keys)
```powershell
.\venv\Scripts\pip.exe install paramiko   # once
.\venv\Scripts\python.exe deploy\sentinel\deploy_sentinel.py
# or: .\venv\Scripts\python.exe deploy_sentinel.py
# heal only:  ... deploy_sentinel.py --heal-only
# A only:     ... deploy_sentinel.py --skip-b
```
Auth: SSH keys (default). Passwords optional via `--password-a` / `--ask-password` — not required when BatchMode keys work.

## Canonical one-click (Windows) — NEVER use /opt/sentinel or /api/v1/health
```powershell
Invoke-RestMethod http://45.8.230.214:8765/output/api/v1/health
.\deploy\sentinel\Sentinel-SreController.ps1 -Action Status
.\deploy\sentinel\Sentinel-SreController.ps1 -Action HealA

# Restart web (canonical — NEVER "docker compose restart web")
ssh root@45.8.230.214 "bash /opt/oracle1001/deploy/sentinel/restart_sentinel_web.sh && bash /opt/oracle1001/deploy/sentinel/fix_map_tiles_korolev.sh"

# Full SRE bundle (heal + parallax cache-bust + tiles)
ssh root@45.8.230.214 "bash /opt/oracle1001/deploy/sentinel/run_canonical_sre_bundle.sh"

start http://45.8.230.214:8765/output/sentinel_dashboard.html?sheet=top10
```

**Compose truth:** `docker-compose.yml` lives in **`/opt/oracle1001/sentinel`**.  
`find .../deploy/sentinel -name docker-compose.yml` is a stale trap — use `resolve_compose_dir.sh`.

**Stale prompt traps:** `/opt/sentinel`, `/api/v1/health`, volume `sentinel_data`, service name `web`.  
Live: `/opt/oracle1001/sentinel`, `/output/api/v1/health`, volume `sentinel_data_sqlite`, service `sentinel-web`.
