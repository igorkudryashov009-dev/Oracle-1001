# AGENTS.md — Oracle-1001 / Sentinel (read before any change)

**Contract-Version:** `1.0.0-prompt12` · **Last-Revised:** `2026-09-10` · Persistent ingest ops  
Any edit to this file is a **versioned event** — bump Contract-Version and add a CHANGELOG.md entry in the same change.

This file is the **binding operational contract** for humans and AI agents opening
the repo for the first time. If anything else (old README sections, stale JSON
reports under `output/`, chat history) conflicts with this file — **this file wins**.

## What this system is

- Dual-plane OSINT + quant HUD over a **terrestrial** AIS free-tier feed (AISstream).
- Production-ready as: **pipeline healthy · fleet sample LIMITED · quant caveated**.
- **Not** a source of statistically valid fleet-wide trading signals at current N.

## What this system is NOT

- Not broken because `top500_live_coverage` is 3–5 (or briefly 0–1). That is **G3**:
  physical terrestrial AIS ceiling, confirmed by a clean 60-min soak (peak≈5, unique≈7/h)
  and a 25-min trend (0→5). Do **not** “fix coverage” by re-tuning subscription.
- Not authorized to invent satellite AIS credentials/endpoints.
- Not authorized to treat high `model_cv_accuracy_pct` as live fleet confidence.

## Dual Deploy Gate (do not collapse these)

| Field | Values | Blocks `--prod-rebuild` / `--prod-gate`? |
|---|---|---|
| `pipeline_health_status` | NOMINAL / DEGRADED / CRITICAL | **YES** — only NOMINAL publishes |
| `fleet_sample_status` | FULL (≥100) / LIMITED (≥5) / INSUFFICIENT (&lt;5) | **NO** — informational + UI banner |

Source of truth: `services/dual_gate.py`, `services/release_gate.py`.

Thresholds (empirical, do not invent new ones without a new soak):

- `FLEET_SAMPLE_FULL_MIN = 100`
- `FLEET_SAMPLE_LIMITED_MIN = 5`  ← Prompt-7 observed peak
- `FLEET_WIDE_METRIC_MIN_N = 30`  ← LSSI/DAR/DFS production eligibility
- Canonical HTTP port: **8765** only (8478 = CRITICAL drift)

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
