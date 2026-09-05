#!/usr/bin/env python3
"""
ORACLE-1001 · Double Cross-Validation Audit
===========================================
Phase 1: backend Sum / Global % / Fill / Synth integrity for TOP-100/200/500.
Phase 2–3: recompute Tri-Donut symmetry + cascade filter consistency from payload;
           nav routing; optional headless DOM audit via playwright if installed.

Usage:
  python scripts/verify_data_integrity.py
  python scripts/verify_data_integrity.py --no-browser
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
WEB = ROOT / "web"
REPORT_PATH = OUTPUT / "audit_data_report.json"
FLEET_CSV = OUTPUT / "fleet_database.csv"
FLEET_BASE = 81_300_000.0  # canonical global denominator for display formula
EPS_PCT = 0.01
EPS_TRI = 0.05
EPS_DWT = 1.0  # tons tolerance on rounded sums

DASHBOARDS = {
    "top100": {
        "html": OUTPUT / "top100_analytics.html",
        "n": 100,
        "dwt_key": "top100_dwt",
        "share_key": "top100_share_pct",
        "payload_re": re.compile(r"const PAYLOAD\s*=\s*(\{.*?\});\s*const ALL", re.S),
        "has_tri_donut": False,
        "mode_key": None,
    },
    "top200": {
        "html": OUTPUT / "top200_analytics.html",
        "n": 200,
        "dwt_key": "top200_dwt",
        "share_key": "top200_share_pct",
        "payload_re": re.compile(
            r"window\.__TOP200_PAYLOAD__\s*=\s*(\{.*?\});\s*</script>", re.S
        ),
        "has_tri_donut": True,
        "mode_key": "top200_d13_view_mode",
    },
    "top500": {
        "html": OUTPUT / "top500_analytics.html",
        "n": 500,
        "dwt_key": "top500_dwt",
        "share_key": "top500_share_pct",
        "payload_re": re.compile(
            r"window\.__TOP500_PAYLOAD__\s*=\s*(\{.*?\});\s*</script>", re.S
        ),
        "has_tri_donut": True,
        "mode_key": "top500_d13_view_mode",
    },
}


def _f(v: Any) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, str) and v.strip().lower() in {"", "nan", "none", "undefined", "null", "—", "-"}:
        return True
    return False


def load_payload(html_path: Path, pattern: re.Pattern[str]) -> dict:
    text = html_path.read_text(encoding="utf-8")
    m = pattern.search(text)
    if not m:
        raise ValueError(f"payload not found in {html_path.name}")
    return json.loads(m.group(1))


def g_pct(dwt: float, fleet: float) -> float:
    return 100.0 * dwt / fleet if fleet else 0.0


def flag_bucket(fl: str) -> str:
    known = {"Маршалловы О-ва", "Панама", "Либерия", "Багамы", "Бермуды"}
    return fl if fl in known else "Китай / Прочие"


def aggregate_dwt(vessels: list[dict], key_fn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for v in vessels:
        k = key_fn(v) or "—"
        if k not in out:
            out[k] = {"n": 0, "dwt": 0.0}
        out[k]["n"] += 1
        out[k]["dwt"] += _f(v.get("dwt_tons")) or 0.0
    return out


def sum_sector_g_pct(agg: dict[str, dict], fleet: float) -> float:
    return sum(g_pct(x["dwt"], fleet) for x in agg.values())


def ensure_validation_assets() -> None:
    """Copy JS suite into output/js for runtime DOM audit."""
    src = WEB / "top_validation_suite.js"
    dst_dir = OUTPUT / "js"
    dst_dir.mkdir(parents=True, exist_ok=True)
    if src.exists():
        (dst_dir / "top_validation_suite.js").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def audit_module(name: str, cfg: dict, fleet_csv_dwt: float | None) -> dict:
    t0 = time.perf_counter()
    disc: list[str] = []
    warns: list[str] = []
    metrics: dict[str, Any] = {}
    html: Path = cfg["html"]

    if not html.exists():
        return {
            "status": "FAILED",
            "discrepancies": [f"{name}: missing HTML {html}"],
            "warnings": [],
            "metrics": {},
            "summary": {},
        }

    try:
        payload = load_payload(html, cfg["payload_re"])
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "FAILED",
            "discrepancies": [f"{name}: payload parse error: {exc}"],
            "warnings": [],
            "metrics": {},
            "summary": {},
        }

    vessels = payload.get("vessels") or []
    fleet = float(payload.get("fleet_total_dwt") or FLEET_BASE)
    dwt_key = cfg["dwt_key"]
    share_key = cfg["share_key"]
    declared_dwt = float(payload.get(dwt_key) or 0)
    declared_share = float(payload.get(share_key) or 0)
    recomputed = sum(_f(v.get("dwt_tons")) or 0.0 for v in vessels)
    recomputed_share = round(g_pct(recomputed, fleet), 2)
    display_share = round(g_pct(recomputed, FLEET_BASE), 2)

    summary = {
        "vessel_count": len(vessels),
        "expected_n": cfg["n"],
        "declared_dwt": declared_dwt,
        "recomputed_dwt": round(recomputed, 1),
        "fleet_total_dwt": fleet,
        "declared_share_pct": declared_share,
        "recomputed_share_pct": recomputed_share,
        "display_formula_share_pct": display_share,
    }

    # Count
    if len(vessels) != cfg["n"]:
        disc.append(f"{name}: vessel count {len(vessels)} != {cfg['n']}")

    # Sum Validation
    if abs(recomputed - declared_dwt) > EPS_DWT:
        disc.append(
            f"{name}: DWT sum mismatch recomputed={recomputed:.1f} declared={declared_dwt:.1f}"
        )

    # Global % vs payload share (fleet denominator from payload)
    if abs(recomputed_share - declared_share) > EPS_PCT:
        disc.append(
            f"{name}: share mismatch recomputed={recomputed_share:.2f}% declared={declared_share:.2f}%"
        )

    # Display formula vs 81.3M — warn if fleet drifts >0.5%
    if fleet and abs(fleet - FLEET_BASE) / FLEET_BASE > 0.005:
        warns.append(
            f"{name}: fleet_total_dwt={fleet:,.0f} differs from canonical 81,300,000 "
            f"(display formula uses payload fleet; Δ={abs(fleet - FLEET_BASE):,.0f})"
        )

    if fleet_csv_dwt is not None and abs(fleet - fleet_csv_dwt) > max(100.0, fleet * 0.001):
        warns.append(
            f"{name}: payload fleet_total_dwt={fleet:,.0f} vs CSV sum={fleet_csv_dwt:,.0f}"
        )

    # Null / NaN integrity on critical fields
    crit = ["dwt_tons", "gt", "imo", "flag", "vessel_type"]
    if cfg["has_tri_donut"]:
        crit += ["ops_zone", "tech_type", "vtype"]
    for i, v in enumerate(vessels):
        for field in crit:
            val = v.get(field)
            if field in ("dwt_tons", "gt"):
                x = _f(val)
                if math.isnan(x) or x < 0:
                    disc.append(f"{name}: vessel[{i}] bad {field}={val!r}")
            elif _is_blank(val):
                disc.append(f"{name}: vessel[{i}] blank {field}")
        # undefined/NaN string pollution
        for field, val in v.items():
            if isinstance(val, str) and ("undefined" in val.lower() or val.strip() == "NaN"):
                disc.append(f"{name}: vessel[{i}].{field} contains '{val}'")

    # Fill / Synth
    avg_fill = payload.get("avg_fill_pct")
    if avg_fill is None and vessels and "fill_pct" in vessels[0]:
        avg_fill = round(sum(_f(v.get("fill_pct")) or 0 for v in vessels) / len(vessels), 1)
    if avg_fill is not None and abs(float(avg_fill) - 100.0) > 0.5:
        disc.append(f"{name}: Fill Rate {avg_fill}% != 100%")
    elif avg_fill is None and name == "top100":
        # TOP-100 has no fill_pct in payload — estimate from non-empty criticals
        fills = []
        for v in vessels:
            cols = ["vessel_name", "imo", "vessel_type", "flag", "dwt_tons", "gt"]
            ok = sum(1 for c in cols if not _is_blank(v.get(c)) and not (c in ("dwt_tons", "gt") and (_f(v.get(c)) or 0) <= 0))
            fills.append(100.0 * ok / len(cols))
        avg_fill = round(sum(fills) / max(len(fills), 1), 1)
        if avg_fill < 99.0:
            disc.append(f"{name}: estimated Fill Rate {avg_fill}% < 99%")
        else:
            warns.append(f"{name}: no avg_fill_pct in payload; estimated {avg_fill}%")
    summary["avg_fill_pct"] = avg_fill

    prov = (payload.get("provenance") or {}).get("pct") or {}
    synth = float(prov.get("SYNTH", 0) if prov else -1)
    if cfg["has_tri_donut"]:
        if synth < 0:
            disc.append(f"{name}: missing provenance.pct.SYNTH")
        elif abs(synth) > 0.01:
            disc.append(f"{name}: Synth OSINT={synth}% != 0.0%")
        summary["synth_pct"] = synth
        summary["osint_pct"] = prov.get("OSINT")
    else:
        # TOP-100: count synthetic_fields tokens on audit surface
        synth_hits = sum(1 for v in vessels if (v.get("synthetic_fields") or "").strip())
        # After provenance fix, synthetic_fields may still list non-audit leftovers — cell SYNTH should be 0 on D07
        # Soft check: no vessel may have NaN dwt
        summary["vessels_with_synthetic_fields_tag"] = synth_hits
        if synth_hits and name == "top100":
            warns.append(
                f"{name}: {synth_hits} vessels still carry synthetic_fields tags "
                "(cell-level D07 may still be 0% — see TOP-100 provenance fix)"
            )

    # Tri-Donut symmetry (200/500)
    if cfg["has_tri_donut"]:
        t_sym0 = time.perf_counter()
        zones = aggregate_dwt(vessels, lambda v: v.get("ops_zone"))
        techs = aggregate_dwt(vessels, lambda v: v.get("tech_type"))
        flags = aggregate_dwt(
            vessels, lambda v: flag_bucket(v.get("flag_short") or v.get("flag") or "—")
        )
        z_pct = sum_sector_g_pct(zones, fleet)
        t_pct = sum_sector_g_pct(techs, fleet)
        f_pct = sum_sector_g_pct(flags, fleet)
        base_pct = g_pct(recomputed, fleet)
        metrics["tri_donut_symmetry_ms"] = round((time.perf_counter() - t_sym0) * 1000, 2)
        summary["tri_donut"] = {
            "zone_g_pct_sum": round(z_pct, 4),
            "tech_g_pct_sum": round(t_pct, 4),
            "flag_g_pct_sum": round(f_pct, 4),
            "slice_g_pct": round(base_pct, 4),
        }
        for label, val in (("zone", z_pct), ("tech", t_pct), ("flag", f_pct)):
            if abs(val - base_pct) > EPS_TRI:
                disc.append(
                    f"{name}: Tri-Donut {label} sector sum {val:.4f}% != slice {base_pct:.4f}% (±{EPS_TRI})"
                )
        if abs(z_pct - t_pct) > EPS_TRI or abs(t_pct - f_pct) > EPS_TRI:
            disc.append(
                f"{name}: Tri-Donut asymmetry z={z_pct:.4f} t={t_pct:.4f} f={f_pct:.4f}"
            )

        # Cascade: each sector N equals filtered vessel count
        t_c0 = time.perf_counter()
        for key, agg, attr in (
            ("ops_zone", zones, "ops_zone"),
            ("tech_type", techs, "tech_type"),
        ):
            for seg_key, info in agg.items():
                filtered = [v for v in vessels if v.get(attr) == seg_key]
                if len(filtered) != info["n"]:
                    disc.append(
                        f"{name}: cascade {key}={seg_key} n={info['n']} filtered={len(filtered)}"
                    )
                # global % of sector
                gp = g_pct(info["dwt"], fleet)
                gp2 = g_pct(sum(_f(v.get("dwt_tons")) or 0 for v in filtered), fleet)
                if abs(gp - gp2) > EPS_PCT:
                    disc.append(f"{name}: sector gPct drift {key}={seg_key}")
        for seg_key, info in flags.items():
            filtered = [
                v
                for v in vessels
                if flag_bucket(v.get("flag_short") or v.get("flag") or "—") == seg_key
            ]
            if len(filtered) != info["n"]:
                disc.append(
                    f"{name}: cascade flag={seg_key} n={info['n']} filtered={len(filtered)}"
                )
        metrics["cascade_filter_ms"] = round((time.perf_counter() - t_c0) * 1000, 2)
        if metrics["cascade_filter_ms"] > 150:
            warns.append(
                f"{name}: cascade recompute {metrics['cascade_filter_ms']}ms > 150ms budget"
            )

    # Nav links exist
    html_text = html.read_text(encoding="utf-8")
    for link in ("top100_analytics.html", "top200_analytics.html", "top500_analytics.html"):
        if link not in html_text:
            disc.append(f"{name}: nav missing link to {link}")
        elif not (OUTPUT / link).exists():
            disc.append(f"{name}: nav target missing on disk: {link}")

    # Validation suite hook
    if "top_validation_suite.js" not in html_text:
        warns.append(f"{name}: top_validation_suite.js not referenced in HTML")

    metrics["module_audit_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    status = "FAILED" if disc else ("PASSED_WITH_WARNINGS" if warns else "SUCCESS")
    return {
        "status": status,
        "discrepancies": disc,
        "warnings": warns,
        "metrics": metrics,
        "summary": summary,
    }


def http_smoke(base: str = "http://127.0.0.1:8765") -> dict:
    out = {}
    for name, cfg in DASHBOARDS.items():
        url = f"{base}/output/{cfg['html'].name}"
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                out[name] = {"http": resp.status, "ok": resp.status == 200}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"http": None, "ok": False, "error": str(exc)}
    suite = f"{base}/output/js/top_validation_suite.js"
    try:
        with urllib.request.urlopen(suite, timeout=6) as resp:
            out["validation_suite"] = {"http": resp.status, "ok": resp.status == 200}
    except Exception as exc:  # noqa: BLE001
        out["validation_suite"] = {"http": None, "ok": False, "error": str(exc)}
    return out


def browser_dom_audit(base: str = "http://127.0.0.1:8765") -> dict:
    """Optional Playwright pass — runs embedded ORACLE_AUDIT.run()."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return {
            "status": "SKIPPED",
            "reason": "playwright not installed",
            "modules": {},
        }

    modules = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, cfg in DASHBOARDS.items():
            if not cfg["has_tri_donut"] and name == "top100":
                url = f"{base}/output/{cfg['html'].name}?audit=1"
            else:
                url = f"{base}/output/{cfg['html'].name}?audit=1"
            page = browser.new_page()
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            t0 = time.perf_counter()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(400)
            result = page.evaluate(
                """async () => {
                  if (window.ORACLE_AUDIT && typeof window.ORACLE_AUDIT.run === 'function') {
                    return await window.ORACLE_AUDIT.run();
                  }
                  return { status: 'FAILED', discrepancies: ['ORACLE_AUDIT missing'] };
                }"""
            )
            # Toggle view mode for 200/500
            if cfg["mode_key"]:
                for mode in ("sunburst", "trio"):
                    page.evaluate(
                        """(m) => {
                          const btn = document.querySelector(`.d13-mode[data-mode="${m}"]`);
                          if (btn) btn.click();
                        }""",
                        mode,
                    )
                    page.wait_for_timeout(120)
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            modules[name] = {
                "result": result,
                "console_errors": console_errors[:20],
                "page_ms": elapsed,
            }
            page.close()
        browser.close()
    disc = []
    for name, mod in modules.items():
        r = mod.get("result") or {}
        if r.get("status") == "FAILED" or (r.get("discrepancies") or []):
            disc.extend([f"dom:{name}: {d}" for d in (r.get("discrepancies") or ["failed"])])
        if mod.get("console_errors"):
            disc.append(f"dom:{name}: console errors: {mod['console_errors'][:3]}")
    return {
        "status": "FAILED" if disc else "SUCCESS",
        "discrepancies": disc,
        "modules": modules,
    }


