"""Unit tests for laden/ballast classification and DWT-flow features.

Synthetic mini-fleet (5 vessels) with artificial drafts — no network, no real archive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import feature_engineering as fe


@pytest.fixture
def synth_root(tmp_path: Path) -> Path:
    """5 vessels, ≥7 days, controlled laden→ballast and ballast→laden flips."""
    by_vessel = tmp_path / "by_vessel"
    by_vessel.mkdir()
    fleet_path = tmp_path / "fleet_database.csv"
    targets_path = tmp_path / "targets.json"
    out_dir = tmp_path / "features"
    out_dir.mkdir()

    # Vessel specs: (imo, dwt, cargo vessel_type, max_draft used in synth)
    # Empirical ceiling for 300k DWT crude ≈ 17+100k/40k = 19.5 → we use archive max
    vessels = [
        # VLCC: swings laden→ballast (discharge)
        {
            "imo": "9000001",
            "dwt": 300_000.0,
            "vtype": "Crude Oil Tanker (VLCC)",
            "days": [
                # 7 days laden (~0.95 of archive max 21.0)
                *[(f"2026-07-{d:02d}", 20.0, 1.5, 103.8) for d in range(1, 8)],
                # then ballast
                *[(f"2026-07-{d:02d}", 10.0, 1.6, 103.9) for d in range(8, 12)],
            ],
        },
        # Aframax: stays ballast
        {
            "imo": "9000002",
            "dwt": 110_000.0,
            "vtype": "Crude Oil Tanker (Aframax)",
            "days": [(f"2026-07-{d:02d}", 7.0, 25.1, 55.3) for d in range(1, 10)],
        },
        # LNG: ballast→laden (load)
        {
            "imo": "9000003",
            "dwt": 80_000.0,
            "vtype": "LNG Tanker (газовоз)",
            "days": [
                *[(f"2026-07-{d:02d}", 6.0, 25.9, 51.6) for d in range(1, 6)],
                *[(f"2026-07-{d:02d}", 11.5, 25.91, 51.58) for d in range(6, 12)],
            ],
        },
        # Products: transitional band (0.6–0.85)
        {
            "imo": "9000004",
            "dwt": 50_000.0,
            "vtype": "Oil Products Tanker",
            "days": [(f"2026-07-{d:02d}", 9.5, 51.9, 4.1) for d in range(1, 10)],
        },
        # No draft → uncertain
        {
            "imo": "9000005",
            "dwt": 40_000.0,
            "vtype": "Oil/Chemical Tanker",
            "days": [(f"2026-07-{d:02d}", None, 29.7, -95.3) for d in range(1, 10)],
        },
    ]

    fleet_rows = []
    targets = []
    for v in vessels:
        rows = []
        for date, draft, lat, lon in v["days"]:
            rows.append(
                {
                    "imo": v["imo"],
                    "vessel_name": f"TEST-{v['imo']}",
                    "mmsi": f"2{v['imo'][-8:]}",
                    "lat": lat,
                    "lon": lon,
                    "timestamp": f"{date}T00:05:00Z",
                    "speed": 10.0,
                    "heading": 90.0,
                    "nav_status": "Under way using engine",
                    "km_last_24h": 100.0,
                    "num_pings_24h": 50,
                    "data_source": "aisstream_live",
                    "snapshot_date": date,
                    "draft_m": draft,
                }
            )
        pd.DataFrame(rows).to_csv(by_vessel / f"{v['imo']}.csv", index=False, encoding="utf-8-sig")
        fleet_rows.append(
            {
                "imo": v["imo"],
                "vessel_category": "vessel",
                "vessel_name": f"TEST-{v['imo']}",
                "vessel_type": v["vtype"],
                "dwt_tons": v["dwt"],
                "draft_m": None,
            }
        )
        targets.append({"imo": v["imo"], "mmsi": f"2{v['imo'][-8:]}", "vessel_name": f"TEST-{v['imo']}"})

    pd.DataFrame(fleet_rows).to_csv(fleet_path, index=False)
    targets_path.write_text(
        json.dumps(
            {
                "stats": {
                    "unique_mmsi_for_subscription": 5,
                    "unique_imo_with_mmsi": 5,
                },
                "targets": targets,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return tmp_path


def test_classify_thresholds():
    assert fe.classify_laden_ballast(0.90) == fe.STATE_LADEN
    assert fe.classify_laden_ballast(0.50) == fe.STATE_BALLAST
    assert fe.classify_laden_ballast(0.70) == fe.STATE_UNCERTAIN
    assert fe.classify_laden_ballast(None) == fe.STATE_UNCERTAIN
    assert fe.classify_laden_ballast(0.99, force_uncertain=True) == fe.STATE_UNCERTAIN


def test_cargo_class_mapping():
    assert fe.classify_cargo_class("Crude Oil Tanker (VLCC)") == "crude_oil"
    assert fe.classify_cargo_class("LNG Tanker (газовоз)") == "lng_lpg"
    assert fe.classify_cargo_class("Oil Products Tanker") == "products_chemical"
    assert fe.classify_cargo_class(None) == "other"


def test_empirical_draft_monotonic_in_dwt():
    d1 = fe.empirical_max_draft_m(40_000, "crude_oil")
    d2 = fe.empirical_max_draft_m(160_000, "crude_oil")
    d3 = fe.empirical_max_draft_m(300_000, "crude_oil")
    assert d1 is not None and d2 is not None and d3 is not None
    assert d1 < d2 < d3


def test_archive_max_preferred_when_enough_variation():
    s = pd.Series([20.0, 19.5, 10.0, 10.5, 20.0, 11.0, 19.0, 10.0])
    ceil = fe.resolve_draft_ceiling(s, 300_000, "crude_oil")
    assert ceil.source == "archive_max"
    assert ceil.confidence == "high"
    assert ceil.max_draft_m == pytest.approx(20.0)


def test_dwt_empirical_when_few_obs():
    s = pd.Series([11.0, 11.2])  # < 7 obs
    ceil = fe.resolve_draft_ceiling(s, 300_000, "crude_oil")
    assert ceil.source == "dwt_empirical"
    assert ceil.confidence == "low_confidence"
    assert ceil.max_draft_m is not None


def test_end_to_end_synthetic(synth_root: Path):
    summary = fe.run_feature_engineering(
        by_vessel_dir=synth_root / "by_vessel",
        fleet_path=synth_root / "fleet_database.csv",
        targets_path=synth_root / "targets.json",
        out_timeseries=synth_root / "features" / "dwt_flow_timeseries.parquet",
        out_transitions=synth_root / "features" / "vessel_state_transitions.csv",
    )

    assert summary["sufficiency"]["sufficient"] is True
    assert summary["n_transitions"] >= 2

    ts = pd.read_parquet(summary["out_timeseries"])
    tr = pd.read_csv(summary["out_transitions"])

    # Coverage metadata must exist on every timeseries row
    required_meta = {
        "n_vessels_observed",
        "n_vessels_tracked",
        "coverage_pct_vessels",
        "fleet_dwt_empirical_max",
        "laden_dwt_vs_fleet_max_pct",
        "insufficient_data_flag",
    }
    assert required_meta.issubset(set(ts.columns))
    assert ts["insufficient_data_flag"].eq(False).all()
    assert (ts["n_vessels_tracked"] == 5).all()

    # VLCC discharge proxy
    vlcc_events = tr[tr["imo"].astype(str) == "9000001"]
    assert not vlcc_events.empty
    assert (vlcc_events["event_proxy"] == "discharge_proxy").any()
    assert (vlcc_events["from_state"] == "laden").any()
    assert (vlcc_events["to_state"] == "ballast").any()

    # LNG load proxy
    lng_events = tr[tr["imo"].astype(str) == "9000003"]
    assert (lng_events["event_proxy"] == "load_proxy").any()

    # DWT-flow: crude laden should be positive on early dates
    crude = ts[ts["cargo_class"] == "crude_oil"].sort_values("date")
    assert not crude.empty
    assert crude.iloc[0]["laden_dwt_sum"] >= 300_000  # VLCC laden day 1


def test_insufficient_data_warning(tmp_path: Path, capsys):
    """<7 calendar days → explicit warning, insufficient flag on outputs."""
    by_vessel = tmp_path / "by_vessel"
    by_vessel.mkdir()
    rows = [
        {
            "imo": "9000099",
            "lat": 1.0,
            "lon": 103.0,
            "timestamp": f"2026-07-0{d}T00:00:00Z",
            "speed": 5.0,
            "snapshot_date": f"2026-07-0{d}",
            "draft_m": 12.0,
            "data_source": "aisstream_live",
        }
        for d in range(1, 4)  # only 3 days
    ]
    pd.DataFrame(rows).to_csv(by_vessel / "9000099.csv", index=False, encoding="utf-8-sig")
    fleet = pd.DataFrame(
        [
            {
                "imo": "9000099",
                "vessel_category": "vessel",
                "vessel_name": "SHORT",
                "vessel_type": "Crude Oil Tanker",
                "dwt_tons": 100_000,
                "draft_m": 12.0,
            }
        ]
    )
    fleet_path = tmp_path / "fleet.csv"
    fleet.to_csv(fleet_path, index=False)
    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps(
            {
                "stats": {"unique_mmsi_for_subscription": 1, "unique_imo_with_mmsi": 1},
                "targets": [{"imo": "9000099", "mmsi": "200000001", "vessel_name": "SHORT"}],
            }
        ),
        encoding="utf-8",
    )

    summary = fe.run_feature_engineering(
        by_vessel_dir=by_vessel,
        fleet_path=fleet_path,
        targets_path=targets,
        out_timeseries=tmp_path / "features" / "dwt_flow_timeseries.parquet",
        out_transitions=tmp_path / "features" / "vessel_state_transitions.csv",
    )
    captured = capsys.readouterr()
    assert fe.INSUFFICIENT_DATA_MSG in captured.out
    assert summary["sufficiency"]["sufficient"] is False

    ts = pd.read_parquet(summary["out_timeseries"])
    assert ts["insufficient_data_flag"].all()
    assert ts["insufficient_data_warning"].iloc[0] == fe.INSUFFICIENT_DATA_MSG
