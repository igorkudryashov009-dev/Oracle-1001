"""
Paper execution ledger + Mark-to-Market tracker for TTF hedge strategies.

Persists ttf_hedge_orders / ttf_hedge_mtm_marks in sentinel_ais.db and
exposes LIVE PAPER P&L for Sentinel TTF sheet (Panels H/I).
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.ttf_forecast.schema import resolve_db

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"
FORECAST_JSON = OUT / "ttf_ensemble_forecast.json"
LEDGER_JSON = OUT / "ttf_paper_ledger.json"

BASE_USD = 1000.0
HORIZON_DAYS = 30

STRATEGY_DEFS: tuple[dict[str, Any], ...] = (
    {
        "strategy_id": "STRAT_A_COLLAR",
        "short_id": "A",
        "projected_return_pct": 8.5,
        "beta": 0.40,
        "name": "Delta-Neutral Collar",
    },
    {
        "strategy_id": "STRAT_B_AIS_FUTURES",
        "short_id": "B",
        "projected_return_pct": 18.4,
        "beta": 1.00,
        "name": "AIS-Gated Dynamic Futures",
    },
    {
        "strategy_id": "STRAT_C_CONVEXITY",
        "short_id": "C",
        "projected_return_pct": 35.0,
        "beta": 1.55,
        "name": "Convexity Volatility Long",
    },
)

TTF_HEDGE_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS ttf_hedge_orders (
    order_id TEXT PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    spot_ref_price REAL NOT NULL,
    position_size_usd REAL NOT NULL,
    target_horizon_days INTEGER NOT NULL,
    projected_return_pct REAL NOT NULL,
    current_mtm_pnl_usd REAL DEFAULT 0.0,
    status TEXT NOT NULL
)
"""

TTF_HEDGE_MTM_MARKS_DDL = """
CREATE TABLE IF NOT EXISTS ttf_hedge_mtm_marks (
    order_id TEXT NOT NULL,
    mark_date TEXT NOT NULL,
    spot_price REAL NOT NULL,
    mtm_pnl_usd REAL NOT NULL,
    nav_usd REAL NOT NULL,
    PRIMARY KEY (order_id, mark_date),
    FOREIGN KEY (order_id) REFERENCES ttf_hedge_orders(order_id)
)
"""


def migrate_paper_ledger_schema(db_path: Optional[Path | str] = None) -> dict[str, Any]:
    path = resolve_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute(TTF_HEDGE_ORDERS_DDL)
        conn.execute(TTF_HEDGE_MTM_MARKS_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ttf_hedge_status ON ttf_hedge_orders(status, strategy_id)"
        )
        conn.commit()
        return {"ok": True, "db": str(path)}
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_spot_and_path() -> dict[str, float]:
    spot = 71.952
    p50_30 = spot
    if FORECAST_JSON.exists():
        try:
            fc = json.loads(FORECAST_JSON.read_text(encoding="utf-8"))
            spot = float(fc.get("spot_eur_mwh") or spot)
            p50_30 = float(((fc.get("horizons") or {}).get("30") or {}).get("p50") or spot)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"spot": spot, "p50_30": p50_30}


