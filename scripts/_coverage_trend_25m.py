"""25-min coverage trend sampler (Prompt 8.1 oscillation vs regression)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "aisstream_connector.log"
OUT = ROOT / "logs" / "coverage_trend_25m.jsonl"
SAMPLE_EVERY = 150  # 2.5 min
DURATION = 25 * 60


def parse_latest_tick(text: str) -> dict | None:
    ticks = re.findall(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?coverage_tick "
        r"top500_live_coverage=(\d+) .*?matched=(\d+) "
        r"raw_subscribed_hits=(\d+) unique_subscribed=(\d+) rate_limit_hits=(\d+)",
        text,
        flags=re.M,
    )
    if not ticks:
        return None
    ts, cov, matched, raw, uniq, rl = ticks[-1]
    return {
        "log_ts": ts,
        "top500_live_coverage": int(cov),
        "matched": int(matched),
        "raw_subscribed_hits": int(raw),
        "unique_subscribed": int(uniq),
        "rate_limit_hits": int(rl),
    }


def parse_latest_heartbeat(text: str) -> dict | None:
    beats = re.findall(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?heartbeat .*?"
        r"matched=(\d+) unmatched=(\d+).*?reconnects=(\d+) rate_limits=(\d+)",
        text,
        flags=re.M,
    )
    if not beats:
        return None
    ts, matched, unmatched, recon, rl = beats[-1]
    return {
        "log_ts": ts,
        "matched": int(matched),
        "unmatched": int(unmatched),
        "reconnects": int(recon),
        "rate_limits": int(rl),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    t0 = time.monotonic()
    samples: list[dict] = []
    print(f"sampling every {SAMPLE_EVERY}s for {DURATION}s -> {OUT}")
    while time.monotonic() - t0 < DURATION:
        text = LOG.read_text(encoding="utf-8", errors="ignore") if LOG.exists() else ""
        tick = parse_latest_tick(text)
        hb = parse_latest_heartbeat(text)
        # Also live coverage from DB if available
        cov_n = None
        try:
            from services.ais_health import compute_top500_live_coverage

            blob = compute_top500_live_coverage()
            cov_n = int(blob.get("top500_live_coverage") or 0)
        except Exception as exc:  # noqa: BLE001
            cov_n = None
            err = str(exc)
        else:
            err = None
        row = {
            "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_sec": round(time.monotonic() - t0, 1),
            "db_top500_live_coverage": cov_n,
            "tick": tick,
            "heartbeat": hb,
            "error": err,
        }
        samples.append(row)
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"[{row['elapsed_sec']:6.0f}s] db_cov={cov_n} "
            f"tick={tick} hb_recon={(hb or {}).get('reconnects')} "
            f"hb_rl={(hb or {}).get('rate_limits')}"
        )
        time.sleep(SAMPLE_EVERY)

    covs = [s["db_top500_live_coverage"] for s in samples if s["db_top500_live_coverage"] is not None]
    tick_covs = [
        (s["tick"] or {}).get("top500_live_coverage")
        for s in samples
        if s.get("tick")
    ]
    recon_max = max(((s.get("heartbeat") or {}).get("reconnects") or 0) for s in samples) if samples else 0
    rl_max = max(((s.get("heartbeat") or {}).get("rate_limits") or 0) for s in samples) if samples else 0
    summary = {
        "n_samples": len(samples),
        "db_cov_min": min(covs) if covs else None,
        "db_cov_max": max(covs) if covs else None,
        "db_cov_series": covs,
        "tick_cov_series": tick_covs,
        "reconnects_max": recon_max,
        "rate_limits_max": rl_max,
    }
    (ROOT / "logs" / "coverage_trend_25m_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print("SUMMARY", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
