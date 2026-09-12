"""
Satellite AIS adapter stub — NOT activated.

Requires paid provider integration (e.g. Spire Maritime / ExactEarth / ORBCOMM) —
activate only after explicit provider selection and API credentials provided by
the user. Do NOT fabricate or guess API endpoints/credentials.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from services.ais_source_adapter import AISSourceAdapter


class SatelliteAISAdapter(AISSourceAdapter):
    """
    Placeholder socket for a future satellite AIS feed.

    Intentionally raises NotImplementedError on all live methods so nothing can
    silently "enable" satellite coverage without a real provider contract.
    """

    name = "satellite_ais_stub"

    def __init__(self, *, provider: str | None = None, credentials: dict[str, Any] | None = None):
        self.provider = provider
        self._credentials = credentials  # must be supplied by operator — never invent

    async def connect(self) -> None:
        raise NotImplementedError(
            "SatelliteAISAdapter requires paid provider integration "
            "(e.g. Spire Maritime / ExactEarth / ORBCOMM) — activate only after "
            "explicit provider selection and API credentials provided by the user. "
            "Do NOT fabricate or guess API endpoints/credentials."
        )

    async def subscribe(self, *, mmsi_filters: list[str], bounding_boxes: list) -> None:
        raise NotImplementedError(
            "SatelliteAISAdapter.subscribe is inactive until a real provider is wired."
        )

    async def on_message(self, raw: Any) -> Optional[dict[str, Any]]:
        raise NotImplementedError(
            "SatelliteAISAdapter.on_message is inactive until a real provider is wired."
        )

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError(
            "SatelliteAISAdapter.messages is inactive until a real provider is wired."
        )
        yield  # pragma: no cover

    def coverage_report(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "coverage_kind": "satellite",
            "status": "not_activated",
            "provider": self.provider,
            "credentials_present": bool(self._credentials),
            "note": (
                "Stub only — no network calls. Activate after user-selected provider "
                "and real credentials."
            ),
        }
