# AGENTS.md — Oracle-1001 / Sentinel (read before any change)

**Contract-Version:** `1.8.0-ops-gis-sot` · **Last-Revised:** `2026-09-20` · GIS compressor stations SoT · route proximity  
Any edit to this file is a **versioned event** — bump Contract-Version and add a CHANGELOG.md entry in the same change.

This file is the **binding operational contract** for humans and AI agents opening
the repo for the first time. If anything else (old README sections, stale JSON
reports under `output/`, chat history) conflicts with this file — **this file wins**.

## Consolidated contract themes (v1.4.0 → … → v1.8.0-ops-gis-sot)

This revision consolidates operational locks that must stay consistent with Dual Gate:

0. **GIS compressor stations (v1.8.0)** — `services/compressor_stations.py` is the SoT for **185** stations (`ALL_COMPRESSOR_STATIONS` as frozen `CompressorStation` tuples; point = bbox centroid). REST: `GET /api/v1/gis/compressor-stations`, `POST /api/v1/route/analytics` (adds `proximity_compressors` within 50 nm). Header `X-Contract-Version: 1.8.0-ops-gis-sot`. Capacity values are corridor-class notional estimates, not live SCADA.
0b. **GIS tile proxy (v1.8.0)** — `services/tile_proxy.py`: same-origin `GET /api/v1/gis/tiles/{provider}/{z}/{x}/{y}.png` (`maptiler` | `mapbox` | `esri_satellite` | `osm_standard`). Keys `MAPTILES_KEY` / `MAPBOX_TOKEN` (plus legacy aliases) stay server-side; missing key or monthly hard-stop (MapTiler ≥90k, Mapbox ≥40k via SQLite `tile_usage_counter`) → Esri/OSM Never-Black. Disk cache `data/tile_cache` TTL 30d; headers `X-Cache` / `X-Contract-Version`. Legacy HUD path `/api/tiles/` (`maptiles_proxy.py`) remains.
0c. **GIS AIS tracker (v1.8.0)** — `services/ais_tracker.py`: `GET /api/v1/gis/ais/status|positions|vessel/{imo}`. Cache-first from local G3 `ais_positions` into `data/ais_history.db` (6h TTL); `$30/mo` hard-stop budget ready for future commercial sat. **Never** invents satellite endpoints; `SatelliteAISAdapter` stays stub. Header `X-AIS-Source` + `X-Contract-Version`. Does **not** feed `fleet_sample_status`.
0d. **OOB zero-touch (v1.8.0)** — `scripts/bootstrap_oob.py` ensures `logs/` / `output/archive/` / `data/cache/` + `.env` vs `config_keys` + intel mocks. `services/ais_worker.py` buffers G3 DB → `data/cache/ais_live.json` (**no second WS**; `--ingest` only when core is off). `services/healthcheck.py` attaches `oob` plane (7 providers masked, disk, node sync) to `/api/v1/health`. Intel adapters use `cache_swr` stale-while-revalidate. Compose profile `oob-buffer` optional.
0b. **Deploy auto-discovery (v1.7.0)** — `scripts/verify_deploy_manifest.py` discovers HUD/runtime assets via globs (`web/js/**`, `web/**/*.css`, `services/**/*.py`, arctic `*.mp4`, GLB). New features under those trees are verified **without** hand-editing a file list. Fourth "hotfix not in image" incident class is closed by discovery, not per-file patches.
0c. **Oracle thresholds single SoT (v1.7.0)** — `health.thresholds` comes from `services.dual_gate.export_dual_gate_thresholds()`. Client Oracle must **not** hardcode `ORACLE_THRESHOLDS` numeric cutoffs (same class of bug as pre-1.6.1 quant SoT drift).
1. **Archive provenance** — OSINT / VesselFinder hybrid **snapshot** ≠ live G3 AIS; never `PREMIUM SATELLITE` without real commercial satellite feed; archive never feeds `fleet_sample_status`. While VF REST returns `Invalid Userkey`, HUD labels **`ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE`**.
2. **Digital Twin (GLB/Voxel)** — assets sync into `output/assets/3d_models/` (canonical — **not** `assets/models/`); Inspector Never-Black hierarchy; lazy-unmount 3D/map/video on sheet leave (single heavy surface). Deploy must **never** `--exclude=*.glb`.
3. **Offline ML** — Node A/B serve `.cbm` inference only; weekly offline retrain (Variant A); `/api/v1/quant/risk` exposes `model_last_retrained`, `model_last_retrained_utc`, and `model_provenance: "offline_batch"`.
4. **Disk headroom** — `disk_free_pct` in `health.json`; `<20%` → DEGRADED; `<10%` → CRITICAL (do **not** retarget to 15% — AGENTS lock); host log/corrupt_backup retention via `services/log_retention.py`.
5. **ARCTIC sheet (v1.6.0+)** — Arc7 Yamalmax flight videos are **LUMA-generated** at the **same trust tier** as Q-Flex REAL VIDEO / LUMA track. Recognizable vessel identity does **not** raise trust. `VIDEO-DERIVED VIEWS` are frames from that AI flight (agent ffmpeg **or** user-curated stills) — **not** an Ortho Triplet and **not** a measurement source. Missing nadir → honest `TOP VIEW UNAVAILABLE`; when a user-curated overhead still exists → show it under the same VIDEO-DERIVED badge (`NOT ORTHO / NOT MEASUREMENT`).
6. **Map tile proxy (v1.6.0 / extended v1.8.0)** — HUD legacy: same-origin `GET /api/tiles/{provider}/{z}/{x}/{y}.png` (`maptiles_proxy.py`). GIS SoT: `GET /api/v1/gis/tiles/...` (`tile_proxy.py`) with monthly free-tier hard-stops. Paid keys stay **server-side only**. Absent/invalid key or upstream failure → **Esri World Imagery Never-Black fallback** (no client-visible `API KEY REQUIRED` dead-end).
7. **VesselFinder commercial client (v1.6.0)** — `services/vesselfinder_client.py` + budget + Q-Flex poller are **wired and honest**. Until a **validated** `VESSELFINDER_API_KEY` exists, Q-Flex cargo / fleet value remains `notional_full_capacity_fallback` (never silently pretend live draft). Key presence alone ≠ live cargo.
8. **Quant Dual-Gate SoT (v1.6.1)** — `/api/v1/quant/risk` must resolve `pipeline_health_status` via the **same live** `build_health_document()` path as `/api/v1/health`. Never prefer a stale on-disk `health.json` snapshot for gate fields.
9. **Particulars provenance (v1.6.2)** — see dedicated section below; round class placeholders (e.g. DWT `130000`) are not registry truth.
10. **OOB seal (v1.6.4)** — Archive SNAPSHOT/DEMO labeling + ML utc/provenance aliases + sheet-leave heavy-media pause + disk prune ops without Dual Gate threshold drift.

