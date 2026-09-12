"""
Шаг 1: диагностика источника HTTP 429 у AISStream.

Открывает несколько WS-соединений подряд и логирует, на каком этапе
приходит отказ: handshake/upgrade vs post-connect subscribe.

Usage:
  python scripts/probe_aisstream_429.py [--connections 4] [--subscribe-mmsi 50]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

WS_URL = "wss://stream.aisstream.io/v0/stream"
WORLD_BBOX = [[[-90.0, -180.0], [90.0, 180.0]]]
LOG_PATH = ROOT / "logs" / "aisstream_429_probe.jsonl"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _log(event: dict) -> None:
    event = {"ts": _ts(), **event}
    line = json.dumps(event, ensure_ascii=False)
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _http_meta(exc: BaseException) -> dict:
    meta: dict = {
        "exc_type": type(exc).__name__,
        "exc": str(exc),
        "http_status": None,
        "retry_after": None,
        "response_headers": None,
    }
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        meta["http_status"] = status
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                hdr = {str(k).lower(): str(v) for k, v in headers.items()}
            except Exception:  # noqa: BLE001
                hdr = {"repr": repr(headers)}
            meta["response_headers"] = hdr
            meta["retry_after"] = hdr.get("retry-after")
    status_attr = getattr(exc, "status_code", None)
    if meta["http_status"] is None and status_attr is not None:
        meta["http_status"] = status_attr
    return meta


async def _one_connection(
    *,
    conn_id: int,
    api_key: str,
    mmsi_batch: list[str],
    hold_seconds: float,
) -> dict:
    import websockets

    result: dict = {
        "batch_id": conn_id,
        "mmsi_count": len(mmsi_batch),
        "handshake_ok": False,
        "subscribe_sent": False,
        "subscribe_ack": False,
        "stage_failed": None,
        "http_status": None,
        "retry_after": None,
    }
    _log(
        {
            "event": "connect_attempt",
            "batch_id": conn_id,
            "mmsi_count": len(mmsi_batch),
            "stage": "handshake",
        }
    )
    try:
        ws = await websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
            max_size=8 * 1024 * 1024,
            compression="deflate",
        )
    except Exception as exc:  # noqa: BLE001
        meta = _http_meta(exc)
        result.update(
            {
                "stage_failed": "handshake",
                "http_status": meta["http_status"],
                "retry_after": meta["retry_after"],
                "error": meta,
            }
        )
        _log(
            {
                "event": "connect_result",
                "batch_id": conn_id,
                "mmsi_count": len(mmsi_batch),
                "stage": "handshake",
                "ok": False,
                "http_status": meta["http_status"],
                "retry_after": meta["retry_after"],
                "error": meta["exc"],
                "exc_type": meta["exc_type"],
                "response_headers": meta["response_headers"],
            }
        )
        return result

    result["handshake_ok"] = True
    _log(
        {
            "event": "connect_result",
            "batch_id": conn_id,
            "mmsi_count": len(mmsi_batch),
            "stage": "handshake",
            "ok": True,
            "http_status": 101,
            "retry_after": None,
        }
    )

    sub = {
        "APIKey": api_key,
        "BoundingBoxes": WORLD_BBOX,
        "FiltersShipMMSI": mmsi_batch,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }
    try:
        _log(
            {
                "event": "subscribe_attempt",
                "batch_id": conn_id,
                "mmsi_count": len(mmsi_batch),
                "stage": "subscribe",
            }
        )
        await ws.send(json.dumps(sub))
        result["subscribe_sent"] = True
        # Wait briefly for SubscriptionConfirmation or error frame
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            msg = json.loads(raw)
            mtype = msg.get("MessageType") or msg.get("messageType")
            if mtype == "SubscriptionConfirmation":
                result["subscribe_ack"] = True
                _log(
                    {
                        "event": "subscribe_result",
                        "batch_id": conn_id,
                        "mmsi_count": len(mmsi_batch),
                        "stage": "subscribe",
                        "ok": True,
                        "http_status": None,
                        "ack": msg,
                    }
                )
                break
            if msg.get("error") or msg.get("Error"):
                result["stage_failed"] = "subscribe"
                _log(
                    {
                        "event": "subscribe_result",
                        "batch_id": conn_id,
                        "mmsi_count": len(mmsi_batch),
                        "stage": "subscribe",
                        "ok": False,
                        "server_error": msg,
                    }
                )
                break
        else:
            # No explicit ack — treat as soft-ok if socket still open
            if result["subscribe_sent"] and not result["stage_failed"]:
                _log(
                    {
                        "event": "subscribe_result",
                        "batch_id": conn_id,
                        "mmsi_count": len(mmsi_batch),
                        "stage": "subscribe",
                        "ok": True,
                        "note": "no_SubscriptionConfirmation_within_5s_but_socket_open",
                    }
                )

        if hold_seconds > 0 and not result.get("stage_failed"):
            await asyncio.sleep(hold_seconds)
    except Exception as exc:  # noqa: BLE001
        meta = _http_meta(exc)
        result["stage_failed"] = "subscribe" if result["handshake_ok"] else "handshake"
        result["http_status"] = meta["http_status"]
        result["retry_after"] = meta["retry_after"]
        result["error"] = meta
        _log(
            {
                "event": "subscribe_result",
                "batch_id": conn_id,
                "mmsi_count": len(mmsi_batch),
                "stage": result["stage_failed"],
                "ok": False,
                "http_status": meta["http_status"],
                "retry_after": meta["retry_after"],
                "error": meta["exc"],
                "exc_type": meta["exc_type"],
                "response_headers": meta["response_headers"],
            }
        )
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    return result


async def main_async(args: argparse.Namespace) -> int:
    api_key = (os.getenv("AISSTREAM_API_KEY") or "").strip()
    if not api_key or api_key.startswith("YOUR_"):
        print("ERROR: AISSTREAM_API_KEY missing in .env", file=sys.stderr)
        return 2

    # Dummy 9-digit MMSIs (or from fleet if available)
    mmsis: list[str] = []
    fleet = ROOT / "output" / "fleet_database.csv"
    if fleet.exists():
        try:
            from services.analytics import select_top500_fleet

            top = select_top500_fleet(fleet, top_n=500)
            mmsis = [str(x) for x in top["mmsi"].astype(str).tolist() if str(x).strip()]
        except Exception as exc:  # noqa: BLE001
            _log({"event": "fleet_fallback", "error": str(exc)})
    if not mmsis:
        mmsis = [f"2{i:08d}" for i in range(args.connections * args.subscribe_mmsi)]

    n = max(1, int(args.connections))
    chunk = max(1, int(args.subscribe_mmsi))
    _log(
        {
            "event": "probe_start",
            "connections": n,
            "subscribe_mmsi": chunk,
            "hold_seconds": args.hold_seconds,
            "note": "Intentional multi-connect to locate 429 stage",
        }
    )

    results = []
    # Open connections nearly back-to-back (small gap) to hit connection limit
    tasks = []
    for i in range(n):
        batch = mmsis[i * chunk : (i + 1) * chunk]
        if not batch:
            batch = mmsis[:chunk]
        tasks.append(
            asyncio.create_task(
                _one_connection(
                    conn_id=i,
                    api_key=api_key,
                    mmsi_batch=batch,
                    hold_seconds=float(args.hold_seconds),
                )
            )
        )
        await asyncio.sleep(float(args.gap_seconds))

    results = await asyncio.gather(*tasks)
    summary = {
        "event": "probe_summary",
        "handshake_ok": sum(1 for r in results if r.get("handshake_ok")),
        "subscribe_ack": sum(1 for r in results if r.get("subscribe_ack")),
        "failed_handshake": [
            r for r in results if r.get("stage_failed") == "handshake"
        ],
        "failed_subscribe": [
            r for r in results if r.get("stage_failed") == "subscribe"
        ],
        "http_429_handshake": any(
            r.get("stage_failed") == "handshake" and r.get("http_status") == 429
            for r in results
        ),
        "http_429_subscribe": any(
            r.get("stage_failed") == "subscribe" and r.get("http_status") == 429
            for r in results
        ),
        "results": results,
    }
    _log(summary)

    # Verdict line for the architect
    if summary["http_429_handshake"]:
        verdict = "VERDICT: 429 at WS handshake/upgrade (connection-count limit)"
    elif summary["http_429_subscribe"]:
        verdict = "VERDICT: 429 after connect on subscribe path (message/MMSI volume limit)"
    elif summary["failed_handshake"]:
        statuses = [r.get("http_status") for r in summary["failed_handshake"]]
        verdict = f"VERDICT: handshake failures (status={statuses}) — not necessarily 429"
    elif summary["failed_subscribe"]:
        verdict = "VERDICT: subscribe-stage failures without HTTP 429"
    else:
        verdict = "VERDICT: no failures observed in this probe window"
    print(verdict, flush=True)
    _log({"event": "verdict", "text": verdict})
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Probe AISStream 429 stage")
    p.add_argument("--connections", type=int, default=4, help="Parallel WS to open")
    p.add_argument("--subscribe-mmsi", type=int, default=50, help="MMSI per subscribe")
    p.add_argument("--hold-seconds", type=float, default=8.0, help="Keep sockets open")
    p.add_argument("--gap-seconds", type=float, default=0.3, help="Stagger between opens")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
