# Changelog

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