### Particulars provenance (LOA / Beam / DWT / Draft)

**Canonical Q-Flex catalog SoT:** `services/top10_vessels.py` → regenerated `web/js/top10_vessels_manifest.js` / `output/js/top10_vessels_manifest.js` via `write_js_manifest()`.  
**ARCTIC catalog SoT:** `web/js/arctic_vessels_manifest.js` (hand-maintained with `particulars_sources`).

| Field | Typical origin today | Notes |
|---|---|---|
| LOA / Beam | Class sheet + fleet CSV / public registries | Usually stable across sources |
| DWT | Mixed: some hull-specific, some **round Q-Max≈130 000 placeholders** | Placeholder ≠ cargo capacity (m³). Do not treat `130000` as verified DWT |
| Draft on Q-Flex cards | Often **current/AIS draught** | Distinct from **design/summer** draught (e.g. LIJMILIYA 9.2 m current vs 13.70 m design) |

**Manual corrections (this class of fix):** when the Architect supplies a VesselFinder (or Q88) page dump proving a mismatch, update the catalog SoT with:
- corrected numeric value;
- `particulars_provenance` (or ARCTIC `particulars_sources`) entry with `method: "manual_verification"`, source label, and `verified_at` UTC date;
- never label the correction as `live API` while `VESSELFINDER_API_KEY` remains invalid.

**Automatic cross-check path (future):** validated VesselFinder commercial REST (`services/vesselfinder_client.py` + `qflex_vf_poller.py`) is the only intended automated particulars refresh. Until then, fleet value / cargo stay `notional_full_capacity_fallback` on catalog DWT.

