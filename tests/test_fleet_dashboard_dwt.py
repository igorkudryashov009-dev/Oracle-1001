"""DWT filter/sort integration for Fleet Dashboard (build_fleet_dashboard)."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd
import pytest

import build_fleet_dashboard as bfd

COLS = [
    "imo",
    "vessel_name",
    "flag",
    "dwt_tons",
    "vessel_category",
    "source_confidence",
]


def _sample_fleet(n: int = 25) -> pd.DataFrame:
    """20–30 vessels: mix of real DWT, null, and invalid."""
    rows = []
    for i in range(n):
        if i % 5 == 0:
            dwt = None  # absent in source
        elif i % 5 == 1:
            dwt = float("nan")
        elif i == 2:
            dwt = 73744.5
        else:
            dwt = float(10_000 * (i + 1))
        rows.append(
            {
                "imo": 9000000 + i,
                "imo_valid": True,
                "imo_format_error": None,
                "imo_from_text": None,
                "imo_source_match": True,
                "identity_spoofing_suspected_imo": None,
                "vessel_category": "vessel",
                "vessel_name": f"TEST VESSEL {i}",
                "mmsi": 200000000 + i,
                "call_sign": f"T{i:03d}",
                "vessel_type": "Tanker",
                "built_year": 2010,
                "age_years": 16,
                "flag": "Panama" if i % 2 == 0 else "Liberia",
                "dwt_tons": dwt,
                "gt": 50000,
                "loa_m": 250,
                "beam_m": 44,
                "draft_m": 12.0,
                "nav_status": "Under way",
                "speed_knots": 12.0,
                "asset_status": "active",
                "compliance_risk_level": "low",
                "destination_port": "Rotterdam",
                "source_confidence": "high",
                "raw_text": f"raw block {i}",
            }
        )
    return pd.DataFrame(rows)


def test_data_dwt_attr_honest():
    assert bfd.data_dwt_attr(290000) == "290000"
    assert bfd.data_dwt_attr(73744.5) == "73744.5"
    assert bfd.data_dwt_attr(None) == "0"
    assert bfd.data_dwt_attr(float("nan")) == "0"
    assert bfd.data_dwt_attr("") == "0"
    assert bfd.data_dwt_attr("nope") == "0"
    assert bfd.dwt_is_missing(None) is True
    assert bfd.dwt_is_missing(float("nan")) is True
    assert bfd.dwt_is_missing(50000) is False
    assert bfd.dwt_is_missing(0.0) is False  # real zero is rare but not "missing"


def test_render_fleet_tr_data_dwt():
    with_dwt = {"imo": 1, "vessel_name": "A", "dwt_tons": 120000, "flag": "X"}
    missing = {"imo": 2, "vessel_name": "B", "dwt_tons": None, "flag": "Y"}
    tr1 = bfd.render_fleet_tr(with_dwt, ["imo", "vessel_name", "dwt_tons", "flag"])
    tr2 = bfd.render_fleet_tr(missing, ["imo", "vessel_name", "dwt_tons", "flag"])
    assert 'data-dwt="120000"' in tr1
    assert "data-dwt-missing" not in tr1
    assert 'data-dwt="0"' in tr2
    assert 'data-dwt-missing="1"' in tr2


def test_dashboard_sample_has_dwt_attrs_and_panel(tmp_path: Path):
    fleet = _sample_fleet(25)
    identity = fleet.iloc[0:0].copy()
    metrics = {
        "source_rows": 25,
        "vessel_count": 25,
        "non_vessel": 0,
        "valid_imo": 25,
        "needs_review": 0,
        "imo_mismatch": 0,
        "identity_conflicts": 0,
        "invalid_imo": 0,
        "avg_fill": 50.0,
    }
    out = tmp_path / "dashboard.html"
    bfd.write_dashboard(fleet, identity, metrics, path=out)

    html = out.read_text(encoding="utf-8")
    assert (tmp_path / "dwt_filter_sort.js").exists()
    assert 'src="dwt_filter_sort.js"' in html
    assert 'id="dwt-from"' in html
    assert 'id="dwt-to"' in html
    assert "DWT от" in html
    assert "DWT до" in html
    assert 'id="dwt-sort-toggle"' in html
    assert "DwtFilterSort.stampRow" in html or "data-dwt" in html
    assert "DwtFilterSort.init" in html

    # Static <tr data-dwt> for every sample vessel (acceptance: attributes + values)
    records = bfd._df_records(fleet)
    rows_html = "\n".join(bfd.render_fleet_tr(r, COLS) for r in records)
    table_html = f"<table id='fleet-check'><tbody>\n{rows_html}\n</tbody></table>"
    check_path = tmp_path / "fleet_dwt_rows.html"
    check_path.write_text(table_html, encoding="utf-8")

    trs = re.findall(r"<tr\s+([^>]+)>", rows_html)
    assert len(trs) == 25
    for i, record in enumerate(records):
        attrs = trs[i]
        expected = bfd.data_dwt_attr(record.get("dwt_tons"))
        assert f'data-dwt="{expected}"' in attrs
        if bfd.dwt_is_missing(record.get("dwt_tons")):
            assert 'data-dwt-missing="1"' in attrs
            assert expected == "0"
        else:
            assert "data-dwt-missing" not in attrs
            dwt = record["dwt_tons"]
            assert math.isfinite(float(dwt))
            assert expected == bfd.data_dwt_attr(dwt)

    # Embedded JSON keeps source nulls (no invented DWT in payload)
    m = re.search(r"const FLEET_DATA = (\[.*?\]);", html, re.S)
    assert m, "FLEET_DATA missing in dashboard"
    import json

    payload = json.loads(m.group(1))
    assert len(payload) == 25
    for src, embedded in zip(records, payload):
        if bfd.dwt_is_missing(src.get("dwt_tons")):
            assert embedded.get("dwt_tons") is None or (
                isinstance(embedded.get("dwt_tons"), float)
                and math.isnan(embedded["dwt_tons"])
            )
        else:
            assert float(embedded["dwt_tons"]) == pytest.approx(float(src["dwt_tons"]))
