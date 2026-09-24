"""VesselFinder Optimal Allocator — 500 credits / month across ~1,260 gas carriers.

Contract 1.8.0-ops-gis-sot:
  - monthly_budget hard-cap 500 (never exceed) via services.vesselfinder_budget
  - Does NOT open a second AIS WebSocket (G3 lock)
  - Does NOT affect Dual Gate fleet_sample_status (archive reporting only)

Split 60/40:
  - 300 credits → TOP-500 by DWT (rolling ≥1 hit / 50 days → ~10/day)
  - 200 credits → rest (~760) (rolling ≥1 hit / 114 days → ~6/day)

Priority inside each tier:
  P1 — terrestrial gap (top: >48h, rest: >96h)
  P2 — STS / sanctions / ghost-detector hotlist
  P3 — round-robin remainder

Daily quota ≈ 16 (10 top + 6 rest); weekly cap 112 with unused rollover.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("sentinel.vf_budget_allocator")

STATE_PATH = ROOT / "data" / "archive" / "vf_allocator_state.json"
HOTLIST_PATH = ROOT / "data" / "archive" / "vf_hotlist.json"
FLEET_CSV = ROOT / "output" / "fleet_database.csv"

MONTHLY_BUDGET = 500
TOP_TIER_BUDGET = 300
REST_TIER_BUDGET = 200
TOP_N = 500
TARGET_FLEET_N = 1260

DAILY_TOP_QUOTA = 10
DAILY_REST_QUOTA = 6
DAILY_TOTAL_QUOTA = DAILY_TOP_QUOTA + DAILY_REST_QUOTA  # 16
WEEKLY_CAP = 112

TOP_GAP_HOURS = 48.0
REST_GAP_HOURS = 96.0
TOP_ROLLING_DAYS = 50
REST_ROLLING_DAYS = 114

ALLOWED_SOURCES = frozenset({"terrestrial_ais", "vf_api", "none"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_key(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m")


def _week_key(dt: Optional[datetime] = None) -> str:
    d = (dt or _utc_now()).astimezone(timezone.utc).date()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class VesselSlot:
    imo: str
    mmsi: str = ""
    name: str = ""
    dwt: float = 0.0
    tier: str = "rest"  # "top" | "rest"
    rank: int = 0
    gap_hours: float = 1e9
    in_hotlist: bool = False
    last_vf_at: Optional[str] = None
    vf_hits_month: int = 0


@dataclass
class AllocatorPlan:
    day: str
    top_imos: list[str] = field(default_factory=list)
    rest_imos: list[str] = field(default_factory=list)
    daily_quota: int = DAILY_TOTAL_QUOTA
    weekly_remaining: int = WEEKLY_CAP
    month_remaining: int = MONTHLY_BUDGET
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def all_imos(self) -> list[str]:
        return list(self.top_imos) + list(self.rest_imos)


def load_fleet_universe(
    csv_path: Path | None = None,
    *,
    target_n: int = TARGET_FLEET_N,
    top_n: int = TOP_N,
) -> tuple[list[VesselSlot], list[VesselSlot]]:
    """Return (top_500, rest) sorted by DWT descending. Caps at target_n unique IMOs."""
    path = csv_path or FLEET_CSV
    import pandas as pd

    if not path.is_file():
        raise FileNotFoundError(f"fleet registry missing: {path}")
    df = pd.read_csv(path, low_memory=False)
    if "vessel_category" in df.columns:
        df = df[df["vessel_category"].astype(str).str.lower() == "vessel"].copy()
    df["dwt_tons"] = pd.to_numeric(df.get("dwt_tons"), errors="coerce").fillna(0.0)
    df["imo"] = df["imo"].apply(lambda x: str(int(float(x))) if str(x).strip() not in ("", "nan") else "")
    df = df[df["imo"] != ""].drop_duplicates(subset=["imo"], keep="first")
    df = df.sort_values("dwt_tons", ascending=False).head(int(target_n)).reset_index(drop=True)

    top: list[VesselSlot] = []
    rest: list[VesselSlot] = []
    for i, row in df.iterrows():
        slot = VesselSlot(
            imo=str(row["imo"]),
            mmsi=str(row.get("mmsi") or "").strip(),
            name=str(row.get("vessel_name") or f"IMO {row['imo']}"),
            dwt=float(row.get("dwt_tons") or 0.0),
            rank=int(i) + 1,
            tier="top" if int(i) < top_n else "rest",
        )
        tags = str(row.get("sanctions_tags") or "").upper()
        risk = str(row.get("compliance_risk_level") or "").upper()
        if any(t in tags for t in ("STS", "OFAC", "SHADOW", "DARK", "SDN")) or risk in (
            "HIGH",
            "EXTREME",
        ):
            slot.in_hotlist = True
        if slot.tier == "top":
            top.append(slot)
        else:
            rest.append(slot)
    return top, rest


def load_hotlist_imos(path: Path | None = None) -> set[str]:
    """Merge file hotlist + ghost_detector spoof IMOs from health snapshot if present."""
    out: set[str] = set()
    p = path or HOTLIST_PATH
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                out.update(str(x).strip() for x in raw if str(x).strip())
            elif isinstance(raw, dict):
                for key in ("imos", "hotlist", "spoofed_imos"):
                    vals = raw.get(key) or []
                    if isinstance(vals, list):
                        out.update(str(x).strip() for x in vals if str(x).strip())
        except (OSError, json.JSONDecodeError):
            pass
    snap = ROOT / "output" / "api" / "v1" / "health.json"
    if snap.is_file():
        try:
            doc = json.loads(snap.read_text(encoding="utf-8"))
            spoof = doc.get("ais_spoofing") if isinstance(doc, dict) else None
            if isinstance(spoof, dict):
                for imo in spoof.get("spoofed_imos") or []:
                    if imo:
                        out.add(str(imo).strip())
        except (OSError, json.JSONDecodeError):
            pass
    return out


def load_terrestrial_gaps(
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, float]:
    """IMO → hours since last ais_positions hit. Missing IMO → large gap (1e9)."""
    import sqlite3

    now = now or _utc_now()
    from services.storage import DEFAULT_DB

    db = Path(db_path or os.environ.get("SENTINEL_DB_PATH") or DEFAULT_DB)
    if not db.is_file():
        return {}
    gaps: dict[str, float] = {}
    try:
        conn = sqlite3.connect(str(db), timeout=15.0)
        try:
            cur = conn.execute(
                """
                SELECT imo, MAX(COALESCE(received_at, timestamp_utc)) AS last_ts
                FROM ais_positions
                WHERE imo IS NOT NULL AND TRIM(imo) != ''
                GROUP BY imo
                """
            )
            for imo, last_ts in cur.fetchall():
                key = str(imo).strip()
                if not key or not last_ts:
                    gaps[key] = 1e9
                    continue
                try:
                    raw = str(last_ts).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    gaps[key] = max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
                except (TypeError, ValueError):
                    gaps[key] = 1e9
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("terrestrial gap probe failed: %s", exc)
    return gaps


def load_state(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    now = now or _utc_now()
    if not p.is_file():
        return _empty_state(_month_key(now), week=_week_key(now))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state(_month_key(now), week=_week_key(now))
    if not isinstance(data, dict):
        return _empty_state(_month_key(now), week=_week_key(now))
    month = _month_key(now)
    if str(data.get("month") or "") != month:
        rolled = _empty_state(month, week=_week_key(now))
        rolled["previous_month"] = data.get("month")
        rolled["previous_tier_used"] = data.get("tier_used")
        # Preserve coverage memory across month boundary
        rolled["last_vf_at"] = dict(data.get("last_vf_at") or {})
        rolled["vf_hits"] = dict(data.get("vf_hits") or {})
        rolled["rr_cursor"] = dict(data.get("rr_cursor") or {"top": 0, "rest": 0})
        return rolled
    week = _week_key(now)
    if str(data.get("week") or "") != week:
        data["week"] = week
        data["week_used"] = 0
    data.setdefault("tier_used", {"top": 0, "rest": 0})
    data.setdefault("day_used", {})
    data.setdefault("last_vf_at", {})
    data.setdefault("vf_hits", {})
    data.setdefault("rr_cursor", {"top": 0, "rest": 0})
    return data


def _empty_state(month: Optional[str] = None, *, week: Optional[str] = None) -> dict[str, Any]:
    m = month or _month_key()
    return {
        "month": m,
        "week": week or _week_key(),
        "tier_budget": {"top": TOP_TIER_BUDGET, "rest": REST_TIER_BUDGET},
        "tier_used": {"top": 0, "rest": 0},
        "week_used": 0,
        "day_used": {},
        "last_vf_at": {},
        "vf_hits": {},
        "rr_cursor": {"top": 0, "rest": 0},
        "updated_at": _utc_iso(),
    }


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc_iso()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _priority_sort_key(slot: VesselSlot, *, gap_threshold: float) -> tuple:
    """Lower tuple = higher priority: P1 gap, P2 hotlist, P3 round-robin by oldest VF."""
    p1 = 0 if slot.gap_hours >= gap_threshold else 1
    p2 = 0 if slot.in_hotlist else 1
    last = slot.last_vf_at or "1970-01-01T00:00:00Z"
    return (p1, p2, last, -slot.dwt, slot.rank)


def _select_tier(
    slots: list[VesselSlot],
    *,
    quota: int,
    gap_threshold: float,
    cursor: int,
    state: dict[str, Any],
    rolling_days: int,
    now: datetime,
) -> tuple[list[VesselSlot], int, dict[str, str]]:
    if quota <= 0 or not slots:
        return [], cursor, {}
    # Annotate from state
    last_map = state.get("last_vf_at") or {}
    hits_map = state.get("vf_hits") or {}
    for s in slots:
        s.last_vf_at = last_map.get(s.imo)
        s.vf_hits_month = int(hits_map.get(s.imo) or 0)

    # Guarantee coverage: vessels never hit in rolling window first
    cutoff = (now - timedelta(days=rolling_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    overdue = [s for s in slots if not s.last_vf_at or s.last_vf_at < cutoff]
    fresh = [s for s in slots if s not in overdue]

    overdue.sort(key=lambda s: _priority_sort_key(s, gap_threshold=gap_threshold))
    fresh.sort(key=lambda s: _priority_sort_key(s, gap_threshold=gap_threshold))

    # Round-robin rotate fresh pool from cursor for fairness among P3
    if fresh:
        c = cursor % len(fresh)
        fresh = fresh[c:] + fresh[:c]

    ordered = overdue + fresh
    picked = ordered[:quota]
    reasons: dict[str, str] = {}
    for s in picked:
        if s.gap_hours >= gap_threshold:
            reasons[s.imo] = "P1_gap"
        elif s.in_hotlist:
            reasons[s.imo] = "P2_hotlist"
        elif not s.last_vf_at or s.last_vf_at < cutoff:
            reasons[s.imo] = "P3_overdue_rr"
        else:
            reasons[s.imo] = "P3_rr"
    new_cursor = (cursor + len(picked)) % max(1, len(slots))
    return picked, new_cursor, reasons


def plan_daily_allocation(
    *,
    day: str | None = None,
    state_path: Path | None = None,
    fleet_csv: Path | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
    monthly_remaining: int | None = None,
    simulate: bool = False,
    top_slots: list[VesselSlot] | None = None,
    rest_slots: list[VesselSlot] | None = None,
) -> AllocatorPlan:
    """Build today's IMO list without spending credits."""
    now = now or _utc_now()
    day_s = day or now.strftime("%Y-%m-%d")
    state = load_state(state_path, now=now)

    from services.vesselfinder_budget import get_budget_status

    budget = get_budget_status() if not simulate else {
        "remaining": MONTHLY_BUDGET - int((state.get("tier_used") or {}).get("top", 0))
        - int((state.get("tier_used") or {}).get("rest", 0)),
        "used": int((state.get("tier_used") or {}).get("top", 0))
        + int((state.get("tier_used") or {}).get("rest", 0)),
        "monthly_budget": MONTHLY_BUDGET,
    }
    month_rem = int(monthly_remaining if monthly_remaining is not None else budget.get("remaining") or 0)
    week_used = int(state.get("week_used") or 0)
    week_rem = max(0, WEEKLY_CAP - week_used)
    day_already = int((state.get("day_used") or {}).get(day_s) or 0)

    daily_left = max(0, DAILY_TOTAL_QUOTA - day_already)
    hard_cap = min(daily_left, week_rem, month_rem)
    if hard_cap <= 0:
        return AllocatorPlan(
            day=day_s,
            daily_quota=0,
            weekly_remaining=week_rem,
            month_remaining=month_rem,
        )

    if top_slots is None or rest_slots is None:
        top_slots, rest_slots = load_fleet_universe(fleet_csv)
    else:
        # Shallow copy so gap/hotlist annotations do not leak across days
        top_slots = [
            VesselSlot(**{**s.__dict__}) for s in top_slots
        ]
        rest_slots = [
            VesselSlot(**{**s.__dict__}) for s in rest_slots
        ]

    gaps = {} if simulate else load_terrestrial_gaps(db_path=db_path, now=now)
    hot = set() if simulate else load_hotlist_imos()
    for s in top_slots + rest_slots:
        s.gap_hours = float(gaps.get(s.imo, 1e9))
        if s.imo in hot:
            s.in_hotlist = True

    tier_used = state.get("tier_used") or {"top": 0, "rest": 0}
    top_tier_left = max(0, TOP_TIER_BUDGET - int(tier_used.get("top") or 0))
    rest_tier_left = max(0, REST_TIER_BUDGET - int(tier_used.get("rest") or 0))

    # Nominal 10/6 split, then rebalance leftover within hard_cap
    want_top = min(DAILY_TOP_QUOTA, top_tier_left, hard_cap)
    want_rest = min(DAILY_REST_QUOTA, rest_tier_left, max(0, hard_cap - want_top))
    # If top undersubscribed, give remainder to rest (and vice versa)
    leftover = hard_cap - want_top - want_rest
    if leftover > 0 and rest_tier_left > want_rest:
        add = min(leftover, rest_tier_left - want_rest)
        want_rest += add
        leftover -= add
    if leftover > 0 and top_tier_left > want_top:
        want_top += min(leftover, top_tier_left - want_top)

    rr = state.get("rr_cursor") or {"top": 0, "rest": 0}
    picked_top, cur_top, reasons_top = _select_tier(
        top_slots,
        quota=want_top,
        gap_threshold=TOP_GAP_HOURS,
        cursor=int(rr.get("top") or 0),
        state=state,
        rolling_days=TOP_ROLLING_DAYS,
        now=now,
    )
    picked_rest, cur_rest, reasons_rest = _select_tier(
        rest_slots,
        quota=want_rest,
        gap_threshold=REST_GAP_HOURS,
        cursor=int(rr.get("rest") or 0),
        state=state,
        rolling_days=REST_ROLLING_DAYS,
        now=now,
    )

    reasons = {**reasons_top, **reasons_rest}
    plan = AllocatorPlan(
        day=day_s,
        top_imos=[s.imo for s in picked_top],
        rest_imos=[s.imo for s in picked_rest],
        daily_quota=len(picked_top) + len(picked_rest),
        weekly_remaining=week_rem,
        month_remaining=month_rem,
        reasons=reasons,
    )
    # Stash cursors for commit
    plan_meta = {
        "rr_cursor": {"top": cur_top, "rest": cur_rest},
        "want_top": want_top,
        "want_rest": want_rest,
    }
    state["_pending_plan_meta"] = plan_meta
    save_state(state, state_path)
    return plan