**Anti-pattern:** do not silently overwrite catalog DWT from `output/fleet_database.csv` archive scrapes without review — CSV can carry heuristic/ballast-era figures (e.g. LIJMILIYA CSV `66050` vs registry `155159`).

### Image bake lock (v1.5.0-baked+) — closes container-only hotfix risk

**As of 2026-09-13** (extended 2026-09-16 for ARCTIC/tiles/VF), hotfixes are **baked into the Docker image** via full `Deploy-TwoNode` rebuild (`docker compose build --no-cache` + `down` + `up --force-recreate`) on Node A, with the **same services SoT** synced to Node B.

- **No outstanding container-only (`docker cp`) state remains on Node A** after a successful bake + recreate proof (`BAKE_OK` in `deploy_korolev_sentinel.sh`).
- **Named volume `output_artifacts`** overlays `/app/output` — after bake, host Sync-Tree HUD bytes (`output/js`, dashboard HTML, `output/assets` incl. arctic) **must** be seeded into that volume (`SEEDED_OUTPUT_VOLUME` in `deploy_korolev_sentinel.sh`). Do not assume image layers alone refresh the HUD volume.
- **ARCTIC videos:** Deploy must **never** blanket `--exclude=*.mp4`. Pack/serve `assets/arctic/` (bind-mounted) and seed `output/assets/arctic/`; `*_source.mp4` may stay out of the pack as provenance-only.
- **Node B (London)** is the lean AIS relay (venv/systemd under `/opt/oracle1001/ais_ingest`), not a second HUD image — the **same services SoT** is synced there on every TwoNode deploy so failover ingest does not resurrect stale gate/archive writers.
- Agents must **not** treat `docker cp` + `restart` as production delivery for code. Permanent path = git commit → Sync-Tree → image bake → force-recreate → output-volume seed.
## What this system is

- Dual-plane OSINT + quant HUD over a **terrestrial** AIS free-tier feed (AISstream).
- Production-ready as: **pipeline healthy · fleet sample LIMITED · quant caveated**.
- Distributed across a two-node physical topology (Korolev Primary analytical core + London Hot-standby edge relay).
- **Not** a source of statistically valid fleet-wide trading signals at current N.

## What this system is NOT

- Not broken because `top500_live_coverage` is 3–5 (or briefly 0–1). That is **G3**:
  physical terrestrial AIS ceiling, confirmed by a clean 60-min soak (peak≈5, unique≈7/h)
  and a 25-min trend (0→5). Do **not** “fix coverage” by re-tuning subscription.
- Not authorized to invent satellite AIS credentials/endpoints.
- Not authorized to treat high `model_cv_accuracy_pct` as live fleet confidence.

## Dual Deploy Gate (Single Source of Truth)

| Field | Values | Blocks `--prod-rebuild` / `--prod-gate`? |
|---|---|---|
| `pipeline_health_status` | NOMINAL / DEGRADED / CRITICAL | **YES** — only NOMINAL publishes |
| `fleet_sample_status` | FULL (≥100) / LIMITED (≥5) / INSUFFICIENT (&lt;5) | **NO** — informational + UI banner |

**Absolute Source of Truth:** `services/dual_gate.py` (and `services/release_gate.py` for CI/publish).  
Every consumer (`api_server.py`, `services/ais_health.py`, `services/quant_risk_service.py`) **must**
import and invoke functions from `services/dual_gate.py`. Parallel or duplicated threshold logic
(e.g. ad-hoc `lag < 300` or `coverage >= 100`) is strictly prohibited.

Thresholds (empirical, do not invent new ones without a new soak):

- `FLEET_SAMPLE_FULL_MIN = 100`
- `FLEET_SAMPLE_LIMITED_MIN = 5`  ← Prompt-7 observed peak
- `FLEET_WIDE_METRIC_MIN_N = 30`  ← LSSI/DAR/DFS production eligibility
- `PIPELINE_LIVE_LAG_SEC = 300.0` ← Max acceptable live telemetry age
- `DISK_FREE_MIN_PCT = 20.0` ← below → `pipeline_health_status=DEGRADED` (pre-ENOSPC)
- `DISK_FREE_CRITICAL_PCT = 10.0` ← below → `CRITICAL`
- Canonical HTTP port: **8765** only (8478 = CRITICAL drift)
- Internal Micro-API port: **8766** (`api_server.py`, loopback `127.0.0.1:8766` only, never exposed to public edge)
- Edge Topology: `sentinel-web` on **8765** is the sole public gateway. `/api/v1/quant/*` is reverse-proxied to internal 8766 with in-process fallback; zero port competition.

