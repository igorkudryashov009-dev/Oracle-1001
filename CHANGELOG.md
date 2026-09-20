# Changelog

## [1.8.0-ops-gis-sot] - 2026-09-20

### Added
- **Unified GIS SoT Registry (`compressor_stations.py`)**: Integrated single source of truth containing 185 gas compressor station nodes with validated WGS84 bounding boxes (`[lon_min, lat_min, lon_max, lat_max]`).
- **Sub-Registry Compatibility Layers**: Re-exported `COMPRESSOR_STATIONS` (50 Core RU), `ADDITIONAL_COMPRESSOR_STATIONS` (50 Additional/UGS), `TURKMENISTAN_COMPRESSOR_STATIONS` (50 Turkmenistan/CAC), and `CHINA_COMPRESSOR_STATIONS` (35 China Import).
- **Unified API registry (`services/config_keys.py`)**: env-only SoT for NEWSAPI / GIE / AISSTREAM / FIRMS / EXCHANGERATE / NASDAQ / BREVO — never hardcodes secrets; `registry_status()` returns masked public blob.
- **Intel adapters (Sections II–V):** `news_service`, `firms_service`, `market_data_service`, `notify_service`; REST `GET /api/v1/news/latest`, `GET /api/v1/gis/firms/anomalies`, `GET /api/v1/market/summary`, `POST /api/v1/alerts/dispatch`; HUD news ticker + FIRMS layer in `web/sentinel_engine.js`; tests `tests/test_sections_ii_v_integrity.py`.
- **Spatial Grid Index (`services/spatial_index.py`)**: Built a zero-dependency, pure-Python Uniform Grid Index providing $O(1)$ average-time point lookups and $O(\log N)$ spatial filtering without C-extensions (`rtree`/`shapely`).
- **Automated GIS Integrity Test Suite (`tests/test_compressor_stations_integrity.py`)**: Added 9 comprehensive pytest scenarios covering spatial limits, count verification (185 nodes), bounding box span constraints, non-fatal overlap checks, and API/catalog alignment.
- **API Domain Registry (`services/compressor_stations.py`)**: 185 frozen `CompressorStation` entries (bbox centroids); `GET /api/v1/gis/compressor-stations`; `POST /api/v1/route/analytics` appends `proximity_compressors` (≤50 nm).
- **Tile proxy (`services/tile_proxy.py`)**: `GET /api/v1/gis/tiles/{provider}/{z}/{x}/{y}.png` (MapTiler / Mapbox / Esri / OSM); disk cache 30d; SQLite monthly hard-stops 90k/40k; missing key → Esri. Legacy `/api/tiles/` unchanged.
- **AIS tracker (`services/ais_tracker.py`)**: `GET /api/v1/gis/ais/{status,positions,vessel/{imo}}`; local G3 cache → `data/ais_history.db` (6h TTL); `$30/mo` budget hard-stop scaffold; no invented satellite APIs; `X-AIS-Source` header.

### Changed
- **API Domain Service (`services/compressor_stations.py`)**: Updated REST endpoints and internal helper routines to ingest `ALL_COMPRESSOR_STATIONS` re-exports dynamically; `find_nearest_stations` / `stations_at_point` use the grid index for candidate prune.
- **Contract Header Enforcement**: Added `X-Contract-Version: 1.8.0-ops-gis-sot` across all Edge (`:8765`) and Micro-API (`:8766`) responses.
- **Route sheet payload**: `build_route_analytics_payload` attaches per-vessel proximity from last track point.
- **Honesty**: `capacity_bcm_y` = corridor-class notional estimate (not live SCADA).
- **Contract**: AGENTS.md → `1.8.0-ops-gis-sot`.

### Verified
- **OOB Pipeline Checks**: Executed `tests/test_compressor_stations_integrity.py` with **9/9 PASSED** (0.71s execution latency).
- **Runtime Proof**: Validated Portovaya proximity search (`KS_10_Portovaya_NordStream1` at 0 nm offset).

## v1.7.0-autodiscover-oracle-sot — Auto-discovery manifest · Oracle Dual Gate SoT (2026-09-20)

Closes the recurring "new code exists locally, not in the Node A image" class of failure and the third Dual Gate threshold twin in JS:

- **Deploy auto-discovery:** `verify_deploy_manifest.py` / `write_deploy_manifest.py` build the critical-asset list from globs (`web/js/**/*.js`, `web/**/*.css`, `services/**/*.py`, arctic videos, GLBs) with explicit excludes. Legacy required basenames stay as a regression lock. New Oracle files are detected automatically (no hand list).
- **Oracle single SoT:** `dual_gate.export_dual_gate_thresholds()` → `health.thresholds`; `oracle_engine.js` / `oracle_sheet.js` apply via `oracle_applyThresholdsFromHealth` — no hardcoded `FLEET_SAMPLE_*` / disk cutoffs in the client.
- **Bake proof:** `deploy_korolev_sentinel.sh` BAKE_OK asserts `oracle_engine.py` / Oracle JS / no hardcoded LIMITED_MIN in the image volume.
- **Contract:** AGENTS.md → `1.7.0-autodiscover-oracle-sot`.

## v1.6.4-oob-seal — Unified OOB provenance · ML utc · disk/3D seal (2026-09-16)

Closes the five-step out-of-the-box stabilization pack without Dual Gate threshold drift:

- **Archive / VF:** HUD + `api_status` label hybrid path as `SNAPSHOT / DEMO MODE`; KNOWN vs LIVE G3 split wording locked; `fleet_sample_status` remains G3-only.
- **Disk:** keep `DISK_FREE_MIN_PCT=20` / CRITICAL `10` (reject ad-hoc 15%); ops prune + `log_retention` for WAL/logs on Node A.
- **3D:** canonical GLB path `output/assets/3d_models/` (already in `web_assets_sync`); sheet-leave pause also covers Route; modal DIGITAL TWIN remains real GLB (card parallax is Never-Black photo layer, not a 3D substitute).
- **ML:** `/api/v1/quant/risk` adds `model_last_retrained_utc` + `model_provenance: "offline_batch"` (inference-only on Node A/B).
- **Contract:** AGENTS.md → `1.6.4-oob-seal`.

## v1.6.3-image-bake-proof — P0 quant/health SoT + top10 manifest permanent bake (2026-09-16)

Closes the docker-cp class of risk for the Dual-Gate split and known HUD drift:

- **P0 quant/health dual-source-of-truth split — resolved** (code in `4b31381`): `quant_risk_service._resolve_live_dual_gate()` uses live `build_health_document()`; stale disk `health.json` is no longer the gate source. This bake puts that layer into the Korolev image and proves survival via `down` + `up --force-recreate` (not a live-container `docker cp`).
- **top10_vessels_manifest.js sync restored** (content in `f4c039e`): LIJMILIYA DWT `155159` + provenance shipped via Sync-Tree → image/volume seed; Node A served bytes must MATCH working tree.
- **London SoT:** lean services pack (no HUD GLB/mp4) so Node B receives identical `services/quant_risk_service.py` / `top10_vessels.py` without hung full-tree scp.
- **Contract:** AGENTS.md → `1.6.3-image-bake-proof`.

## v1.6.2-particulars-provenance — LIJMILIYA DWT + Particulars contract (2026-09-16)


- **LIJMILIYA (IMO 9388819):** DWT `130000` → `155159` after Architect VesselFinder page-dump manual verification. Prior value was Q-Max class placeholder (not cargo m³ confusion). LOA/Beam/current draft 9.2 m matched dump; design draught `13.70` m recorded separately.
- **Particulars provenance:** AGENTS.md section documents catalog SoT, manual_verification workflow, and VF API as future auto-check path.
- **Fleet notional value:** rebuild `output/qflex_fleet_cargo.json` from updated catalog DWT (still `notional_full_capacity_fallback`).

## v1.6.1-arctic-user-frames — User ARCTIC stills · Quant Dual-Gate SoT (2026-09-16)


- **ARCTIC frames:** Architect-provided stills from `C:\111\1001\Artic` **replace** prior agent ffmpeg extracts as primary VIDEO-DERIVED sources (agent frames archived under `assets/arctic/frames/_agent_ffmpeg_extracted/`). Badge unchanged: extracted from AI flight · not independent photographs. Ushakov inventory fact: `-2-1.png`, `-2-2.jpg`, `-2-3.jpg` (symmetric 3+3 with Margerie). Overhead stills (`*-1-3` / `*-2-3`) enable VIDEO-DERIVED TOP when present; still not Ortho / not measurement.
- **Quant SoT:** `quant_risk_service` resolves gate via live `build_health_document()` — closes health=NOMINAL vs quant=CRITICAL split from stale disk `health.json`.
- **Contract:** AGENTS.md → `1.6.1-arctic-user-frames`.

