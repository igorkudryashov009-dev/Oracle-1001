# Sentinel Operational Anomalies Report

- Generated (UTC): `2026-09-09T14:03:12.888679Z`
- Source DB: `./история1/sentinel_ais.db` (65,097,728 bytes)
- Requested lookback: last **24h** (`2026-09-08T14:01:17.413758Z` → `2026-09-09T14:01:17.413758Z`)
- Observed AIS span in DB: `2026-09-01T00:00:00Z` → `2026-09-09T14:01:17.413758Z` (**206.02h** available)
- Positions total / registry-tiered: **192,188** / **1**

## Summary

| Category | Count |
|----------|------:|
| STS operations (< 0.5 nm, SOG < 1.0 kn) | **0** |
| Dark AIS (gap > 4h → critical zone) | **0** |

## 1. STS Operations (Ship-to-Ship)

Criteria: both vessels in Alpha–Delta registry, contemporaneous (±10 min bucket), distance **< 0.5 nm**, SOG **< 1.0 kn**.

_No STS proximity pairs detected in the available window._

## 2. Dark AIS Activity

Criteria: Alpha/Bravo only; consecutive-track gap **> 4 hours**; re-acquisition inside chokepoint or STS hub.

_No Dark AIS gap→critical-zone reacquisition events detected._

## Method notes

- STS pairing uses time-bucketed co-location (not full O(n²) continuous tracks).
- Dark AIS requires both sides of the gap inside the local SQLite retention window.
- Critical zones = Sentinel chokepoints + Fujairah / Laconian / Ceuta / GoG / Singapore STS hubs.
- Empty telemetry windows emit this report with zero counts (non-fatal).