## Two-Node Physical Topology & Failover Contract

1. **Node A — Korolev Primary Analytical Core (`45.8.230.214`):**
 - Hosts public HUD & Edge gateway on `:8765` (`sentinel-web`).
 - Hosts internal quant micro-API on `:8766` (`api_server.py`).
 - Runs analytical **inference** (load `.cbm` + HMM params), hedging ledger — **not** CatBoost/HMM batch retrain.
 - When `SENTINEL_AIS_MODE != "off"`, acts as primary terrestrial AIS ingest.
2. **Node B — London Hot-Standby Edge Relay (`185.39.19.75`):**
   - Low-latency edge connection to AISstream London PoP.
   - Runs identical G3 ingest profile: `single_persistent`, `mmsi_per_subscription: 200`, `rotation_interval_seconds: 180`.
   - Continuous unidirectional SQLite snapshot replication via `sync_ais_db_to_korolev.sh` to Korolev `/opt/oracle1001/ais_data/sentinel_ais.db`.
3. **Active Node Designation:**
   - Both `health.json` and `/api/v1/quant/risk` return `active_node: "korolev" | "london"`.
4. **Failover Cutover Guard (Zero-False-Nominal Window):**
   - When ingest cutover to London is triggered (`switch_korolev_analytics_mode.sh`), a timestamp marker `/opt/oracle1001/logs/failover_cutover.ts` is created.
   - `services/dual_gate.py` (`check_failover_status`) verifies whether the first post-cutover replication has landed (`/opt/oracle1001/logs/last_london_sync.ts` > cutover).
   - While cutover is awaiting the first London sync, `pipeline_health_status` is forced to **`DEGRADED`** with reason `failover_cutover_awaiting_first_edge_sync`. It CANNOT report `NOMINAL` on stale Korolev data.

## Quant Risk Endpoint & Synthetic Data Contract

Endpoint `/api/v1/quant/risk` (and `/output/api/v1/quant/risk`) provides quantitative risk metrics governed by strict anti-hallucination rules:

- **Mandatory `is_synthetic: bool`:** True if ANY metric or component in the payload relies on synthetic, unbacked, or statistically insufficient data.
- **Mandatory `synthetic_components: list[str]`:** Explicitly enumerates non-production components (e.g. `["returns_sharpe_cvar"]` when ledger marks \(N < 30\)).
- **Dual-Gate Consumer Enforcement:** `production_actionable` is **strictly False** unless ALL of the following are satisfied:
  1. `pipeline_health_status == "NOMINAL"`
  2. `fleet_sample_status == "FULL"`
  3. `is_synthetic == False`
- **P0 Strategy Gating Invariant:** Whenever `production_actionable == False`, `recommended_strategy_id` and `recommended_strategy_name` are **strictly None**. Instead, payload includes `blocked_reason: str` detailing the exact gate rejection (e.g. `insufficient_sample_N=...` or `synthetic_returns_data`). Web and API consumers cannot read an active strategy while execution is unbacked.
- **UI Warning Overlay:** Web client (`uaip_quant_visualizer.js`) MUST render a blocking high-contrast overlay `SYNTHETIC DEMO DATA · NOT WIRED TO LIVE MODEL` across all forecast / Sharpe charts whenever `is_synthetic === true`.
- **Model freshness (honest caveat):** `/api/v1/quant/risk` MUST expose `model_last_retrained` (UTC ISO) from offline artifact meta / `.cbm` mtime. Never hide stale CV accuracy behind silent live retrain.
- **Database Schema Invariant:** The SQLite table is strictly named **`ais_positions`** (columns `received_at`, `timestamp_utc`, `mmsi`). Never query legacy `positions`.

## ML Serving ≠ Training (Prompt 15)

**Principle:** Node A (Korolev) and Node B (London) are **serving / inference** nodes. They load shipped CatBoost `.cbm` files and use persisted HMM regime artifacts for forward inference. They do **not** run CatBoost purged-CV / full retrain on the live edge (disk+CPU constrained: ingest + HUD + quant API).