## v1.6.0-arctic-tiles-vf — ARCTIC sheet · tile proxy · VesselFinder client (2026-09-16)

Ships the architect-local honest-fallback delta to Node A/B **without** waiting on paid keys.

- **ARCTIC:** Arc7 sheet (`web/js/arctic_sheet.js` + manifest + `assets/arctic` videos/frames). LUMA-generated flight video = same trust tier as Q-Flex REAL VIDEO; `VIDEO-DERIVED VIEWS` ≠ Ortho Triplet / not measurement; `TOP VIEW UNAVAILABLE` when nadir absent.
- **Maptiles:** same-origin `/api/tiles/` proxy (`services/maptiles_proxy.py` + budget); provider key server-side only; Esri World Imagery Never-Black fallback when `MAPTILES_PROVIDER_KEY` absent/fails.
- **VesselFinder:** client + budget + Q-Flex poller wired; cargo/fleet value stays `notional_full_capacity_fallback` until a validated API key exists.
- **Deploy hardening:** stop blanket `--exclude=*.mp4`; pack `assets/arctic`; seed named `output_artifacts` volume from host Sync-Tree after bake (`SEEDED_OUTPUT_VOLUME`); `verify_deploy_manifest.py` covers arctic JS/assets.
- **Contract:** AGENTS.md → `1.6.0-arctic-tiles-vf`. Missing maptiles/VF keys are an accepted prod state, not a deploy blocker.

## v1.5.0-baked — Full Deploy-TwoNode image bake (2026-09-13)

Closes P0 “ssh-memory”: prompts 1–5 hotfixes are no longer `docker cp` + restart only.

- **Bake path:** `deploy_korolev_sentinel.sh` → `build --no-cache` + `compose down` + `up --force-recreate`; in-container `BAKE_OK` asserts disk gate, archive honesty, `log_retention`, quant service.
- **Deploy-TwoNode.ps1:** packs `api_server.py`, `output/assets` (no `*.glb` exclude), `output/models`, `output/archive/api_status.json`; syncs London stage → `install_london_ais_relay.sh` (same services SoT on B).
- **Contract:** AGENTS.md → `1.5.0-baked` — permanent-in-image vs temporary-in-container lock.

## v1.4.0-consolidated — Archive · Digital Twin · Offline ML · Disk Gate (2026-09-13)

Single Contract-Version bump covering the four post–Prompt-14 operational locks (no per-prompt version spam):

1. **Archive provenance:** VesselFinder commercial REST inactive; HUD/API must label OSINT hybrid fallback (`is_synthetic: true`); KNOWN REGISTRY vs LIVE G3 AIS split; Dual Gate fleet sample from live AIS only.
2. **Digital Twin restore:** `.glb`/voxel sync into `output/assets/3d_models/`; deploy no longer excludes GLB; lazy-unmount + Never-Black inspector; Node A serves live GLB (e.g. IMO 9388833).
3. **Offline ML:** Node A/B inference-only; `force_retrain` offline/weekly (Variant A); `/api/v1/quant/risk` exposes `model_last_retrained`.
4. **Disk gate:** cleanup freed ~3.5G on Korolev; `DISK_FREE_MIN_PCT=20` / `CRITICAL=10`; `log_retention` for jsonl + corrupt_backup.

Supersedes interim labels `1.2.0-prompt14` … `1.3.1-prompt15b` as the binding version.

## Prompt 15b — Disk headroom gate + retention (v1.3.1-prompt15b)

- **Inventory (Node A):** primary reclaim was `corrupt_backup` (~1.1G duplicate salvage dumps) + stale `analytical_engine/история1` 543M copy; live WAL is small (~3MB). Docker json-file 20m×5 already on; host jsonl/backups had no retention.
- **Retention:** `services/log_retention.py` — jsonl ≤90d / 50MiB; corrupt_backup keep newest 1 (≤200MiB). Hooked into health snapshot + sentinel-core rollup.
- **Gate:** `DISK_FREE_MIN_PCT=20` → DEGRADED; `DISK_FREE_CRITICAL_PCT=10` → CRITICAL. `health.json` exposes `disk_free_pct` / `disk`.
- AGENTS.md Contract-Version bumped to `1.3.1-prompt15b`.