def commit_allocation(
    plan: AllocatorPlan,
    *,
    billed_imos: Iterable[str],
    state_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record successful VF spends against allocator state (after client billed)."""
    now = now or _utc_now()
    state = load_state(state_path, now=now)
    billed = [str(x) for x in billed_imos if x]
    now_s = _utc_iso(now)
    top_set = set(plan.top_imos)
    for imo in billed:
        tier = "top" if imo in top_set else "rest"
        state["tier_used"][tier] = int(state["tier_used"].get(tier) or 0) + 1
        state["last_vf_at"][imo] = now_s
        state["vf_hits"][imo] = int(state["vf_hits"].get(imo) or 0) + 1
    state["week_used"] = int(state.get("week_used") or 0) + len(billed)
    day_used = dict(state.get("day_used") or {})
    day_used[plan.day] = int(day_used.get(plan.day) or 0) + len(billed)
    # prune old days
    state["day_used"] = {k: v for k, v in sorted(day_used.items())[-45:]}
    meta = state.pop("_pending_plan_meta", None) or {}
    if isinstance(meta.get("rr_cursor"), dict):
        state["rr_cursor"] = meta["rr_cursor"]
    save_state(state, state_path)
    return {
        "ok": True,
        "billed": len(billed),
        "tier_used": state["tier_used"],
        "week_used": state["week_used"],
        "day_used": state["day_used"].get(plan.day),
    }


def simulate_month(
    *,
    days: int = 30,
    fleet_csv: Path | None = None,
    state_path: Path | None = None,
    start: date | None = None,
) -> dict[str, Any]:
    """Offline 30-day simulation: every planned credit is treated as successful spend.

    Does not call VesselFinder HTTP. Uses an isolated state file when path provided.
    """
    start = start or date(2026, 3, 1)
    path = state_path or (ROOT / "data" / "archive" / "vf_allocator_sim_state.json")
    if path.is_file():
        path.unlink()
    save_state(
        _empty_state(start.strftime("%Y-%m"), week=_week_key(datetime(start.year, start.month, start.day, tzinfo=timezone.utc))),
        path,
    )

    hits: dict[str, int] = {}
    used = 0
    daily_plans: list[dict[str, Any]] = []
    top_slots, rest_slots = load_fleet_universe(fleet_csv)
    all_imos = [s.imo for s in top_slots] + [s.imo for s in rest_slots]
    top_set = {s.imo for s in top_slots}

    for i in range(days):
        day = start + timedelta(days=i)
        day_s = day.isoformat()
        now = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)
        # Month boundary: reset monthly used counter (tier_used resets inside load_state)
        if day.day == 1 and i > 0:
            used = 0
        plan = plan_daily_allocation(
            day=day_s,
            state_path=path,
            fleet_csv=fleet_csv,
            now=now,
            monthly_remaining=MONTHLY_BUDGET - used,
            simulate=True,
        )
        billed = plan.all_imos
        room = MONTHLY_BUDGET - used
        if len(billed) > room:
            billed = billed[:room]
        commit_allocation(
            AllocatorPlan(
                day=day_s,
                top_imos=[x for x in billed if x in top_set],
                rest_imos=[x for x in billed if x not in top_set],
            ),
            billed_imos=billed,
            state_path=path,
            now=now,
        )
        used += len(billed)
        for imo in billed:
            hits[imo] = hits.get(imo, 0) + 1
        daily_plans.append(
            {"day": day_s, "n": len(billed), "top": len([x for x in billed if x in top_set])}
        )

    # Recompute used as sum of daily plans for single-month sims
    total_used = sum(p["n"] for p in daily_plans)
    top_hits = [hits.get(s.imo, 0) for s in top_slots]
    rest_hits = [hits.get(s.imo, 0) for s in rest_slots]
    return {
        "ok": True,
        "days": days,
        "used": total_used if days <= 31 else min(total_used, MONTHLY_BUDGET),
        "remaining": max(0, MONTHLY_BUDGET - min(total_used, MONTHLY_BUDGET)) if days <= 31 else 0,
        "monthly_budget": MONTHLY_BUDGET,
        "fleet_n": len(all_imos),
        "top_n": len(top_slots),
        "rest_n": len(rest_slots),
        "top_min_hits": min(top_hits) if top_hits else 0,
        "top_max_hits": max(top_hits) if top_hits else 0,
        "rest_min_hits": min(rest_hits) if rest_hits else 0,
        "rest_max_hits": max(rest_hits) if rest_hits else 0,
        "daily_plans": daily_plans,
        "within_budget": total_used <= MONTHLY_BUDGET if days <= 31 else True,
    }


def allocator_status() -> dict[str, Any]:
    """Public snapshot for health.fleet_archive.vf_budget."""
    from services.vesselfinder_budget import get_budget_status

    st = load_state()
    budget = get_budget_status()
    day_s = _utc_now().strftime("%Y-%m-%d")
    return {
        "used": int(budget.get("used") or 0),
        "remaining": int(budget.get("remaining") or 0),
        "monthly_budget": int(budget.get("monthly_budget") or MONTHLY_BUDGET),
        "daily_quota": DAILY_TOTAL_QUOTA,
        "daily_used_today": int((st.get("day_used") or {}).get(day_s) or 0),
        "weekly_cap": WEEKLY_CAP,
        "weekly_used": int(st.get("week_used") or 0),
        "tier_split": {"top": TOP_TIER_BUDGET, "rest": REST_TIER_BUDGET},
        "tier_used": st.get("tier_used") or {"top": 0, "rest": 0},
        "status": budget.get("status"),
        "month": budget.get("month"),
        "warn_remaining_threshold": budget.get("warn_remaining_threshold"),
    }


__all__ = (
    "ALLOWED_SOURCES",
    "AllocatorPlan",
    "DAILY_TOTAL_QUOTA",
    "MONTHLY_BUDGET",
    "TARGET_FLEET_N",
    "TOP_N",
    "allocator_status",
    "commit_allocation",
    "load_fleet_universe",
    "plan_daily_allocation",
    "simulate_month",
)