| Role | Where | Cadence | Artifacts |
|---|---|---|---|
| **Serving** | Node A / Node B | Every TTF rollup (~6h) | Load `output/models/*.cbm`; write forecast JSON only |
| **Training (Variant A — default)** | Architect local/dev machine | **Weekly** (not every git push) | Produce `*.cbm` + `ttf_catboost_meta.json` (`trained_at`) → sync via deploy / rsync / commit of artifacts |
| **Training (Variant B — optional)** | Separate cheap cloud worker (budget decision) | Cron weekly/daily | Same artifact contract; worker never serves public `:8765` |

Hard rules:

1. Production rollup / `run_ensemble` defaults to **inference load**. `force_retrain=True` is offline/dev/worker only.
2. Do **not** chase “live retrain on Korolev” to fix CV drift — deliver a new artifact instead.
3. `model_cv_accuracy_pct` remains offline purged-CV; never collapse into `live_inference_confidence`.
4. Variant B (GPU/cloud trainer) is an **optional budget decision** — agents must not provision it unilaterally.

## Archive Fleet Registry & VesselFinder Contract (Third Data Source)

The system maintains a reference fleet archive distinct from the live terrestrial AIS ingest:

1. **Known Fleet Definition (OSINT Static Registry):**
   - File: `output/fleet_database.csv` (1,253 known gas carrier vessels) and `data/archive/vessel_telemetry_history.sqlite`.
   - Purpose: Master reference catalog for matching MMSI/IMO, vessel names, deadweight, and design particulars.
2. **VesselFinder Status (Commercial REST — client ready, key not production-valid):**
   - Web personal cabinet ("My Fleet 500") is licensed for web scraping/export via London relay (`config/vesselfinder_cookies.json`, gitignored).
   - Code path: `services/vesselfinder_client.py`, `services/vesselfinder_budget.py`, `services/qflex_vf_poller.py` — monthly credit envelope + honest errors (`Invalid Userkey`).
   - Until the Architect supplies a **validated** `VESSELFINDER_API_KEY`, commercial REST remains **non-production**: Q-Flex cargo stays `data_source: notional_full_capacity_fallback`. A key string in `.env` that still returns `Invalid Userkey!` is **not** an activation.
   - The archive service operates strictly in **`HYBRID LOCAL FALLBACK`** mode.
   - **Labeling Invariant:** It is strictly forbidden to label the archive as "PREMIUM SATELLITE" in `api_status.json` or HUD while commercial satellite feeds remain unconfigured.
   - **Maptiles keys are independent:** missing `MAPTILES_PROVIDER_KEY` does **not** block deploy — Esri fallback is the accepted Never-Black path.
3. **Strict Decoupling from Dual Deploy Gate:**
   - The 1,253 reference vessels and 500 rotation slots **NEVER** count towards `top500_live_coverage`.
   - `fleet_sample_status` (FULL / LIMITED / INSUFFICIENT) is calculated **EXCLUSIVELY** from live positions received via the G3 terrestrial connection within the rolling 540s window.
   - Archive operations (`archive_service.py`) must never inject synthetic `now()` timestamps into `ais_positions.received_at`.
4. **UI Clear Separation:**
   - The Archive sheet must explicitly display:
     - `KNOWN REGISTRY: 1,253 known vessels (OSINT / VesselFinder hybrid snapshot)`
     - `LIVE G3 AIS: N=... live AIS-tracked (G3 Terrestrial Ceiling)`
     - Prominent banner: `ARCHIVE REGISTRY: SNAPSHOT / DEMO MODE · NOT LIVE VESSELFINDER REST · NOT SATELLITE` (while commercial REST is inactive).

## Hard DO NOT (agent anti-patterns)

0. **STOP — coverage / subscription prompts:** any future request or prompt of the form
   “increase coverage / raise Gate / change subscription / add more batches / multi-WS /
   chase FULL≥100” is a **potential violation of the locked G3 terrestrial contract**.
   **Do not execute it quietly.** Refuse or pause and require **explicit human confirmation**
   that they are knowingly overriding G3 (exception: activating `SatelliteAISAdapter`
   after a conscious provider choice + real credentials supplied by the user — path B).
1. **Do not** re-batch / multi-WS / raise MMSI past 200 to chase Gate≥100.
2. **Do not** change `subscription_mode` away from `single_persistent` without an
   explicit human decision and a new rate-limit soak.