## Prompt 15 — ML Serving ≠ Training (v1.3.0-prompt15)

- **Architecture lock:** Node A/B are inference-only for CatBoost. Heavy retrain is offline (Variant A, weekly on architect/dev machine) or optional dedicated cloud worker (Variant B — budget decision, not auto-provisioned).
- **Code:** `run_ensemble` / TTF rollup default to `load_catboost_for_inference`; `force_retrain=True` is the only full-train path. Meta writes `trained_at` / `model_last_retrained`.
- **Honesty:** `/api/v1/quant/risk` exposes `model_last_retrained` (CBM mtime / meta). `model_cv_accuracy_pct` remains offline purged-CV (h14 dirAcc 75%).
- AGENTS.md Contract-Version bumped to `1.3.0-prompt15`.

## Digital Twin restore — deploy sync + lazy-unmount (Node A)

- **Root cause (not GPU):** `.glb` were already in Docker volume `sentinel_output_artifacts`; `vessel_*_voxels.json` were on host `web/assets/3d_models/` but never copied into `/output/`. `Deploy-TwoNode.ps1` excluded `*.glb`; `web_assets_sync` previously synced only JS/CSS.
- **Sync fix:** `services/web_assets_sync.py` now copies `.glb` / `.json` / `.gltf` from `web/assets/3d_models/` → `output/assets/3d_models/`. `Deploy-TwoNode.ps1` drops `--exclude=*.glb` and includes `output/assets`.
- **UI restore:** Inspector default hierarchy = Digital Twin GLB → Voxel → Video → Ortho (Never-Black per vessel). Lazy-unmount: pause card hover videos when modal opens; dispose GLB/voxel on tab switch / sheet leave. Cache-bust `?v=glb-twin-v6`. CSS: hide `#sheet-balance` when `data-sheet=top10` (balance bleed fixed).
- **Verify:** Node A `45.8.230.214:8765` — BU SAMRA / MEKAINES render live GLB; screenshot `logs/node_a_glb_restored_9388833.png`.

## Prompt 14 — Archive Registry & VesselFinder Audit Contract (v1.2.0-prompt14)

- **VesselFinder Integration Audit & Honest Categorization:**
  - Audited `services/archive_service.py`, `scripts/vesselfinder_ingest.py`, and `services/key_manager.py`.
  - Confirmed VesselFinder web personal cabinet exists ("My Fleet 500"), but commercial REST API (`api.vesselfinder.com/listmanager`) is inactive due to unvalidated userkey (HTTP 200 `Invalid Userkey!`).
  - Prohibited fraudulent `"api_plan": "PREMIUM SATELLITE"` string; downgraded plan to `"OSINT REGISTRY (HYBRID LOCAL FALLBACK)"`.
  - Added mandatory `is_synthetic: true`, `registry_source: "OSINT static snapshot (fleet_database.csv)"`, and descriptive disclaimer to `output/archive/api_status.json`.
- **Strict Decoupling of Known Fleet Registry and Live Fleet (Dual Gate):**
  - Clarified distinction: "1,253 known vessels" is an OSINT reference catalog; "N=... live AIS-tracked" is live G3 terrestrial coverage.
  - Hard invariant: reference fleet registry and 500 rotation slots NEVER contribute to `top500_live_coverage` or affect `fleet_sample_status` (FULL / LIMITED / INSUFFICIENT).
  - Fixed timestamp injection in `services/archive_service.py` to preserve original historical timestamps in `ais_positions.received_at`.
- **Archive HUD Interface Overhaul:**
  - Added high-contrast notice banner on Archive sheet: `ARCHIVE REGISTRY: STATIC OSINT SNAPSHOT · NOT A LIVE THIRD-PARTY FEED`.
  - Split KPI metrics into distinct labels: `KNOWN REGISTRY` (1,253 vessels) vs `LIVE G3 AIS` (N=... live, fetched from `/output/api/v1/health`).
