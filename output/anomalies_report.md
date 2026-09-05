# Sentinel Operational Anomalies Report

- Generated (UTC): `2026-09-05T12:31:00.405310Z`
- Source DB: `C:/Users/MSI/Oracle-1001/7000/история1/sentinel_ais.db` (40,955,904 bytes)
- Requested lookback: last **24h** (`2026-09-04T11:10:45.374942Z` → `2026-09-05T11:10:45.374942Z`)
- Observed AIS span in DB: `2026-09-05T08:13:58.278220Z` → `2026-09-05T11:10:45.374942Z` (**2.95h** available)
- Positions total / registry-tiered: **119,906** / **3,981**

## Summary

| Category | Count |
|----------|------:|
| STS operations (< 0.5 nm, SOG < 1.0 kn) | **9** |
| Dark AIS (gap > 4h → critical zone) | **0** |

> **Data caveat:** continuous ingest window is only **2.95h**, which is shorter than the Dark AIS threshold (4h). Zero Dark events in this run are expected until retention exceeds the gap threshold.

## 1. STS Operations (Ship-to-Ship)

Criteria: both vessels in Alpha–Delta registry, contemporaneous (±10 min bucket), distance **< 0.5 nm**, SOG **< 1.0 kn**.

| # | Vessel A | Vessel B | Min dist (nm) | Contact window (UTC) | Lat / Lon | Zone / hub | Samples |
|--:|----------|----------|--------------:|---------------------:|-----------|------------|--------:|
| 1 | CLEAN RESOLUTION : (LNG Tanker / Large) (IMO 9943475, ALPHA, risk=LOW, SOG=0.0 kn) | CELSIUS CHARLOTTE (IMO 9878711, CHARLIE, risk=LOW, SOG=0.0 kn) | 0.228 | 2026-09-05T08:23:50.867447Z → 2026-09-05T11:05:55.460989Z | 27.88043, -97.26884 | Open water / other | 17 |
| 2 | NAVIGATOR 7189 (IMO 9997189, DELTA, risk=LOW, SOG=0.0 kn) | STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) | 0.364 | 2026-09-05T08:29:14.361648Z → 2026-09-05T11:05:36.095940Z | 30.01047, -93.9842 | Open water / other | 17 |
| 3 | COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) | NAVIGATOR 7189 (IMO 9997189, DELTA, risk=LOW, SOG=0.0 kn) | 0.299 | 2026-09-05T08:26:52.771998Z → 2026-09-05T11:05:36.095940Z | 30.00851, -93.98705 | Open water / other | 16 |
| 4 | COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) | STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) | 0.369 | 2026-09-05T08:26:52.771998Z → 2026-09-05T11:05:14.918090Z | 30.01167, -93.98703 | Open water / other | 16 |
| 5 | BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) | COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) | 0.356 | 2026-09-05T08:23:07.904159Z → 2026-09-05T11:09:28.198317Z | 30.01204, -93.99155 | Open water / other | 15 |
| 6 | ENERGY GUARDIANIMO : 1050117 (IMO 1050117, CHARLIE, risk=LOW, SOG=0.0 kn) | COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) | 0.210 | 2026-09-05T08:35:25.536309Z → 2026-09-05T11:08:24.292053Z | 30.01008, -93.99161 | Open water / other | 12 |
| 7 | ENERGY GUARDIANIMO : 1050117 (IMO 1050117, CHARLIE, risk=LOW, SOG=0.0 kn) | BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) | 0.232 | 2026-09-05T08:35:25.536309Z → 2026-09-05T11:09:28.198317Z | 30.01255, -93.9937 | Open water / other | 12 |
| 8 | BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) | STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) | 0.487 | 2026-09-05T08:17:08.599105Z → 2026-09-05T10:29:17.036715Z | 30.01404, -93.98864 | Open water / other | 9 |
| 9 | HASBAH (IMO 9986075, BRAVO, risk=LOW, SOG=0.0 kn) | VANTAGE LNG (IMO 9970674, CHARLIE, risk=LOW, SOG=0.0 kn) | 0.453 | 2026-09-05T09:58:13.527412Z → 2026-09-05T11:02:11.252602Z | 1.17793, 103.77309 | Strait of Malacca | 3 |

### STS detail cards

#### STS-01
- **A:** CLEAN RESOLUTION : (LNG Tanker / Large) (IMO 9943475, ALPHA, risk=LOW, SOG=0.0 kn) · MMSI `256232000`
- **B:** CELSIUS CHARLOTTE (IMO 9878711, CHARLIE, risk=LOW, SOG=0.0 kn) · MMSI `538009152`
- **Min distance:** 0.228 nm
- **Window:** `2026-09-05T08:23:50.867447Z` → `2026-09-05T11:05:55.460989Z`
- **Position:** 27.88043, -97.26884 · **Area:** Open water / other
- **Registry status:** A=ALPHA/LOW; B=CHARLIE/LOW