def _days_held(ts_utc: str) -> int:
    try:
        opened = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    except ValueError:
        return 0
    now = datetime.now(timezone.utc)
    return max(0, int((now - opened).total_seconds() // 86400))


def compute_mtm_pnl(
    *,
    entry_spot: float,
    live_spot: float,
    p50_30: float,
    size_usd: float,
    projected_return_pct: float,
    beta: float,
    days_held: int,
    horizon_days: int = HORIZON_DAYS,
) -> float:
    """
    Mark-to-market USD PnL:
      - spot leg: beta × spot drift × notional
      - path leg: progress toward projected return, scaled by ensemble P50 vs entry
    """
    entry = max(float(entry_spot), 1e-6)
    spot_ret = (float(live_spot) / entry) - 1.0
    path_ret = (float(p50_30) / entry) - 1.0
    progress = min(1.0, max(0.0, float(days_held) / float(max(horizon_days, 1))))
    # Blend live spot alpha with time-progress toward desk target
    target = float(projected_return_pct) / 100.0
    implied = 0.55 * beta * spot_ret + 0.25 * beta * path_ret + 0.20 * target * progress
    # Mild convexity for aggressive book
    if beta > 1.2 and spot_ret > 0:
        implied += 0.15 * beta * spot_ret**2
    return round(float(size_usd) * implied, 2)


def _get_open_order(conn: sqlite3.Connection, strategy_id: str) -> Optional[tuple]:
    return conn.execute(
        """
        SELECT order_id, timestamp_utc, strategy_id, spot_ref_price, position_size_usd,
               target_horizon_days, projected_return_pct, current_mtm_pnl_usd, status
        FROM ttf_hedge_orders
        WHERE strategy_id = ? AND status = 'OPEN'
        ORDER BY timestamp_utc DESC
        LIMIT 1
        """,
        (strategy_id,),
    ).fetchone()


def _insert_order(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    spot: float,
    size: float,
    projected: float,
) -> str:
    oid = f"PAPER-{strategy_id[-1] if strategy_id else 'X'}-{uuid.uuid4().hex[:10].upper()}"
    conn.execute(
        """
        INSERT INTO ttf_hedge_orders (
            order_id, timestamp_utc, strategy_id, spot_ref_price, position_size_usd,
            target_horizon_days, projected_return_pct, current_mtm_pnl_usd, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 'OPEN')
        """,
        (oid, _now_iso(), strategy_id, float(spot), float(size), HORIZON_DAYS, float(projected)),
    )
    return oid


def _upsert_mark(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    spot: float,
    mtm: float,
    nav: float,
) -> None:
    conn.execute(
        """
        INSERT INTO ttf_hedge_mtm_marks (order_id, mark_date, spot_price, mtm_pnl_usd, nav_usd)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(order_id, mark_date) DO UPDATE SET
            spot_price = excluded.spot_price,
            mtm_pnl_usd = excluded.mtm_pnl_usd,
            nav_usd = excluded.nav_usd
        """,
        (order_id, _today(), float(spot), float(mtm), float(nav)),
    )


def _expire_if_needed(conn: sqlite3.Connection, order_id: str, ts_utc: str, horizon: int) -> str:
    if _days_held(ts_utc) >= int(horizon):
        conn.execute(
            "UPDATE ttf_hedge_orders SET status='EXPIRED' WHERE order_id=?",
            (order_id,),
        )
        return "EXPIRED"
    return "OPEN"


def auto_log_daily_paper_trades(
    *,
    db_path: Optional[Path | str] = None,
    spot: Optional[float] = None,
    p50_30: Optional[float] = None,
    position_size_usd: float = BASE_USD,
    write_json: bool = True,
) -> dict[str, Any]:
    """
    Ensure OPEN $1,000 paper tickets for A/B/C and refresh live MtM marks.
    Called from release after ensemble/hedging build.
    """
    path = resolve_db(db_path)
    migrate_paper_ledger_schema(path)
    px = _load_spot_and_path()
    live_spot = float(spot if spot is not None else px["spot"])
    live_p50 = float(p50_30 if p50_30 is not None else px["p50_30"])

    conn = sqlite3.connect(str(path), timeout=60.0)
    orders_out: list[dict[str, Any]] = []
    try:
        for spec in STRATEGY_DEFS:
            sid = spec["strategy_id"]
            row = _get_open_order(conn, sid)
            if row is None:
                oid = _insert_order(
                    conn,
                    strategy_id=sid,
                    spot=live_spot,
                    size=position_size_usd,
                    projected=float(spec["projected_return_pct"]),
                )
                row = _get_open_order(conn, sid)
            assert row is not None
            (
                order_id,
                ts_utc,
                strategy_id,
                spot_ref,
                size,
                horizon,
                projected,
                _old_mtm,
                status,
            ) = row

            status = _expire_if_needed(conn, order_id, ts_utc, int(horizon))
            held = _days_held(ts_utc)
            mtm = compute_mtm_pnl(
                entry_spot=float(spot_ref),
                live_spot=live_spot,
                p50_30=live_p50,
                size_usd=float(size),
                projected_return_pct=float(projected),
                beta=float(spec["beta"]),
                days_held=held,
                horizon_days=int(horizon),
            )
            nav = round(float(size) + mtm, 2)
            if status == "OPEN":
                conn.execute(
                    "UPDATE ttf_hedge_orders SET current_mtm_pnl_usd=? WHERE order_id=?",
                    (mtm, order_id),
                )
                _upsert_mark(conn, order_id=order_id, spot=live_spot, mtm=mtm, nav=nav)

            # MtM history for equity overlay (up to horizon+1 points)
            marks = conn.execute(
                """
                SELECT mark_date, spot_price, mtm_pnl_usd, nav_usd
                FROM ttf_hedge_mtm_marks
                WHERE order_id=?
                ORDER BY mark_date ASC
                """,
                (order_id,),
            ).fetchall()

            orders_out.append({
                "order_id": order_id,
                "strategy_id": strategy_id,
                "short_id": spec["short_id"],
                "name": spec["name"],
                "timestamp_utc": ts_utc,
                "spot_ref_price": float(spot_ref),
                "live_spot": live_spot,
                "position_size_usd": float(size),
                "target_horizon_days": int(horizon),
                "projected_return_pct": float(projected),
                "current_mtm_pnl_usd": mtm,
                "current_nav_usd": nav,
                "mtm_return_pct": round(100.0 * mtm / float(size), 3) if size else 0.0,
                "days_held": held,
                "status": status,
                "badge": (
                    f"MtM: {'+' if mtm >= 0 else ''}${mtm:,.2f} / Target: +{float(projected):.1f}%"
                ),
                "marks": [
                    {
                        "date": m[0],
                        "spot": float(m[1]),
                        "mtm_pnl_usd": float(m[2]),
                        "nav_usd": float(m[3]),
                    }
                    for m in marks
                ],
            })
        conn.commit()
    finally:
        conn.close()

    # Strategy B showcase badge
    b = next((o for o in orders_out if o["short_id"] == "B"), None)
    report = {
        "generated_at_utc": _now_iso(),
        "live_spot_eur_mwh": live_spot,
        "p50_30": live_p50,
        "base_usd": position_size_usd,
        "orders": orders_out,
        "open_count": sum(1 for o in orders_out if o["status"] == "OPEN"),
        "strategy_b": b,
        "live_paper_badge": (b or {}).get("badge") or "MtM: — / Target: +18.4%",
        "equity_overlay": {
            # NAV series for Panel I overlay (A/B/C)
            sid: {
                "labels": [m["date"] for m in o["marks"]] or [_today()],
                "nav": [m["nav_usd"] for m in o["marks"]] or [position_size_usd + o["current_mtm_pnl_usd"]],
                "mtm": [m["mtm_pnl_usd"] for m in o["marks"]] or [o["current_mtm_pnl_usd"]],
            }
            for o in orders_out
            for sid in [o["short_id"]]
        },
    }
    if write_json:
        OUT.mkdir(parents=True, exist_ok=True)
        LEDGER_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def count_open_orders(db_path: Optional[Path | str] = None) -> int:
    path = resolve_db(db_path)
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15.0)
        n = int(
            conn.execute(
                "SELECT COUNT(*) FROM ttf_hedge_orders WHERE status='OPEN'"
            ).fetchone()[0]
        )
        conn.close()
        return n
    except sqlite3.Error:
        return 0


def main() -> int:
    report = auto_log_daily_paper_trades()
    print(json.dumps({
        "ok": True,
        "open_count": report["open_count"],
        "live_spot": report["live_spot_eur_mwh"],
        "badge": report["live_paper_badge"],
        "orders": [
            {
                "id": o["strategy_id"],
                "mtm": o["current_mtm_pnl_usd"],
                "nav": o["current_nav_usd"],
                "status": o["status"],
            }
            for o in report["orders"]
        ],
    }, indent=2))
    return 0 if report["open_count"] >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