- AGENTS.md Contract-Version bumped to `1.2.0-prompt14`.

## Prompt 13 — Two-Node Topology, Unified Dual Gate SoT & Quant Risk Contract (v1.1.0-prompt13)

- **Two-Node Physical Topology:**
  - Node A (Korolev, `45.8.230.214`): Analytical core + Public Edge Gateway on `:8765`.
  - Node B (London, `185.39.19.75`): Hot-standby edge relay with continuous unidirectional SQLite replication (`sync_ais_db_to_korolev.sh`).
  - Active node designation: added `active_node: "korolev" | "london"` in `GateStatus`, `health.json`, and `QuantRiskMetrics`.
  - Failover Cutover Guard: `check_failover_status` forces `pipeline_health_status = DEGRADED` (`failover_cutover_awaiting_first_edge_sync`) until post-cutover London sync arrives, eliminating false `NOMINAL` windows on stale data.
- **Unified Dual Deploy Gate Source of Truth:**
  - Refactored `api_server.py`, `services/ais_health.py`, and `services/quant_risk_service.py` to directly invoke `services/dual_gate.py`.
  - Prohibited parallel or duplicated threshold calculations (`lag < 300` / `coverage >= 100`).
- **Network Port Topology & Reverse Proxy:**
  - Public Edge `:8765` (`sentinel-web` / `serve_dashboard.py`) reverse-proxies `/api/v1/quant/*` to internal loopback `:8766` (`api_server.py`) with in-process fallback.
- **Quant Risk Anti-Hallucination & Synthetic Data Contract:**
  - Enforced mandatory `is_synthetic: bool` and `synthetic_components: list[str]`.
  - When paper ledger has \(N < 30\) marks, `is_synthetic = True`, `production_actionable = False`.
  - Frontend visualizer (`uaip_quant_visualizer.js`) renders blocking warning overlay `SYNTHETIC DEMO DATA · NOT WIRED TO LIVE MODEL`.
  - Canonical table name invariant enforced: `ais_positions` (never legacy `positions`).
  - **P0 Strategy Recommendation Gate:** `recommended_strategy_id` and `recommended_strategy_name` are set to `None` whenever `production_actionable == False`. Added `blocked_reason` to explain exact gating reason. UI explicitly renders `NO ACTIONABLE STRATEGY — {blocked_reason}`.
- AGENTS.md Contract-Version bumped to `1.1.0-prompt13`.

## Digital Twin — triplanar projection bounds (v3.4.1)

- GATE REJECT on Prompt-2 screenshot: beige-white formless mass vs maroon ortho truth.
- Root cause: sanitize ``rezero()`` put verts in positive octant while triplanar divided
  by design ``hx/hy/hz`` (centered ``[-h,+h]``) → ~64% verts clipped to ``u=+1`` →
  photo-edge / median fill. SPEC PLATE was inactive (HUD overlay only) — not the cause.
- Fix: ``mesh_projection_frame`` from actual AABB; bake samples centered local verts;
  sanitize recenters at origin instead of rezero. Alg ``top10-glb-v3.4.1-triplanar-bounds``.

## Digital Twin — geometry∥presentation decouple (v3.4.0)

- Structural fix for the v3.0→v3.2.2 regression class: presentation no longer
  mutates geometry. Pipeline split into executable contracts:
  `generate_geometry(masks) → RawMesh` (frozen verts, content-bbox carve only)
  and `apply_presentation(raw, photos) → PresentedMesh` (triplanar + material;
  deep-copy only; runtime hash guard).
- Viewer: Z-up camera framing only (`camera.up=+Z`); removed `rotation.x=-π/2`,
  `heightBoost`, and anisotropic `mesh.scale`. Axis contract asserted in code:
  LOA(X) > Beam(Y) > height(Z).
- Immutability test: `tests/test_geometry_presentation_immutability.py`
  (live `generate_geometry` hash before/after presentation).
- Alg: `top10-glb-v3.4.0-decoupled` (carving still v3.3.0 content-bbox).

## Digital Twin — carving content-bbox UV (v3.3.0)

- Root cause of ribbon occupancy: full-frame UV mapped empty sky/water margins into
  design extents (Side FG ≈24% of frame height → midship z_span_frac≈0.21).
