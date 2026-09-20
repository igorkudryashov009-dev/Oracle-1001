"""In-memory spatial index for compressor-station bboxes — Contract 1.8.0-ops-gis-sot.

Pure-Python grid index (no rtree/shapely required). Point-in-bbox lookup is
O(1) average via uniform lon/lat hashing; rebuild is O(N).

Ingests the root bbox catalog ``compressor_stations.ALL_COMPRESSOR_STATIONS``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import compressor_stations as _catalog

CONTRACT_VERSION = "1.8.0-ops-gis-sot"

BBox = Tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max


@dataclass(frozen=True)
class IndexedStation:
    """One catalog node with validated WGS84 bbox."""

    name: str
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float
    cluster: str = ""

    @property
    def bbox(self) -> BBox:
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)

    @property
    def centroid(self) -> Tuple[float, float]:
        return (
            (self.lat_min + self.lat_max) / 2.0,
            (self.lon_min + self.lon_max) / 2.0,
        )

    def contains(self, lon: float, lat: float) -> bool:
        return (
            self.lon_min <= lon <= self.lon_max
            and self.lat_min <= lat <= self.lat_max
        )


def _cluster_for(name: str) -> str:
    if name in _catalog.COMPRESSOR_STATIONS:
        return "core_ru"
    if name in _catalog.ADDITIONAL_COMPRESSOR_STATIONS:
        return "additional_ugs"
    if name in _catalog.TURKMENISTAN_COMPRESSOR_STATIONS:
        return "turkmenistan"
    if name in _catalog.CHINA_COMPRESSOR_STATIONS:
        return "china"
    return "unknown"


def _cell_key(lon: float, lat: float, cell_deg: float) -> Tuple[int, int]:
    return (math.floor(lon / cell_deg), math.floor(lat / cell_deg))


class CompressorSpatialIndex:
    """Uniform grid over station bboxes — point query O(1) avg, O(k) candidates."""

    def __init__(
        self,
        stations: Optional[Mapping[str, Sequence[float]]] = None,
        *,
        cell_deg: float = 1.0,
    ) -> None:
        if cell_deg <= 0:
            raise ValueError("cell_deg must be > 0")
        self.cell_deg = float(cell_deg)
        src = stations if stations is not None else _catalog.ALL_COMPRESSOR_STATIONS
        self._stations: Dict[str, IndexedStation] = {}
        self._grid: Dict[Tuple[int, int], List[str]] = {}
        self._build(src)

    def _build(self, src: Mapping[str, Sequence[float]]) -> None:
        self._stations.clear()
        self._grid.clear()
        for name, bbox in src.items():
            if len(bbox) != 4:
                raise ValueError(f"invalid bbox for {name}: {bbox}")
            lon_min, lat_min, lon_max, lat_max = (float(x) for x in bbox)
            st = IndexedStation(
                name=name,
                lon_min=lon_min,
                lat_min=lat_min,
                lon_max=lon_max,
                lat_max=lat_max,
                cluster=_cluster_for(name),
            )
            self._stations[name] = st
            i0, j0 = _cell_key(lon_min, lat_min, self.cell_deg)
            i1, j1 = _cell_key(lon_max, lat_max, self.cell_deg)
            for i in range(min(i0, i1), max(i0, i1) + 1):
                for j in range(min(j0, j1), max(j0, j1) + 1):
                    self._grid.setdefault((i, j), []).append(name)

    def __len__(self) -> int:
        return len(self._stations)

    def stations(self) -> Iterable[IndexedStation]:
        return self._stations.values()

    def get(self, name: str) -> Optional[IndexedStation]:
        return self._stations.get(name)

    def query_point(self, lon: float, lat: float) -> List[IndexedStation]:
        """Return stations whose bbox contains (lon, lat). O(1) avg."""
        lon_f = float(lon)
        lat_f = float(lat)
        key = _cell_key(lon_f, lat_f, self.cell_deg)
        names = self._grid.get(key, ())
        out: List[IndexedStation] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            st = self._stations[name]
            if st.contains(lon_f, lat_f):
                out.append(st)
        return out

    def query_bbox(
        self,
        lon_min: float,
        lat_min: float,
        lon_max: float,
        lat_max: float,
    ) -> List[IndexedStation]:
        """Stations intersecting the query rectangle. O(cells + k)."""
        i0, j0 = _cell_key(lon_min, lat_min, self.cell_deg)
        i1, j1 = _cell_key(lon_max, lat_max, self.cell_deg)
        seen: set[str] = set()
        out: List[IndexedStation] = []
        for i in range(min(i0, i1), max(i0, i1) + 1):
            for j in range(min(j0, j1), max(j0, j1) + 1):
                for name in self._grid.get((i, j), ()):
                    if name in seen:
                        continue
                    seen.add(name)
                    st = self._stations[name]
                    if (
                        st.lon_min <= lon_max
                        and st.lon_max >= lon_min
                        and st.lat_min <= lat_max
                        and st.lat_max >= lat_min
                    ):
                        out.append(st)
        return out

    def overlapping_pairs(self) -> List[Tuple[str, str]]:
        """Unordered pairs of distinct stations with intersecting bboxes."""
        names = sorted(self._stations)
        pairs: List[Tuple[str, str]] = []
        for a_i, a_name in enumerate(names):
            a = self._stations[a_name]
            for b_name in names[a_i + 1 :]:
                b = self._stations[b_name]
                if (
                    a.lon_min < b.lon_max
                    and a.lon_max > b.lon_min
                    and a.lat_min < b.lat_max
                    and a.lat_max > b.lat_min
                ):
                    pairs.append((a_name, b_name))
        return pairs


_DEFAULT_INDEX: Optional[CompressorSpatialIndex] = None


def get_spatial_index(*, rebuild: bool = False) -> CompressorSpatialIndex:
    """Process-wide singleton over root ``ALL_COMPRESSOR_STATIONS``."""
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None or rebuild:
        _DEFAULT_INDEX = CompressorSpatialIndex()
    return _DEFAULT_INDEX


def stations_containing_point(lon: float, lat: float) -> List[IndexedStation]:
    """Convenience: point-in-bbox against the default SoT index."""
    return get_spatial_index().query_point(lon, lat)


__all__ = (
    "CONTRACT_VERSION",
    "BBox",
    "IndexedStation",
    "CompressorSpatialIndex",
    "get_spatial_index",
    "stations_containing_point",
)
