"""Maritime chokepoint geofences for Sentinel density analytics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Chokepoint:
    id: str
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


CHOKEPOINTS: tuple[Chokepoint, ...] = (
    Chokepoint("malacca", "Strait of Malacca", 1.0, 8.0, 98.0, 105.0),
    Chokepoint("suez", "Suez Canal", 29.0, 31.8, 32.0, 33.5),
    Chokepoint("bab_el_mandeb", "Bab-el-Mandeb", 11.5, 14.5, 42.0, 44.5),
    Chokepoint("bosphorus", "Bosphorus", 40.8, 41.4, 28.7, 29.4),
    Chokepoint("danish", "Danish Straits", 54.3, 56.8, 10.0, 13.5),
    Chokepoint("hormuz", "Strait of Hormuz", 25.0, 27.5, 55.5, 57.5),
    Chokepoint("gibraltar", "Strait of Gibraltar", 35.7, 36.3, -6.0, -5.0),
)


def detect_chokepoint(lat: float, lon: float) -> Optional[Chokepoint]:
    for cp in CHOKEPOINTS:
        if cp.contains(lat, lon):
            return cp
    return None


SANCTIONED_ZONES: tuple[Chokepoint, ...] = (
    Chokepoint("black_sea_risk", "Black Sea High-Risk", 41.0, 47.0, 27.0, 42.0),
    Chokepoint("persian_gulf", "Persian Gulf", 24.0, 30.5, 48.0, 57.0),
    Chokepoint("red_sea", "Red Sea Corridor", 12.0, 28.0, 32.0, 44.0),
)


def in_sanctioned_zone(lat: float, lon: float) -> Optional[str]:
    for z in SANCTIONED_ZONES:
        if z.contains(lat, lon):
            return z.name
    return None