- Fix: content-bbox UV sampling in `carve_voxels`; Side/Bow height scales already
  agreed (~91m) — desync was frame margins, not LOA/Beam constants.
- Visual depth from Side silhouette (clamped); `Z_INFLATE_ITERS=0`,
  `FIT_MESH_TO_EXTENTS=False` (no post-hoc bbox stretch masking).
- Alg: `top10-glb-v3.3.0-content-bbox`. Raw baseline diagnostic updated.

## Digital Twin — splinter / scale×remap fix (v3.2.2-silhouette)

- Root cause: presentation `scale` ran before `rotation.x=-π/2` with undocumented
  local-Z meaning; legacy `scale.z*=2.35` was easy to mis-map onto Beam.
- Fix (Variant A): remap Z-up→Y-up first; then fit + `heightBoost` on **local Z only**
  (= world height) from target L/D≈7.2; axis legend in code.
- Mesh: `TARGET_LD_MAX=8.5`, taller air-draft; alg `top10-glb-v3.2.2-silhouette`.
- Wireframe/orbit default OFF (solid shaded still).
- WebGL policy unchanged (0 grid / 1 modal / 0 after close).

## Prompt 12 — persistent ingest via Docker Compose (canonical ops)


- Canonical start: `docker compose up -d` (`sentinel-core` + `sentinel-web`).
- Manual `python -m services.aisstream_connector` marked **diagnostic/manual only**.
- Compose: keep `restart: unless-stopped`; add json-file log rotation
  (`max-size=20m`, `max-file=5`). Volumes unchanged: `data_sqlite`, `output_artifacts`.
- First-boot note: seed host `история1/sentinel_ais.db` into `7000_data_sqlite`,
  then `chown -R 10001:10001` (sentinel uid) so WAL writes are not readonly.
- `.dockerignore` excludes `output/generative_pilot/` (TripoSR stays isolated).
- AGENTS.md Contract-Version → `1.0.0-prompt12`.

## Coverage window sync — rotation cycle 540s (legacy 720 drift)

- Root cause: `ais_health.DEFAULT_COVERAGE_WINDOW_SEC=720` was leftover from
  `3×240s` before Prompt-7 locked `rotation_interval_seconds=180` → cycle **540s**.
  Not an intentional buffer — unexplained semantic drift vs connector `_chunk_plan`.
- Fix: `coverage_window_from_config()` derives window as `interval × n_chunks`
  from `config.yaml` (same math as connector); connector fallbacks 240→180.
- Contract assert now guards `resolve_coverage_window_seconds()==540`.

## SRE Prompt-4 — dual-gate release / port / asset sync

- `verify_3d_and_health`: GLB >100KiB; port 8765 / reject 8478; DB=`sentinel_ais.db` only;
  web→output MD5 sync blocking; `fleet_sample_status` remains informational.
- `run_release --prod-rebuild --serve`: sync gate + disk verify + mandatory `--require-http`.

- Runtime audits: NaN/Inf verts, zero-area triangle sample, pancake bbox (min/max aspect).
- `triggerTriViewFallback(reason)`: unmount WebGL, Tri-View tabs, OSINT warning banner.
- Tablist: single delegated binder (`data-tabs-bound`); GLB tab `is-degraded` when mesh fails.

- Camera: Box3 + bounding sphere; `near=maxDim/100`, `far=maxDim*100`; Orbit target = bbox center.
- Initial pose from LOA/beam for ~70% canvas fill; HUD `resetCamera()` eases home without wiping zoom envelope.

- Explicit `anisotropic_pitch[px,py,pz]` from LOA/Beam/Depth; isotropic mean banned.
- `_to_unit` clamps L/B∈[5.5,8.5], B/D≈3.5; sanitize + mild Taubin; capsule fallback.
- Meta exports pitch + lb/bd ratios; force-rebuild alg `top10-glb-v2.1`.

- Backend `top10-glb-v2`: anisotropic marching-cubes pitch (was mean → crumpled sheets);
  `sanitize_hull_mesh` (fix normals, Taubin smooth, volume/aspect gates, capsule fallback).
- Runtime viewer: `computeVertexNormals` + DoubleSide + degenerate bbox → Tri-View fallback.

- Clamp hull PBR: metalness `0.08`, roughness `0.65`, envMapIntensity `0.4`;
  clearcoat off; map `SRGBColorSpace`; muted studio env gradient.
