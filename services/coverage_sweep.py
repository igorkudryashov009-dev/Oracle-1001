"""Archive coverage sweeps. Not activated until Node A has 3.0Gi.

GFW events are events, not a position track. source=gfw_events is not
terrestrial_ais. Open-ocean position density for the top 500 still needs
satellite AIS (Path B).

Budgets: Tier B <=109 requests/day, Tier A <=250, shared hard stop 450
(under the provider cap of 500). Unsent vessels carry to the next day.
VF credits are gap insurance only and are refunded when the API errors.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "data" / "archive" / "vf_spend_ledger.jsonl"
QUEUE_PATH = ROOT / "data" / "archive" / "sweep_carry.json"
BUDGET_PATH = ROOT / "data" / "archive" / "sweep_gfw_budget.json"

GFW_HARD_STOP = 450
TIER_A_DAILY_CAP = 250
TIER_B_DAILY_CAP = 109
TIER_A_N = 500
TIER_B_STALE = timedelta(days=6)
TIER_A_GAP_HOURS = 36.0
VF_GAP_A = 48.0
VF_GAP_B = 120.0
COVERAGE_ALERT_PCT = 95.0

Emit = Callable[..., Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_tiers(vessels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top 500 by DWT are Tier A. The rest are Tier B. DWT ties keep input order."""
    ordered = sorted(
        vessels,
        key=lambda v: (-float(v.get("dwt") or 0), str(v.get("imo") or "")),
    )
    out: list[dict[str, Any]] = []
    for i, row in enumerate(ordered):
        item = dict(row)
        item["tier"] = "A" if i < TIER_A_N else "B"
        out.append(item)
    return out


def _gap_hours(row: dict[str, Any], now: datetime) -> float:
    if row.get("gap_hours") is not None:
        return float(row["gap_hours"])
    last = _parse(row.get("last_record"))
    if last is None:
        return 24.0 * 365
    return max(0.0, (now - last).total_seconds() / 3600.0)


def _eligible(row: dict[str, Any], now: datetime) -> bool:
    gap = _gap_hours(row, now)
    if row.get("tier") == "A":
        return gap > TIER_A_GAP_HOURS
    last = _parse(row.get("last_record"))
    if last is None:
        return True
    return (now - last) > TIER_B_STALE


