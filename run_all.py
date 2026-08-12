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

from parser import parse_block
from schema import FIELD_ORDER

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
SOURCE = Path(r"C:\Users\MSI\OneDrive\Desktop\7000\Книга 1 (1).xlsx")
SHEET = "7000"


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
    """Average % of non-null fields excluding raw_text and audit helpers."""
    skip = {
        "raw_text",
        "imo_from_text",
        "identity_spoofing_suspected_imo",
        "identity_spoofing_note",
        "imo_format_error",
    }
    cols = [c for c in df.columns if c not in skip]
    if not cols or len(df) == 0:
        return 0.0
    rates = [df[col].notna().mean() * 100.0 for col in cols]
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

    if not SOURCE.exists():
        log(f"ERROR: source file not found: {SOURCE}", log_lines)
        (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        return 1

    xl = pd.ExcelFile(SOURCE, engine="openpyxl")
    log(f"Sheet names: {xl.sheet_names}", log_lines)
    if SHEET not in xl.sheet_names:
        log(f"ERROR: sheet '{SHEET}' not found. Available: {xl.sheet_names}", log_lines)
        (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        return 1

    df_raw = pd.read_excel(SOURCE, sheet_name=SHEET, engine="openpyxl", header=None)
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
        df = pd.read_excel(SOURCE, sheet_name=SHEET, engine="openpyxl", header=0)
    else:
        df = pd.read_excel(SOURCE, sheet_name=SHEET, engine="openpyxl", header=None)
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
        rec = parse_block(text if pd.notna(text) else "", declared if pd.notna(declared) else "")
        records.append(rec.model_dump())

    out = pd.DataFrame(records)
    for col in FIELD_ORDER:
        if col not in out.columns:
            out[col] = None
    out = out[FIELD_ORDER]

    vessels = out[out["vessel_category"] == "vessel"].copy()
    non_vessels = out[out["vessel_category"] == "non_vessel"].copy()
    needs = out[out["source_confidence"] == "needs_review"].copy()
    mismatch = out[out["source_confidence"] == "imo_mismatch"].copy()
    identity = out[out["source_confidence"] == "identity_conflict_flagged"].copy()
    invalid = out[out["imo_valid"] == False].copy()  # noqa: E712

    imo_norm = vessels["imo"].astype(str).str.replace(r"\s+", "", regex=True)
    valid_imos = imo_norm[(imo_norm != "UNKNOWN") & (vessels["imo_valid"] == True)]  # noqa: E712
    counts = Counter(imo_norm[imo_norm != "UNKNOWN"].tolist())
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
    }

    write_dashboard(vessels, identity, metrics, dash_path)

    summary = [
        "",
        "=== SUMMARY ===",
        f"Всего строк исходника: {source_rows}",
        f"Vessel-записей в основной базе: {len(vessels)}",
        f"Non-vessel (AtoN и т.п.) исключено: {len(non_vessels)}",
        f"Уникальных валидных IMO: {unique_valid}",
        f"Дубликаты IMO: {duplicates if duplicates else '[]'}",
        f"Несовпадение IMO (A vs текст, source_confidence=imo_mismatch): {len(mismatch)}",
        f"⚠ ПОДОЗРЕНИЕ НА ПОДМЕНУ ИДЕНТИЧНОСТИ (identity_conflict_flagged): {len(identity)}",
        f"Невалидная контрольная цифра / формат IMO: {len(invalid)}",
        f"Требуют ручной проверки (пустые/малоинформативные): {len(needs)}",
        f"Средний % заполненности полей по vessel-записям: {avg_fill:.2f}%",
        f"parsed: {(out['source_confidence'] == 'parsed').sum()}",
        f"needs_review: {len(needs)}",
        f"imo_mismatch: {len(mismatch)}",
        f"identity_conflict_flagged: {len(identity)}",
        f"non_vessel: {len(non_vessels)}",
        f"Dashboard: {dash_path}",
    ]
    for line in summary:
        log(line, log_lines)

    (LOGS / "run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"Log written: {LOGS / 'run_log.txt'}", log_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