- Lights: Ambient `#fff`@1.2 · Key `#f8fafc`@2.2 @(15,25,20) · Rim `#38bdf8`@0.8 ·
  Hemi@0.7; ACES exposure `1.15` (was 1.35).

## TOP-10 GLB viewer — production studio rig

- Lights: Ambient `#fff`@1.5 · Key `#f8fafc`@3.0 @(15,25,20) · Rim `#38bdf8`@1.2 ·
  Hemi@1.0; ACES exposure 1.35; PBR roughness 0.35 / metalness 0.25; hull `#1e293b`.
- Camera: `box.getCenter(controls.target)` + LOA-aware ~70% viewport framing / Reset.

## TOP-10 GLB viewer — silhouette / lighting fix

- Studio lights: Ambient `#fff`@1.2, Key `#f8fafc`@2.5, Rim `#38bdf8`@1.0,
  Hemisphere sky/ground; brighter env + lighter fog; exposure 1.35.
- Material harden: PBR roughness 0.4 / metalness 0.2; naval hull `#1e293b` fallback
  when maps/color missing; camera frames bbox (~70% fill) with LOA bias.

## Vessel Daily Archive sheet

- New immutable table `vessel_daily_archive` in `sentinel_ais.db` (20 params + PK date/imo).
- Worker: `services/archive_snapshot_worker.py` — upserts full fleet (~1253) from
  `fleet_database.csv` + live AIS overlay; exports `output/archive/snapshots/*.json|csv`.
- UI: `build_archive_dashboard.py` + HUD sheet `?sheet=archive` (`web/js/archive_sheet.js`).
- Wired into `run_release.py` steps `4i` / `5e`. Does not change AIS subscription (G3 intact).

## TOP-10 rigid 2-col grid / modal stack

- `.top10-grid`: `repeat(2, minmax(0,1fr)) !important` + `isolation` to stop card bleed;
  modal overlay `z-index: 9999 !important` (`web/css/sentinel_hud.css` + inline build CSS).
- 500 concurrent FiltersShipMMSI **not** implemented — production remains ≤200 MMSI
  rotation over TOP-500 universe (G3 / AISstream hard cap).

## Final production integration — port lock + dual-gate/GLB verify

- `services/utils/canonical_port.py`: reject bind to **8478** (CRITICAL drift);
  wired into `run_server.py`, `scripts/serve_dashboard.py`, `run_release.py`.
- History dossier disclaimer: `sentinel_ais.db` (not legacy `raw_positions.db`).
- Integration path: `--prod-rebuild --serve` (mutually exclusive with `--prod-gate`).

## History dashboard map tiles

- `build_history_dashboard.py` / `output/history_dashboard.html`: Carto `light_all`
  → public `dark_all` basemap (`basemaps.cartocdn.com`) to stop "API KEY REQUIRED" tiles.
- Does **not** change AIS subscription / MMSI quota (G3 + AISstream ≤200 intact).

## verify_3d_and_health pre-deploy gate

- New `scripts/verify_3d_and_health.py`: asserts 10× GLB (>50KB + `glTF` magic),
  `pipeline_health_status==NOMINAL`, AIS lag &lt;300s, UI HTML (disk / optional HTTP).
- `fleet_sample_status` reported only (FULL/LIMITED/INSUFFICIENT) — **never** blocks
  release; `NOMINAL` is not a valid fleet_sample enum (G3 / dual-gate intact).
- Wired into `run_release.py` rebuild pipeline and `--prod-gate` / `--prod-rebuild`.

## TOP-10 Three.js GLB modal viewer

- Sheet `top10` inspector: `web/js/top10_3d_viewer.js` loads `vessel_{IMO}.glb`
  via GLTFLoader + OrbitControls (auto-rotate ≈0.5 RPM, pan, zoom 2–10).
- PBR lighting: ambient `#e0f7fc` @0.6, directional sun (10,20,15) + 2048 soft
  shadows, synthetic NASA HUD env gradient `#030712`→`#0f172a`.
- HUD overlays: Orbit / Wireframe / Measure Specs / Reset Camera.
- Never-black: GLB or WebGL context loss falls back to ortho triplet modal.
- Cards optionally prefer GLB mesh when `vessel.glb` is ready; else parametric PBR.

