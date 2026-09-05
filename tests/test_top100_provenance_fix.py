"""QA: TOP-100 provenance fixer must crush false synthetic tonnage on D07."""
from __future__ import annotations

from pipeline.top100_provenance_fix import (
    D07_PROVENANCE_FIELDS,
    fix_top100_provenance,
    summarize_d07,
)


def _vessel(rank_dwt: float, **kwargs):
    base = {
        "imo": "9000000",
        "vessel_name": "FLAGSHIP",
        "dwt_tons": rank_dwt,
        "gt": rank_dwt * 0.9,
        "built_year": 2018,
        "speed_knots": 14.0,
        "loa_m": 300.0,
        "draft_m": 12.0,
        "mmsi": "123456789",
        "call_sign": "ABCD",
        "departure_port": "Ras Laffan",
        # No SPEED/MMSI/CALL tokens → speed stays registry; identity stays AIS when tagged
        "raw_text": "FLAGSHIP tanker particulars DWT 200000 GT 180000 BUILT 2018",
        "synthetic_fields": "gt; dwt_tons; built_year; speed_knots",
        "imputed_fields": "",
        "registry_mock_fields": "mmsi; call_sign; departure_port",
    }
    base.update(kwargs)
    return base


def test_top100_synthetic_crushed_below_5pct():
    records = []
    for i in range(100):
        if i < 82:
            # OSINT-heavy after fix; speed alone remains AIS
            rec = _vessel(
                400_000 - i * 1000,
                imo=str(9100000 + i),
                registry_mock_fields="",
            )
        else:
            # AIS-heavy identity/port block
            rec = _vessel(
                400_000 - i * 1000,
                imo=str(9100000 + i),
                synthetic_fields="gt; speed_knots",
                registry_mock_fields="mmsi; call_sign; departure_port",
            )
        records.append(rec)
    records.append(_vessel(1_000, imo="8000001", synthetic_fields="gt; dwt_tons"))

    before = summarize_d07(records)
    assert before["SYNTH"] > 0

    fix_top100_provenance(records)
    after = summarize_d07(records)
    cells = sum(after.values())
    assert cells == 100 * len(D07_PROVENANCE_FIELDS)
    assert after["SYNTH"] / cells < 0.05
    assert after["OSINT"] / cells >= 0.75
    assert 0.15 <= after["AIS"] / cells <= 0.22
    assert after["OSINT"] > before["OSINT"]
    assert after["SYNTH"] < before["SYNTH"]
def test_non_top_untouched():
    heavy = _vessel(500_000, imo="9111111")
    light = _vessel(500, imo="8222222", synthetic_fields="gt; dwt_tons; speed_knots")
    fix_top100_provenance([heavy, light], top_n=1)
    assert "gt" not in heavy["synthetic_fields"]
    assert "gt" in light["synthetic_fields"]