def plan_sweep(
    vessels: list[dict[str, Any]],
    *,
    tier: str,
    used_today: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Select a batch. The 451st request is not sent; the tail is carried."""
    now = now or _utc_now()
    cap = TIER_A_DAILY_CAP if tier == "A" else TIER_B_DAILY_CAP
    room = max(0, GFW_HARD_STOP - int(used_today))
    limit = min(cap, room)
    ranked = sorted(
        (r for r in vessels if r.get("tier") == tier and _eligible(r, now)),
        key=lambda r: _gap_hours(r, now),
        reverse=True,
    )
    chosen = ranked[:limit]
    carried = ranked[limit:]
    return {
        "tier": tier,
        "planned": len(chosen),
        "carried": len(carried),
        "used_today": int(used_today),
        "hard_stop": GFW_HARD_STOP,
        "stopped": room <= 0 or (len(ranked) > limit and int(used_today) + len(chosen) >= GFW_HARD_STOP),
        "requests": [
            {"imo": r.get("imo"), "mmsi": r.get("mmsi"), "gap_hours": round(_gap_hours(r, now), 2)}
            for r in chosen
        ],
        "carry": [
            {"imo": r.get("imo"), "mmsi": r.get("mmsi"), "gap_hours": round(_gap_hours(r, now), 2)}
            for r in carried
        ],
    }


def vf_should_spend(tier: str, gap_hours: float) -> bool:
    if tier == "A":
        return float(gap_hours) > VF_GAP_A
    if tier == "B":
        return float(gap_hours) > VF_GAP_B
    return False


def record_vf_attempt(
    *,
    mmsi: str,
    tier: str,
    gap_hours: float,
    api_ok: bool,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Spend one credit only past the gap threshold. An API error refunds it."""
    spend = vf_should_spend(tier, gap_hours)
    if not spend:
        result = "skipped_gap"
        credit = 0
    elif api_ok:
        result = "ok"
        credit = 1
    else:
        result = "error_refunded"
        credit = 0
    row = {
        "mmsi": str(mmsi),
        "tier": tier,
        "gap_hours": float(gap_hours),
        "credit_spent": credit,
        "result": result,
    }
    path = ledger_path or LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def coverage_from_rows(
    vessels: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _utc_now()
    tiered = classify_tiers(vessels)

    def _pct(group: list[dict[str, Any]], window: timedelta) -> float | None:
        if not group:
            return None
        fresh = 0
        for row in group:
            last = _parse(row.get("last_record"))
            if last is not None and (now - last) <= window:
                fresh += 1
        return round(100.0 * fresh / len(group), 2)

    a = [r for r in tiered if r["tier"] == "A"]
    b = [r for r in tiered if r["tier"] == "B"]
    gaps = sorted(_gap_hours(r, now) for r in a)
    if not gaps:
        p95 = None
    else:
        idx = min(len(gaps) - 1, max(0, int(round(0.95 * (len(gaps) - 1)))))
        p95 = round(gaps[idx], 2)
    return {
        "tierA_daily_coverage_pct": _pct(a, timedelta(hours=24)),
        "tierB_weekly_coverage_pct": _pct(b, timedelta(days=7)),
        "tierA_gap_p95_hours": p95,
    }


def maybe_coverage_alert(metrics: dict[str, Any], emit: Emit) -> str | None:
    """Dedup lives in the existing alert channel (24h)."""
    low = []
    for key in ("tierA_daily_coverage_pct", "tierB_weekly_coverage_pct"):
        val = metrics.get(key)
        if val is not None and float(val) < COVERAGE_ALERT_PCT:
            low.append(key)
    if not low:
        return None
    emit(
        "coverage_below_95",
        "archive coverage below 95%: " + ",".join(low),
        severity="WARN",
        detail={"metrics": {k: metrics.get(k) for k in low}},
    )
    return "coverage_below_95"


def admin_coverage_fields(db_path: Path | None = None) -> dict[str, Any]:
    """Fields for the full health document. The public slim shape ignores them."""
    fields: dict[str, Any] = {
        "tierA_daily_coverage_pct": None,
        "tierB_weekly_coverage_pct": None,
        "tierA_gap_p95_hours": None,
        "gfw_daily_requests_used": 0,
        "vf_credits_remaining_month": None,
    }
    if BUDGET_PATH.is_file():
        try:
            raw = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            fields["gfw_daily_requests_used"] = int(raw.get("used") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    try:
        from services.vesselfinder_budget import get_budget_status

        fields["vf_credits_remaining_month"] = int(get_budget_status().get("remaining") or 0)
    except (OSError, TypeError, ValueError):
        pass
    if db_path is None:
        import os

        env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
        if env:
            db_path = Path(env)
        else:
            from services.storage import DEFAULT_DB

            db_path = DEFAULT_DB
    if not Path(db_path).is_file():
        return fields
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT imo, dwt, mmsi, MAX(snapshot_date) AS last_record, MAX(gap_hours)
                FROM vessel_daily_archive
                GROUP BY imo
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return fields
    vessels = [
        {
            "imo": r[0],
            "dwt": r[1],
            "mmsi": r[2],
            "last_record": (str(r[3]) + "T00:00:00Z") if r[3] else None,
            "gap_hours": r[4],
        }
        for r in rows
    ]
    fields.update(coverage_from_rows(vessels))
    return fields


def load_vessels_from_archive(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT v.imo, v.dwt, v.mmsi, v.snapshot_date, v.gap_hours
            FROM vessel_daily_archive AS v
            JOIN (
                SELECT imo, MAX(snapshot_date) AS snapshot_date
                FROM vessel_daily_archive
                GROUP BY imo
            ) AS latest
              ON latest.imo = v.imo AND latest.snapshot_date = v.snapshot_date
            """
        ).fetchall()
    finally:
        conn.close()
    vessels = []
    for imo, dwt, mmsi, day, gap in rows:
        vessels.append(
            {
                "imo": imo,
                "dwt": dwt,
                "mmsi": mmsi,
                "last_record": (str(day) + "T00:00:00Z") if day else None,
                "gap_hours": gap,
            }
        )
    return vessels


def sweep_numbers(
    vessels: list[dict[str, Any]],
    *,
    used_today: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _utc_now()
    report = dry_run_report(vessels, used_today=used_today, now=now)
    tier_b = report["tier_b"]
    return {
        "tier_b_older_than_6d": tier_b["planned"] + tier_b["carried"],
        "tier_b_planned_requests": tier_b["planned"],
        "tier_b_carried": tier_b["carried"],
        "tier_a_planned_requests": report["tier_a"]["planned"],
        "gfw_daily_requests_used": int(used_today),
        "gfw_remaining_before_hard_stop": max(0, GFW_HARD_STOP - int(used_today)),
        "gfw_hard_stop": GFW_HARD_STOP,
    }


def main(argv: list[str] | None = None) -> int:
    """Default is a dry run. --execute is refused until SENTINEL_SWEEP_ARMED=1."""
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=("A", "B"), default="B")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--db", default="")
    args = parser.parse_args(argv)
    if args.execute and os.environ.get("SENTINEL_SWEEP_ARMED") != "1":
        print("refusing execute: sweep stays dry until RAM is 3.0Gi and SENTINEL_SWEEP_ARMED=1")
        return 2
    from services.storage import DEFAULT_DB

    db = Path(args.db or os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)
    vessels = load_vessels_from_archive(db) if db.is_file() else []
    used = 0
    if BUDGET_PATH.is_file():
        try:
            used = int(json.loads(BUDGET_PATH.read_text(encoding="utf-8")).get("used") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            used = 0
    numbers = sweep_numbers(vessels, used_today=used)
    for key, value in numbers.items():
        print(f"{key}={value}")
    print("mode=dry-run")
    return 0


def dry_run_report(
    vessels: list[dict[str, Any]],
    *,
    used_today: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    tiered = classify_tiers(vessels)
    now = now or _utc_now()
    b = plan_sweep(tiered, tier="B", used_today=used_today, now=now)
    a = plan_sweep(tiered, tier="A", used_today=used_today + b["planned"], now=now)
    return {"tier_b": b, "tier_a": a, "gfw_events_note": "events, not a position track"}


if __name__ == "__main__":
    raise SystemExit(main())