## TOP-10 photogrammetric GLB meshes

- New pipeline: `services/top10_3d_mesh.py` + `scripts/build_top10_3d_models.py`
  builds binary `.glb` from orthographic triplets (`{rank}-1/2/3.jpg` = side/bow/sat;
  optional `{IMO}_side/_bow/_sat.jpg` aliases).
- Multi-planar voxel carving → marching cubes → decimated mesh + PBR JPEG atlas;
  target ≤1.5 MB/ship → `web/assets/3d_models/vessel_{IMO}.glb` (+ `output/` mirror).
- `run_release.py` step `4h` regenerates only when missing or source hash stale.
- Manifest enrichment: `TOP10_VESSELS[].glb.url` / `ready` / `bytes`.
- Does **not** touch AIS ingest, dual-gate, or satellite adapter (AGENTS.md G3 lock intact).

## Prompt 11.1 — health.json dual-gate retention on connector heartbeat

- `services/ais_health.build_health_document` now always recomputes and stamps
  `pipeline_health_status` / `fleet_sample_status` / `sample_size_caveat` so AIS
  connector heartbeat writes cannot wipe dual-gate fields from `/api/v1/health.json`.
- Does **not** change subscription/coverage (G3 lock intact).

## Prompt 11 — Maintenance Mode (contract drift guard)

- **Contract-Version:** `1.0.0-prompt11` (see `AGENTS.md` header; revise date on edits).
  After the Prompt-11 change set is committed, record short hash here:
  `Contract-Git-Anchor: <fill via git rev-parse --short HEAD>`.
- **Git hook:** `githooks/pre-commit` + `scripts/install_githooks.py`
  (`git config core.hooksPath githooks`). Blocks commits that stage ingest/gate/UI/docs
  surfaces when `scripts/assert_out_of_box_contract.py` is not PASS.
- **Append-only G3 log:** `logs/health_coverage_daily.jsonl` via
  `scripts/append_health_snapshot.py` (every 4th TTF rollup in `run_sentinel_core`).
- **Coverage stop-signal:** AGENTS.md item 0 — coverage/subscription prompts require
  explicit human confirmation (except conscious satellite activation).
- Chain 1→11 closed for exploitation mode (A). Path (B) satellite remains a separate
  human decision.

## Prompt 10 — Out-of-box misuse prevention

- **Contract files:** root `AGENTS.md` (binding for humans/agents) +
  `.cursor/rules/sentinel-envelope.mdc` (`alwaysApply: true`).
- **README:** leading `READ THIS FIRST` banner; obsolete “50 MMSI / parallel
  ceil(N/50)” guidance removed from the current-facts section; legacy
  `collector.py` multi-batch path explicitly marked non-Sentinel-prod.
- **Self-check:** `scripts/assert_out_of_box_contract.py` verifies docs↔
  `dual_gate` constants, `single_persistent` config, satellite stub, and
  flags historical readiness reports that still narrate coverage-gate=100.
- **Definition of done:** next opener cannot reasonably confuse G3 terrestrial
  ceiling with a code bug, publish Gate with coverage Gate, CV accuracy with
  live inference confidence, or activate satellite with invented credentials.

## Dual Deploy Gate (fleet sample LIMITED / terrestrial AIS)

- **Breaking semantics (publish):** `--prod-rebuild` / `--prod-gate` fail only when
  `pipeline_health_status != NOMINAL`. Coverage no longer blocks publish.
- **Informational:** `fleet_sample_status` = `FULL` (≥100) / `LIMITED` (≥5, observed
  terrestrial peak from Prompt-7 60-min soak) / `INSUFFICIENT` (&lt;5). Always published
  in `health.json` and UI banner.
- **Quant guardrails:** LSSI/DAR/DFS marked `insufficient_sample` when N &lt; 30;
  `model_cv_accuracy_pct` vs `live_inference_confidence` separated in ensemble/quant.
- **Adapter socket:** `AISSourceAdapter` + inactive `SatelliteAISAdapter` stub
  (no credentials / endpoints fabricated).
- **Downstream rule:** any automated consumer of fleet-wide quant signals **must**
  check `fleet_sample_status` (and `signal_status`) before acting — do not read bare
  numeric LSSI/DAR/DFS alone.