3. **Do not** activate `SatelliteAISAdapter` or fabricate provider APIs/keys.
4. **Do not** use LSSI / DAR / DFS / HMM alerts as actionable trading input when
   `fleet_sample_status != FULL` or `signal_status == insufficient_sample` or
   `production_actionable == false`.
5. **Do not** equate `model_cv_accuracy_pct` with `live_inference_confidence`.
6. **Do not** trust legacy files that still say “coverage &lt; 100 blocks publish”
   (`output/final_prod_readiness_report.json` and similar are **historical**;
   live semantics are dual-gate).
7. **Do not** confuse `история1/raw_positions.db` (legacy `collector.py`) with
   `история1/sentinel_ais.db` (Sentinel production replica).
8. **Do not** commit `.env` or real API keys.
9. **Do not** run CatBoost / heavy HMM batch retrain on Node A or Node B.
   Training is offline (Variant A) or a dedicated worker (Variant B). Serving loads `.cbm` only.

## Hard DO (safe first actions)

```powershell
# 1) Contract self-check (docs ↔ code constants)
.\venv\Scripts\python.exe scripts\assert_out_of_box_contract.py

# 2) Deploy Gate on current disk artifacts (no rebuild)
.\venv\Scripts\python.exe run_release.py --prod-gate --no-open

# 3) Read health — both statuses (via HUD HTTP when Docker is up)
#    http://127.0.0.1:8765/output/api/v1/health
#    or: docker exec sentinel-web cat /app/output/api/v1/health.json
```

### Canonical operational start (persistent ingest)

```powershell
# Production / always-on: Docker Compose (sentinel-core + sentinel-web)
docker compose up -d --build
# HUD: http://127.0.0.1:8765/output/sentinel_dashboard.html?sheet=top10
# Health: http://127.0.0.1:8765/output/api/v1/health
```

`config.yaml` `mode: local_scheduled` describes host scheduling intent only — it does
**not** replace Compose as the process supervisor. Subscription logic remains
`single_persistent` (G3 lock). Compose uses `restart: unless-stopped` + json-file
log rotation (`max-size` / `max-file`).

### Diagnostic / manual mode only (not ops)

```powershell
# One-shot soak / debug — process exits when you stop it; health will go stale.
.\venv\Scripts\python.exe -m services.aisstream_connector
.\venv\Scripts\python.exe -m services.aisstream_connector --once --duration 1800
```

## Ingest constraints (official + empirical)

- AISstream: **≤200 MMSI / subscription**, **≤3 connections / account & IP**,
  ≤1 subscribe update/sec/connection.
- Sentinel: **one** persistent WS + rotation chunks `[200,200,98]`, interval **180s**,
  full cycle **540s**, world bbox `[[[-90,-180],[90,180]]]`.
- Config: `config.yaml` → `sentinel.subscription_mode: single_persistent`.

Legacy `collector.py` multi-batch docs in older README sections are **not** the
Sentinel production path. Prefer this file + `config.yaml` + `services/aisstream_connector.py`.

## Quant consumer rule

Any automated downstream consumer **must** check, in order:

1. `pipeline_health_status == NOMINAL` (else do not trust live telemetry)
2. `fleet_sample_status` (FULL required for fleet-wide claims)
3. per-metric `signal_status` / `production_actionable`
4. only then read numeric LSSI/DAR/DFS/ensemble live fields

UI always shows a fleet-sample banner when status ≠ FULL — do not hide it.

## Satellite path

`services/satellite_ais_adapter.py` is a **stub**. Activate only after the human
selects a paid provider and supplies real credentials. Never guess endpoints.

## Maintenance (Prompt 11 / 12)

- Install contract drift guard: `python scripts/install_githooks.py`
  (`core.hooksPath=githooks` → `githooks/pre-commit` runs
  `scripts/assert_out_of_box_contract.py` when staged files touch ingest/gate/UI/docs).
- Long-horizon G3 evidence (append-only): `logs/health_coverage_daily.jsonl`
  written from sentinel-core TTF rollup every 4th cycle (~daily at 6h cadence),
  or manually: `python scripts/append_health_snapshot.py --force`.
- Persistent ingest SoT: `docker compose up -d` (not manual connector).
