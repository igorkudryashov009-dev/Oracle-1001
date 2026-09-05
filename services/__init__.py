"""Oracle-1001 / Sentinel — live AIS ingestion & analytics services."""

from __future__ import annotations

__all__ = [
    "AISStreamConnector",
    "FleetRegistry",
    "assign_strategic_tier",
]


def __getattr__(name: str):
    if name == "AISStreamConnector":
        from services.aisstream_connector import AISStreamConnector
        return AISStreamConnector
    if name in ("FleetRegistry", "assign_strategic_tier"):
        from services.fleet_registry import FleetRegistry, assign_strategic_tier
        if name == "FleetRegistry":
            return FleetRegistry
        return assign_strategic_tier
    raise AttributeError(f"module 'services' has no attribute {name!r}")