def fleet_csv_sum() -> float | None:
    if not FLEET_CSV.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_csv(FLEET_CSV, low_memory=False)
        if "dwt_tons" not in df.columns:
            return None
        return float(df["dwt_tons"].fillna(0).astype(float).sum())
    except Exception:  # noqa: BLE001
        return None


def merge_status(statuses: list[str]) -> str:
    if any(s == "FAILED" for s in statuses):
        return "FAILED"
    if any(s == "PASSED_WITH_WARNINGS" for s in statuses):
        return "PASSED_WITH_WARNINGS"
    return "SUCCESS"


def print_table(report: dict) -> None:
    print()
    print("=" * 72)
    print(" ORACLE-1001 · DOUBLE CROSS-VALIDATION AUDIT")
    print("=" * 72)
    print(f" overall : {report['status']}")
    print(f" generated: {report.get('generated_at')}")
    print("-" * 72)
    print(f"{'Module':<12} {'Status':<22} {'N':>5} {'DWT (М т)':>12} {'Share%':>8} {'Synth%':>8}")
    print("-" * 72)
    for key in ("top100", "top200", "top500"):
        block = report["modules"][key]
        s = block.get("summary") or {}
        dwt_m = (s.get("recomputed_dwt") or 0) / 1e6
        synth = s.get("synth_pct")
        synth_s = f"{synth:.2f}" if isinstance(synth, (int, float)) else "n/a"
        print(
            f"{key:<12} {block.get('status', '?'):<22} {s.get('vessel_count', 0):>5} "
            f"{dwt_m:>12.2f} {s.get('recomputed_share_pct', 0):>8.2f} {synth_s:>8}"
        )
    print("-" * 72)
    discs = report.get("discrepancies_found") or []
    if discs:
        print(f" discrepancies ({len(discs)}):")
        for d in discs[:40]:
            print(f"  - {d}")
        if len(discs) > 40:
            print(f"  … +{len(discs) - 40} more")
    else:
        print(" discrepancies: none")
    warns = report.get("warnings") or []
    if warns:
        print(f" warnings ({len(warns)}):")
        for w in warns[:20]:
            print(f"  ! {w}")
    perf = report.get("performance_metrics") or {}
    print("-" * 72)
    print(" performance:", json.dumps(perf, ensure_ascii=False))
    http = report.get("http_smoke") or {}
    print(" http_smoke:", json.dumps(http, ensure_ascii=False))
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="ORACLE-1001 data integrity audit")
    ap.add_argument("--no-browser", action="store_true", help="Skip Playwright DOM audit")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = ap.parse_args()

    ensure_validation_assets()
    fleet_sum = fleet_csv_sum()
    modules = {}
    all_disc: list[str] = []
    all_warn: list[str] = []
    perf: dict[str, Any] = {}

    for name, cfg in DASHBOARDS.items():
        block = audit_module(name, cfg, fleet_sum)
        modules[name] = block
        all_disc.extend(block.get("discrepancies") or [])
        all_warn.extend(block.get("warnings") or [])
        for mk, mv in (block.get("metrics") or {}).items():
            perf[f"{name}.{mk}"] = mv

    http = http_smoke(args.base_url)
    for name, info in http.items():
        if not info.get("ok"):
            all_warn.append(f"http:{name}: {info}")

    dom = {"status": "SKIPPED", "reason": "--no-browser"}
    if not args.no_browser:
        dom = browser_dom_audit(args.base_url)
        if dom.get("status") == "FAILED":
            all_disc.extend(disc for disc in (dom.get("discrepancies") or []))
        elif dom.get("status") == "SKIPPED":
            all_warn.append(f"dom_audit: {dom.get('reason')}")
        else:
            # merge module page timings
            for name, mod in (dom.get("modules") or {}).items():
                perf[f"{name}.dom_page_ms"] = mod.get("page_ms")

    overall = merge_status(
        [modules[k]["status"] for k in ("top100", "top200", "top500")]
        + ([dom["status"]] if dom.get("status") in {"SUCCESS", "FAILED", "PASSED_WITH_WARNINGS"} else [])
    )
    # HTTP failures alone → warnings unless page missing on disk already failed
    if any(not (http.get(k) or {}).get("ok") for k in ("top100", "top200", "top500")):
        if overall == "SUCCESS":
            overall = "PASSED_WITH_WARNINGS"

    report = {
        "status": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top100_status": modules["top100"]["status"],
        "top200_status": modules["top200"]["status"],
        "top500_status": modules["top500"]["status"],
        "discrepancies_found": all_disc,
        "warnings": all_warn,
        "performance_metrics": perf,
        "http_smoke": http,
        "dom_audit": {
            "status": dom.get("status"),
            "reason": dom.get("reason"),
            "discrepancy_count": len(dom.get("discrepancies") or []),
        },
        "modules": {
            k: {
                "status": modules[k]["status"],
                "summary": modules[k]["summary"],
                "metrics": modules[k]["metrics"],
                "discrepancies": modules[k]["discrepancies"],
                "warnings": modules[k]["warnings"],
            }
            for k in ("top100", "top200", "top500")
        },
        "canonical_fleet_dwt": FLEET_BASE,
        "fleet_csv_dwt": fleet_sum,
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_table(report)
    print(f"\nReport written: {REPORT_PATH}")
    return 0 if overall != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
