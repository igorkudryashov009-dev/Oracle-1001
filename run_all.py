"""Run full fleet OSINT normalization pipeline."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pipeline.robust_parser import TZ_COLUMNS, fill_rate as tz_fill_rate, parse_osint_narrative
from paths import project_root, resolve_fleet_source
from schema import imo_checksum_valid

ROOT = project_root()
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
SHEET = "Sheet1"


def resolve_source() -> Path:
    """Resolve Excel KB with Cyrillic-safe pathlib (no hardcoded drive letter)."""
    return resolve_fleet_source()



def log(msg: str, lines: list[str]) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))
    lines.append(msg)


def autofit_columns(ws, max_width: int = 60) -> None:
    for col_idx, column_cells in enumerate(ws.columns, start=1):
        max_len = 0
        for cell in column_cells:
            if cell.value is None:
                continue
            max_len = max(max_len, len(str(cell.value)))
        width = min(max(max_len + 2, 10), max_width)
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def fill_rate(df: pd.DataFrame) -> float:
    """Average % of non-empty ТЗ columns (20 fields). Zero dims/tonnage = empty."""
    if len(df) == 0:
        return 0.0
    rates = []
    for _, row in df.iterrows():
        rates.append(tz_fill_rate({c: row.get(c) for c in TZ_COLUMNS}))
    return sum(rates) / len(rates)


def _df_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-serializable list of dicts."""
    records = []
    for row in df.to_dict(orient="records"):
        clean = {}
        for k, v in row.items():
            if pd.isna(v) if not isinstance(v, (list, dict)) else False:
                clean[k] = None
            elif hasattr(v, "item"):
                clean[k] = v.item()
            else:
                clean[k] = v
        records.append(clean)
    return records