#### STS-02
- **A:** NAVIGATOR 7189 (IMO 9997189, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `538011891`
- **B:** STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `636021206`
- **Min distance:** 0.364 nm
- **Window:** `2026-09-05T08:29:14.361648Z` → `2026-09-05T11:05:36.095940Z`
- **Position:** 30.01047, -93.9842 · **Area:** Open water / other
- **Registry status:** A=DELTA/LOW; B=DELTA/LOW

#### STS-03
- **A:** COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `228474800`
- **B:** NAVIGATOR 7189 (IMO 9997189, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `538011891`
- **Min distance:** 0.299 nm
- **Window:** `2026-09-05T08:26:52.771998Z` → `2026-09-05T11:05:36.095940Z`
- **Position:** 30.00851, -93.98705 · **Area:** Open water / other
- **Registry status:** A=DELTA/LOW; B=DELTA/LOW

#### STS-04
- **A:** COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `228474800`
- **B:** STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `636021206`
- **Min distance:** 0.369 nm
- **Window:** `2026-09-05T08:26:52.771998Z` → `2026-09-05T11:05:14.918090Z`
- **Position:** 30.01167, -93.98703 · **Area:** Open water / other
- **Registry status:** A=DELTA/LOW; B=DELTA/LOW

#### STS-05
- **A:** BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `563101300`
- **B:** COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `228474800`
- **Min distance:** 0.356 nm
- **Window:** `2026-09-05T08:23:07.904159Z` → `2026-09-05T11:09:28.198317Z`
- **Position:** 30.01204, -93.99155 · **Area:** Open water / other
- **Registry status:** A=DELTA/LOW; B=DELTA/LOW

#### STS-06
- **A:** ENERGY GUARDIANIMO : 1050117 (IMO 1050117, CHARLIE, risk=LOW, SOG=0.0 kn) · MMSI `538012413`
- **B:** COURCHEVEL (IMO 9983504, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `228474800`
- **Min distance:** 0.210 nm
- **Window:** `2026-09-05T08:35:25.536309Z` → `2026-09-05T11:08:24.292053Z`
- **Position:** 30.01008, -93.99161 · **Area:** Open water / other
- **Registry status:** A=CHARLIE/LOW; B=DELTA/LOW

#### STS-07
- **A:** ENERGY GUARDIANIMO : 1050117 (IMO 1050117, CHARLIE, risk=LOW, SOG=0.0 kn) · MMSI `538012413`
- **B:** BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `563101300`
- **Min distance:** 0.232 nm
- **Window:** `2026-09-05T08:35:25.536309Z` → `2026-09-05T11:09:28.198317Z`
- **Position:** 30.01255, -93.9937 · **Area:** Open water / other
- **Registry status:** A=CHARLIE/LOW; B=DELTA/LOW

#### STS-08
- **A:** BW YUSHI (IMO 9810044, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `563101300`
- **B:** STL YANGTZE (IMO 9918171, DELTA, risk=LOW, SOG=0.0 kn) · MMSI `636021206`
- **Min distance:** 0.487 nm
- **Window:** `2026-09-05T08:17:08.599105Z` → `2026-09-05T10:29:17.036715Z`
- **Position:** 30.01404, -93.98864 · **Area:** Open water / other
- **Registry status:** A=DELTA/LOW; B=DELTA/LOW

#### STS-09
- **A:** HASBAH (IMO 9986075, BRAVO, risk=LOW, SOG=0.0 kn) · MMSI `636025940`
- **B:** VANTAGE LNG (IMO 9970674, CHARLIE, risk=LOW, SOG=0.0 kn) · MMSI `538011431`
- **Min distance:** 0.453 nm
- **Window:** `2026-09-05T09:58:13.527412Z` → `2026-09-05T11:02:11.252602Z`
- **Position:** 1.17793, 103.77309 · **Area:** Strait of Malacca
- **Registry status:** A=BRAVO/LOW; B=CHARLIE/LOW

## 2. Dark AIS Activity

Criteria: Alpha/Bravo only; consecutive-track gap **> 4 hours**; re-acquisition inside chokepoint or STS hub.

_No Dark AIS gap→critical-zone reacquisition events detected._

## Method notes

- STS pairing uses time-bucketed co-location (not full O(n²) continuous tracks).
- Dark AIS requires both sides of the gap inside the local SQLite retention window.
- Critical zones = Sentinel chokepoints + Fujairah / Laconian / Ceuta / GoG / Singapore STS hubs.
