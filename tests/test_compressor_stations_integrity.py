"""Integrity tests for Unified Compressor Stations Register — Contract 1.8.0-ops-gis-sot."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import pytest

import compressor_stations as catalog
from services.spatial_index import (
    CompressorSpatialIndex,
    get_spatial_index,
    stations_containing_point,
)


EXPECTED_TOTAL = 185
CLUSTER_SIZES = {
    "COMPRESSOR_STATIONS": 50,
    "ADDITIONAL_COMPRESSOR_STATIONS": 50,
    "TURKMENISTAN_COMPRESSOR_STATIONS": 50,
    "CHINA_COMPRESSOR_STATIONS": 35,
}


def test_exact_total_count_185() -> None:
    ok, count, errs = catalog.validate_stations_registry()
    assert ok, errs
    assert count == EXPECTED_TOTAL
    assert len(catalog.ALL_COMPRESSOR_STATIONS) == EXPECTED_TOTAL


def test_wgs84_range_validation() -> None:
    for name, bbox in catalog.ALL_COMPRESSOR_STATIONS.items():
        lon_min, lat_min, lon_max, lat_max = bbox
        assert -180.0 <= lon_min <= 180.0, name
        assert -180.0 <= lon_max <= 180.0, name
        assert -90.0 <= lat_min <= 90.0, name
        assert -90.0 <= lat_max <= 90.0, name


def test_spatial_integrity_min_lt_max() -> None:
    for name, bbox in catalog.ALL_COMPRESSOR_STATIONS.items():
        lon_min, lat_min, lon_max, lat_max = bbox
        assert lon_min < lon_max, f"lon span invalid: {name}"
        assert lat_min < lat_max, f"lat span invalid: {name}"


def test_import_regression_four_subdictionaries() -> None:
    assert len(catalog.COMPRESSOR_STATIONS) == CLUSTER_SIZES["COMPRESSOR_STATIONS"]
    assert (
        len(catalog.ADDITIONAL_COMPRESSOR_STATIONS)
        == CLUSTER_SIZES["ADDITIONAL_COMPRESSOR_STATIONS"]
    )
    assert (
        len(catalog.TURKMENISTAN_COMPRESSOR_STATIONS)
        == CLUSTER_SIZES["TURKMENISTAN_COMPRESSOR_STATIONS"]
    )
    assert len(catalog.CHINA_COMPRESSOR_STATIONS) == CLUSTER_SIZES["CHINA_COMPRESSOR_STATIONS"]
    # Regional isolation preserved + merged SoT
    assert set(catalog.COMPRESSOR_STATIONS).issubset(catalog.ALL_COMPRESSOR_STATIONS)
    assert set(catalog.ADDITIONAL_COMPRESSOR_STATIONS).issubset(
        catalog.ALL_COMPRESSOR_STATIONS
    )
    assert set(catalog.TURKMENISTAN_COMPRESSOR_STATIONS).issubset(
        catalog.ALL_COMPRESSOR_STATIONS
    )
    assert set(catalog.CHINA_COMPRESSOR_STATIONS).issubset(catalog.ALL_COMPRESSOR_STATIONS)
    # No cross-cluster key collisions (sum == ALL)
    assert (
        sum(CLUSTER_SIZES.values()) == len(catalog.ALL_COMPRESSOR_STATIONS) == EXPECTED_TOTAL
    )


def _bbox_overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def test_overlap_checking_flags_pairs() -> None:
    """Detect bbox overlaps; fail hard only on identical duplicate geofences."""
    items = list(catalog.ALL_COMPRESSOR_STATIONS.items())
    overlaps: List[Tuple[str, str]] = []
    identical: List[Tuple[str, str]] = []
    for i, (na, ba) in enumerate(items):
        for nb, bb in items[i + 1 :]:
            if list(ba) == list(bb):
                identical.append((na, nb))
            elif _bbox_overlap(ba, bb):
                overlaps.append((na, nb))

    # Hard fail: two distinct keys must never share an identical bbox.
    assert identical == [], f"identical duplicate bboxes: {identical}"

    # Soft flag: record intentional corridor proximity overlaps (informational).
    # Dense corridors may intentionally abut; cap prevents accidental mass merge bugs.
    assert len(overlaps) < 40, (
        f"unexpectedly high overlap count={len(overlaps)}; sample={overlaps[:8]}"
    )


def test_spatial_index_point_in_bbox_portovaya() -> None:
    idx = get_spatial_index(rebuild=True)
    assert len(idx) == EXPECTED_TOTAL
    # Centroid of Portovaya NordStream1
    hits = idx.query_point(28.05, 60.565)
    names = {h.name for h in hits}
    assert "KS_10_Portovaya_NordStream1" in names
    # Ocean midpoint far from any station
    assert stations_containing_point(0.0, 0.0) == []


def test_spatial_index_matches_catalog_validate() -> None:
    idx = CompressorSpatialIndex(catalog.ALL_COMPRESSOR_STATIONS)
    assert len(idx) == 185
    # Every station centroid must resolve to itself
    for name, bbox in catalog.ALL_COMPRESSOR_STATIONS.items():
        lon_min, lat_min, lon_max, lat_max = bbox
        lon_c = (lon_min + lon_max) / 2.0
        lat_c = (lat_min + lat_max) / 2.0
        hit_names = {h.name for h in idx.query_point(lon_c, lat_c)}
        assert name in hit_names, name


def test_services_api_sot_aligned_with_root_catalog() -> None:
    from services import compressor_stations as api

    assert api.CONTRACT_VERSION == "1.8.0-ops-gis-sot"
    assert len(api.ALL_COMPRESSOR_STATIONS) == EXPECTED_TOTAL
    assert api.COMPRESSOR_STATIONS is catalog.COMPRESSOR_STATIONS
    assert set(api.COMPRESSOR_STATIONS_BY_LEGACY) == set(catalog.ALL_COMPRESSOR_STATIONS)
    near = api.find_nearest_stations(60.565, 28.05, 50.0)
    assert near and near[0]["legacy_id"] == "KS_10_Portovaya_NordStream1"
    at = api.stations_at_point(60.565, 28.05)
    assert any(r.get("legacy_id") == "KS_10_Portovaya_NordStream1" for r in at)


def test_validate_stations_registry_api_export() -> None:
    ok, n, errs = catalog.validate_stations_registry()
    assert ok and n == 185 and errs == []