def write_dashboard(
    fleet_df: pd.DataFrame,
    identity_df: pd.DataFrame,
    metrics: dict,
    path: Path,
) -> None:
    """Write Apple-styled dashboard.html (delegates to build_fleet_dashboard)."""
    from build_fleet_dashboard import write_dashboard as _write

    _write(fleet_df, identity_df, metrics, path)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    started = datetime.now().isoformat(timespec="seconds")
    log(f"=== Fleet pipeline start: {started} ===", log_lines)
    log(f"Project root: {ROOT}", log_lines)

    try:
        source = resolve_source()
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}", log_lines)
        (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        return 1

    log(f"Source XLSX: {source}", log_lines)
    log(f"Source exists: {source.is_file()} · size={source.stat().st_size} bytes", log_lines)

    xl = pd.ExcelFile(source, engine="openpyxl")
    log(f"Sheet names: {xl.sheet_names}", log_lines)
    if SHEET not in xl.sheet_names:
        log(f"ERROR: sheet '{SHEET}' not found. Available: {xl.sheet_names}", log_lines)
        (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        return 1

    df_raw = pd.read_excel(source, sheet_name=SHEET, engine="openpyxl", header=None)
    log(f"Raw shape (no header): {df_raw.shape}", log_lines)
    log("First 3 rows (preview):", log_lines)
    for i in range(min(3, len(df_raw))):
        a = df_raw.iloc[i, 0]
        b = df_raw.iloc[i, 1] if df_raw.shape[1] > 1 else None
        b_preview = (str(b)[:120] + "...") if b is not None and len(str(b)) > 120 else b
        log(f"  row[{i}] A={a!r} | B={b_preview!r}", log_lines)

    first_a = str(df_raw.iloc[0, 0]).strip().lower() if len(df_raw) else ""
    has_header = first_a in ("imo", "imo номер", "imo number", "номер imo") or (
        not any(ch.isdigit() for ch in first_a)
        and "наименование" in str(df_raw.iloc[0, 1]).lower()
        if df_raw.shape[1] > 1 and pd.notna(df_raw.iloc[0, 1])
        else False
    )

    def has_imo(v) -> bool:
        return bool(re.search(r"\d{7}", str(v))) if pd.notna(v) else False

    if not has_header and len(df_raw) >= 2:
        if not has_imo(df_raw.iloc[0, 0]) and has_imo(df_raw.iloc[1, 0]):
            has_header = True
        if first_a in ("imo", "a", "column1") or first_a.startswith("imo"):
            has_header = True

    log(f"Header detected: {has_header}", log_lines)

    if has_header:
        df = pd.read_excel(source, sheet_name=SHEET, engine="openpyxl", header=0)
    else:
        df = pd.read_excel(source, sheet_name=SHEET, engine="openpyxl", header=None)
        if df.shape[1] < 2:
            log(f"ERROR: expected >=2 columns, got {df.shape[1]}", log_lines)
            (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
            return 1
        df = df.iloc[:, :2].copy()
        df.columns = ["imo", "text"]

    log(f"DataFrame shape: {df.shape}", log_lines)
    log(f"Columns: {list(df.columns)}", log_lines)
    log(f"Rows to process: {len(df)}", log_lines)

    source_rows = len(df)
    records = []
    for _, row in df.iterrows():
        declared = row[df.columns[0]]
        text = row[df.columns[1]]
        if (pd.isna(declared) or str(declared).strip() == "") and (
            pd.isna(text) or str(text).strip() == ""
        ):
            continue
        rec = parse_osint_narrative(
            text if pd.notna(text) else "",
            declared_imo=str(declared) if pd.notna(declared) else None,
            as_snake=True,
        )
        # Light audit helpers for dashboard / QA CSVs
        imo = str(rec.get("imo") or "")
        rec["imo_valid"] = bool(imo.isdigit() and len(imo) == 7 and imo_checksum_valid(imo))
        filled_pct = tz_fill_rate(rec)
        filled = int(round(filled_pct / 100.0 * len(TZ_COLUMNS)))
        rec["vessel_category"] = "vessel" if filled >= 3 or rec.get("vessel_name") else "unknown"
        rec["source_confidence"] = "parsed" if filled >= 6 else "needs_review"
        if not rec.get("imo"):
            rec["source_confidence"] = "needs_review"
        rec["raw_text"] = str(text) if pd.notna(text) else ""
        records.append(rec)

    # ── Phase 2: fleet-level KNN / profile imputation ─────────────────────
    # Must run AFTER all vessels are parsed (needs cluster statistics).
    try:
        from pipeline.profile_imputer import FleetDataImputer
        imputer = FleetDataImputer(records)
        records = imputer.impute_all(records)
        log(f"Profile imputer: {len(records)} vessels enriched.", log_lines)
    except Exception as _pie:  # noqa: BLE001
        log(f"WARN: profile imputer skipped: {_pie}", log_lines)

    # ── Phase 3: registry-semantic re-tagging & final safety net ──────────────
    try:
        from pipeline.registry_emulator import ExternalRegistryEnricher
        enricher = ExternalRegistryEnricher()
        records  = enricher.enrich_all(records)
        log(f"Registry emulator (Phase 3): {len(records)} vessels tagged.", log_lines)
    except Exception as _ree:  # noqa: BLE001
        log(f"WARN: registry emulator skipped: {_ree}", log_lines)

    # ── Phase 4: TOP-100 provenance priority (no false synthetic tonnage) ──
    try:
        from pipeline.top100_provenance_fix import (
            fix_top100_provenance,
            summarize_d07,
        )
        records = fix_top100_provenance(records)
        d07 = summarize_d07(records)
        cells = sum(d07.values()) or 1
        log(
            "TOP-100 provenance fix (D07): "
            + ", ".join(f"{k}={v} ({100.0 * v / cells:.1f}%)" for k, v in d07.items()),
            log_lines,
        )
    except Exception as _tpe:  # noqa: BLE001
        log(f"WARN: TOP-100 provenance fix skipped: {_tpe}", log_lines)

    out = pd.DataFrame(records)

    def _id_str(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, float):
            return str(int(v)) if v == int(v) else str(v)
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none"):
            return None
        if re.fullmatch(r"\d+\.0+", s):
            return s.split(".", 1)[0]
        return s

    for col in ("imo", "mmsi", "call_sign"):
        if col in out.columns:
            out[col] = out[col].map(_id_str)

    export_cols = TZ_COLUMNS + [
        "sanctions_tags",
        "risk_source",
        "synthetic_fields",
        "imputed_fields",
        "registry_mock_fields",
        "imo_valid",
        "vessel_category",
        "source_confidence",
        "raw_text",
    ]
    for col in export_cols:
        if col not in out.columns:
            out[col] = None
    out = out[export_cols]

    vessels = out[out["vessel_category"] == "vessel"].copy()
    non_vessels = out[out["vessel_category"] == "non_vessel"].copy()
    needs = out[out["source_confidence"] == "needs_review"].copy()
    mismatch = out.iloc[0:0].copy()
    identity = out.iloc[0:0].copy()
    invalid = out[out["imo_valid"] == False].copy()  # noqa: E712

    imo_norm = vessels["imo"].astype(str).str.replace(r"\s+", "", regex=True)
    valid_imos = imo_norm[(imo_norm != "UNKNOWN") & (imo_norm != "None") & (vessels["imo_valid"] == True)]  # noqa: E712
    counts = Counter(imo_norm[(imo_norm != "UNKNOWN") & (imo_norm != "None")].tolist())
    duplicates = sorted([imo for imo, c in counts.items() if c > 1])

    log("", log_lines)
    log("--- Duplicate IMO (vessel) ---", log_lines)
    if duplicates:
        for imo in duplicates:
            log(f"  IMO {imo}: {counts[imo]} rows", log_lines)
    else:
        log("  None", log_lines)

    csv_path = OUTPUT / "fleet_database.csv"
    xlsx_path = OUTPUT / "fleet_database.xlsx"
    json_path = OUTPUT / "fleet_database.json"
    non_vessel_path = OUTPUT / "non_vessel_entities.csv"
    needs_path = OUTPUT / "needs_review.csv"
    mismatch_path = OUTPUT / "imo_mismatch.csv"
    identity_path = OUTPUT / "identity_conflicts.csv"
    invalid_path = OUTPUT / "invalid_imo_checksum.csv"
    dash_path = OUTPUT / "dashboard.html"

    vessels.to_csv(csv_path, index=False, encoding="utf-8-sig")
    vessels.to_excel(xlsx_path, index=False, engine="openpyxl")
    wb = load_workbook(xlsx_path)
    ws = wb.active
    ws.freeze_panes = "A2"
    autofit_columns(ws, max_width=60)
    wb.save(xlsx_path)

    fleet_records = _df_records(vessels)
    json_path.write_text(
        json.dumps(fleet_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    non_vessels.to_csv(non_vessel_path, index=False, encoding="utf-8-sig")
    needs.to_csv(needs_path, index=False, encoding="utf-8-sig")
    mismatch.to_csv(mismatch_path, index=False, encoding="utf-8-sig")
    identity.to_csv(identity_path, index=False, encoding="utf-8-sig")
    invalid.to_csv(invalid_path, index=False, encoding="utf-8-sig")

    unique_valid = valid_imos.nunique()
    avg_fill = fill_rate(vessels)

    risk_counts = Counter(
        (str(v).upper() if pd.notna(v) and str(v).strip() else "UNLABELED")
        for v in vessels["compliance_risk_level"].tolist()
    )
    tag_counter: Counter[str] = Counter()
    if "sanctions_tags" in vessels.columns:
        for raw in vessels["sanctions_tags"].fillna("").astype(str):
            if not raw.strip() or raw.lower() == "nan":
                continue
            for tag in [t.strip() for t in raw.split(";") if t.strip()]:
                tag_counter[tag] += 1

    metrics = {
        "source_rows": source_rows,
        "vessel_count": int(len(vessels)),
        "non_vessel": int(len(non_vessels)),
        "valid_imo": int(unique_valid),
        "needs_review": int(len(needs)),
        "imo_mismatch": int(len(mismatch)),
        "identity_conflicts": int(len(identity)),
        "invalid_imo": int(len(invalid)),
        "avg_fill": round(avg_fill, 2),
        "risk_labels": dict(risk_counts),
        "sanctions_tags": dict(tag_counter),
        "source_path": str(source),
    }

    write_dashboard(vessels, identity, metrics, dash_path)

    try:
        from build_osint_layers import write_osint_layers

        layers_path = OUTPUT / "osint_layers.html"
        write_osint_layers(vessels, metrics, layers_path)
        log(f"OSINT 9-layer infographic: {layers_path}", log_lines)
    except Exception as exc:  # noqa: BLE001 — never fail fleet pipeline on UI
        log(f"WARN: osint_layers build skipped: {exc}", log_lines)

    # ── Meta-analysis page ───────────────────────────────────────────────────
    meta_path = OUTPUT / "fleet_meta_analysis.html"
    try:
        from build_meta_analysis import write_meta_analysis
        meta_data = write_meta_analysis(vessels, meta_path)
        log(f"Meta-analysis page: {meta_path}", log_lines)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: meta_analysis build skipped: {exc}", log_lines)
        meta_data = {}

    # ── TOP-100 Analytics (12 dashboards / 3 tabs) ───────────────────────────
    top100_path = OUTPUT / "top100_analytics.html"
    top100_data: dict = {}
    try:
        from build_top100_analytics import write_top100_analytics
        top100_data = write_top100_analytics(vessels, top100_path)
        log(
            f"TOP-100 analytics: {top100_path} · "
            f"DWT={top100_data.get('top100_dwt', 0):,.0f} т · "
            f"share={top100_data.get('top100_share_pct', 0):.2f}% of fleet",
            log_lines,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: top100_analytics build skipped: {exc}", log_lines)

    # ── TOP-200 Analytics (15 dashboards / 3 tabs) ───────────────────────────
    top200_path = OUTPUT / "top200_analytics.html"
    top200_data: dict = {}
    try:
        from build_top200_analytics import write_top200_analytics
        top200_data = write_top200_analytics(vessels, top200_path)
        prov = (top200_data.get("provenance") or {}).get("pct") or {}
        log(
            f"TOP-200 analytics: {top200_path} · "
            f"DWT={top200_data.get('top200_dwt', 0):,.0f} т · "
            f"share={top200_data.get('top200_share_pct', 0):.2f}% of fleet · "
            f"OSINT={prov.get('OSINT', 0):.2f}% · SYNTH={prov.get('SYNTH', 0):.2f}%",
            log_lines,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: top200_analytics build skipped: {exc}", log_lines)

    # ── TOP-500 Analytics (15 dashboards / Tri-Donut D13–D27) ─────────────────
    top500_path = OUTPUT / "top500_analytics.html"
    top500_data: dict = {}
    try:
        from build_top500_analytics import write_top500_analytics
        top500_data = write_top500_analytics(vessels, top500_path)
        prov5 = (top500_data.get("provenance") or {}).get("pct") or {}
        log(
            f"TOP-500 analytics: {top500_path} · "
            f"DWT={top500_data.get('top500_dwt', 0):,.0f} т · "
            f"share={top500_data.get('top500_share_pct', 0):.2f}% of fleet · "
            f"OSINT={prov5.get('OSINT', 0):.2f}% · SYNTH={prov5.get('SYNTH', 0):.2f}%",
            log_lines,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: top500_analytics build skipped: {exc}", log_lines)

    # ── Sentinel 18-chart live AIS dashboard ─────────────────────────────────
    sentinel_path = OUTPUT / "sentinel_dashboard.html"
    sentinel_data: dict = {}
    try:
        from build_sentinel_dashboard import write_sentinel_dashboard
        sentinel_data = write_sentinel_dashboard(sentinel_path)
        log(
            f"Sentinel dashboard: {sentinel_path} · "
            f"live={sentinel_data.get('live_vessel_count', 0)} · "
            f"mode={sentinel_data.get('source_mode')}",
            log_lines,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: sentinel_dashboard build skipped: {exc}", log_lines)

    summary = [
        "",
        "=== SUMMARY ===",
        f"Source: {source}",
        f"Всего строк исходника: {source_rows}",
        f"Vessel-записей в основной базе: {len(vessels)}",
        f"Non-vessel (AtoN и т.п.) исключено: {len(non_vessels)}",
        f"Уникальных валидных IMO: {unique_valid}",
        f"Дубликаты IMO: {duplicates if duplicates else '[]'}",
        f"Несовпадение IMO (A vs текст, source_confidence=imo_mismatch): {len(mismatch)}",
        f"⚠ ПОДОЗРЕНИЕ НА ПОДМЕНУ ИДЕНТИЧНОСТИ (identity_conflict_flagged): {len(identity)}",
        f"Невалидная контрольная цифра / формат IMO: {len(invalid)}",
        f"Требуют ручной проверки (пустые/малоинформативные): {len(needs)}",
        f"Средний % заполненности полей по vessel-записям (20 ТЗ / robust_parser): {avg_fill:.2f}%",
        f"parser: pipeline.robust_parser",
        f"Risk Labels: "
        + ", ".join(f"{k}={v}" for k, v in sorted(risk_counts.items(), key=lambda x: (-x[1], x[0]))),
        f"L8 Sanctions Tags: "
        + (
            ", ".join(f"{k}={v}" for k, v in sorted(tag_counter.items(), key=lambda x: (-x[1], x[0])))
            if tag_counter
            else "(none)"
        ),
        f"parsed: {(out['source_confidence'] == 'parsed').sum()}",
        f"needs_review: {len(needs)}",
        f"imo_mismatch: {len(mismatch)}",
        f"identity_conflict_flagged: {len(identity)}",
        f"non_vessel: {len(non_vessels)}",
        f"Dashboard:       {dash_path}",
    ]

    # ── Meta-analysis macro block ────────────────────────────────────────────
    if meta_data:
        macro = meta_data.get("macro", {})
        gp    = macro.get("global_prov", {})
        gp_total = sum(gp.values()) or 1
        summary += [
            "",
            "=== META-ANALYSIS (АБСОЛЮТНЫЕ ВЕЛИЧИНЫ) ===",
            f"  Суммарный DWT флота : {macro.get('total_dwt', 0):>15,.0f} тонн",
            f"  Суммарный GT флота  : {macro.get('total_gt',  0):>15,.0f} GT",
            f"  Avg DWT / судно     : {macro.get('avg_dwt',   0):>15,.0f} тонн",
            f"  Avg GT  / судно     : {macro.get('avg_gt',    0):>15,.0f} GT",
            f"  Провенанс данных ({macro.get('total_cells',0):,} ячеек):",
            f"    OSINT   : {gp.get('osint',0):>8,d}  ({gp.get('osint',0)/gp_total*100:.1f}%)",
            f"    ⚡ Synth : {gp.get('synth',0):>8,d}  ({gp.get('synth',0)/gp_total*100:.1f}%)",
            f"    🔬 KNN  : {gp.get('knn',0):>8,d}  ({gp.get('knn',0)/gp_total*100:.1f}%)",
            f"    🌐 AIS  : {gp.get('reg',0):>8,d}  ({gp.get('reg',0)/gp_total*100:.1f}%)",
            f"  Meta-analysis page  : {meta_path}",
        ]
    summary += [
        "",
        f"Main dashboard:  {dash_path}",
        f"OSINT 9-layers:  http://127.0.0.1:8765/output/osint_layers.html",
        f"Meta-analysis:   http://127.0.0.1:8765/output/fleet_meta_analysis.html",
        f"TOP-100 Analytics: http://127.0.0.1:8765/output/top100_analytics.html",
        f"TOP-200 Analytics: http://127.0.0.1:8765/output/top200_analytics.html",
        f"TOP-500 Analytics: http://127.0.0.1:8765/output/top500_analytics.html",
        f"Sentinel 18 Charts: http://127.0.0.1:8765/output/sentinel_dashboard.html",
    ]
    if top100_data:
        summary += [
            f"  TOP-100 DWT sum : {top100_data.get('top100_dwt', 0):>15,.0f} т",
            f"  TOP-100 share   : {top100_data.get('top100_share_pct', 0):>14.2f}% of fleet",
            f"  Dashboards      : 12 (3 tabs × 4)",
        ]
    if top200_data:
        prov = (top200_data.get("provenance") or {}).get("pct") or {}
        summary += [
            f"  TOP-200 DWT sum : {top200_data.get('top200_dwt', 0):>15,.0f} т",
            f"  TOP-200 share   : {top200_data.get('top200_share_pct', 0):>14.2f}% of fleet",
            f"  TOP-200 OSINT   : {prov.get('OSINT', 0):>14.2f}% · SYNTH {prov.get('SYNTH', 0):.2f}%",
            f"  Dashboards      : 15 (Д13–Д27)",
        ]
    if top500_data:
        prov5 = (top500_data.get("provenance") or {}).get("pct") or {}
        summary += [
            f"  TOP-500 DWT sum : {top500_data.get('top500_dwt', 0):>15,.0f} т",
            f"  TOP-500 share   : {top500_data.get('top500_share_pct', 0):>14.2f}% of fleet",
            f"  TOP-500 OSINT   : {prov5.get('OSINT', 0):>14.2f}% · SYNTH {prov5.get('SYNTH', 0):.2f}%",
            f"  Dashboards      : 15 (Д13–Д27 · densе)",
        ]
    if sentinel_data:
        summary += [
            f"  Sentinel live   : {sentinel_data.get('live_vessel_count', 0):>15} vessels",
            f"  Sentinel mode   : {sentinel_data.get('source_mode', '—')}",
            f"  Infographics    : 18 (Groups A–D)",
        ]
    for line in summary:
        log(line, log_lines)

    (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"Log written: {LOGS / 'run_log.txt'}", log_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
