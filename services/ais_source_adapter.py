"""
AIS source adapter interface (architectural socket for multi-provider ingest).

Current production path: AISStream WebSocket terrestrial adapter
(services/aisstream_connector.py). Satellite providers are NOT activated —
see services/satellite_ais_adapter.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional


class AISSourceAdapter(ABC):
    """Protocol for AIS ingest backends (terrestrial WS, future satellite, etc.)."""

    name: str = "abstract"

    @abstractmethod
    async def connect(self) -> None:
        """Open transport to the provider."""

    @abstractmethod
    async def subscribe(self, *, mmsi_filters: list[str], bounding_boxes: list) -> None:
        """Apply vessel / geo filters for this session."""

    @abstractmethod
    async def on_message(self, raw: Any) -> Optional[dict[str, Any]]:
        """Normalize one provider frame into a dict (or None to skip)."""

    @abstractmethod
    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized AIS message dicts (connect+subscribe loop helper)."""

    @abstractmethod
    def coverage_report(self) -> dict[str, Any]:
        """Adapter-level coverage / health snapshot for dual-gate consumers."""

    async def close(self) -> None:
        """Optional clean shutdown."""
        return None


class AISStreamTerrestrialAdapter(AISSourceAdapter):
    """
    Marker / façade documenting that the live ingest lives in aisstream_connector.

    The production daemon remains ``AISStreamConnector`` (single persistent WS +
    MMSI rotation). This class exists so SatelliteAISAdapter can share the same
    interface without rewriting matching / storage / analytics.
    """

    name = "aisstream_terrestrial"

    def __init__(self, connector: Any | None = None):
        self._connector = connector

    async def connect(self) -> None:
        raise NotImplementedError(
            "Use services.aisstream_connector.AISStreamConnector.run() for live ingest; "
            "this façade documents the adapter boundary only."
        )

    async def subscribe(self, *, mmsi_filters: list[str], bounding_boxes: list) -> None:
        raise NotImplementedError("Delegated to AISStreamConnector single-WS subscribe path.")

    async def on_message(self, raw: Any) -> Optional[dict[str, Any]]:
        raise NotImplementedError("Delegated to AISStreamConnector.parse_message.")

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError("Delegated to AISStreamConnector._consume_socket.")
        yield  # pragma: no cover — make this an async generator for type checkers

    def coverage_report(self) -> dict[str, Any]:
        if self._connector is None:
            return {"adapter": self.name, "status": "unbound"}
        m = getattr(self._connector, "metrics", None)
        return {
            "adapter": self.name,
            "coverage_kind": "terrestrial",
            "raw_subscribed_hits": getattr(m, "raw_subscribed_hits", None),
            "unique_subscribed_mmsis": getattr(m, "unique_subscribed_mmsis", None),
            "coverage_window_seconds": getattr(m, "coverage_window_seconds", None),
            "strategy": getattr(m, "subscription_strategy", None),
        }
