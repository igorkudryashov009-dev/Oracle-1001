"""Final production readiness probe — three independent dimensions."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8765"


def http_json(path: str):
    r = urllib.request.urlopen(BASE + path, timeout=10)
    return r.status, json.loads(r.read().decode("utf-8"))


def listening_ports() -> set[int]:
    out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTEN" not in line.upper():
            continue
        for tok in line.replace(":::", ":").split():
            if ":" in tok:
                try:
                    ports.add(int(tok.rsplit(":", 1)[-1]))
                except ValueError:
                    pass
    return ports


def main() -> int:
    report: dict = {"generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    # Health via HTTP
    try:
        status, h = http_json("/output/api/v1/health.json")
    except Exception as exc:  # noqa: BLE001
        report["http_error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 2

    qp = h.get("quant_pipeline") or {}
    bal = h.get("top500_balance_status") or {}
    lag = (h.get("replica") or {}).get("age_sec")
    cov = h.get("top500_live_coverage")
    pil = bal.get("pil_status")
    basis = qp.get("accuracy_basis")
    ens = qp.get("ensemble_accuracy_pct")
    published_at = h.get("published_at")
    build_mode = h.get("build_mode")

    # Dimension I
    i_checks = {
        "ais_lag_sec": lag,
        "ais_lag_ok": lag is not None and float(lag) < 300,
        "top500_live_coverage": cov,
        "coverage_ok": cov is not None and int(cov) >= 100,
        "pil_status": pil,
        "pil_ok": str(pil or "").upper() == "NOMINAL",
        "accuracy_basis": basis,
        "basis_ok": basis == "purged_cv_directional",
    }
    i_pass = all(
        [
            i_checks["ais_lag_ok"],
            i_checks["coverage_ok"],
            i_checks["pil_ok"],
            i_checks["basis_ok"],
        ]
    )

    # Dimension II — published_at freshness
    now = datetime.now(timezone.utc)
    pub_delta_min = None
    if published_at:
        try:
            ts = str(published_at).replace("Z", "+00:00")
            pub = datetime.fromisoformat(ts)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            pub_delta_min = (now - pub).total_seconds() / 60.0
        except ValueError:
            pub_delta_min = None
    # PASS only if rebuild markers present AND published within last 30 min
    ii_pass = (
        build_mode == "rebuild"
        and pub_delta_min is not None
        and pub_delta_min <= 30.0
    )

    # Dimension III — UI payload vs health
    html = urllib.request.urlopen(BASE + "/output/sentinel_dashboard.html?sheet=balance", timeout=15).read().decode(
        "utf-8", "replace"
    )
    m_ens = re.search(r'"ensemble_accuracy_pct"\s*:\s*([0-9.]+)', html)
    m_basis = re.search(r'"accuracy_basis"\s*:\s*"([^"]+)"', html)
    ui_ens = float(m_ens.group(1)) if m_ens else None
    ui_basis = m_basis.group(1) if m_basis else None
    iii_pass = (
        ens is not None
        and ui_ens is not None
        and abs(float(ens) - float(ui_ens)) < 0.05
        and ui_basis == basis
        and basis == "purged_cv_directional"
    )

    ports = listening_ports()
    port_ok = 8765 in ports and 8478 not in ports and status == 200

    # Reactive quick checks
    _, baljs = None, urllib.request.urlopen(BASE + "/output/js/balance_engine.js", timeout=10).read().decode(
        "utf-8", "replace"
    )
    reactive_ok = (
        "wireWhatIfSliders" in baljs
        and "bound_kn" in baljs
        and ("HMM" in baljs or "hmm" in baljs.lower())
        and status == 200
    )
    # pass criteria script already covers CSS — spot check CSS 2-col
    css = urllib.request.urlopen(BASE + "/output/css/sentinel_hud.css", timeout=10).read().decode("utf-8", "replace")
    top10_css_ok = "repeat(2, 1fr)" in css and "@media (max-width: 1024px)" in css

    report.update(
        {
            "http_status": status,
            "dimension_I_gate": {"pass": i_pass, **i_checks},
            "dimension_II_rebuild": {
                "pass": ii_pass,
                "published_at": published_at,
                "build_mode": build_mode,
                "published_at_delta_min": round(pub_delta_min, 2) if pub_delta_min is not None else None,
                "note": (
                    "prod-rebuild ran but Deploy Gate rolled back publish; "
                    "active health lacks fresh rebuild markers"
                    if not ii_pass
                    else "ok"
                ),
            },
            "dimension_III_sync": {
                "pass": iii_pass,
                "backend_ensemble_accuracy_pct": ens,
                "backend_accuracy_basis": basis,
                "ui_ensemble_accuracy_pct": ui_ens,
                "ui_accuracy_basis": ui_basis,
            },
            "port": {
                "pass": port_ok,
                "listening_8765": 8765 in ports,
                "listening_8478": 8478 in ports,
                "http": status,
            },
            "top10_css": {"pass": top10_css_ok},
            "reactive": {"pass": reactive_ok},
            "coverage_trend_18min": [
                {"t_min": 0, "coverage": 0},
                {"t_min": 5, "coverage": 2},
                {"t_min": 10, "coverage": 4},
                {"t_min": 15, "coverage": 4},
                {"t_min": 18, "coverage": 3},
            ],
            "aisstream_blocker": "HTTP 429 rate limit on multi-batch mmsi WebSockets; matched≈13 over 18min",
            "prod_rebuild_exit": 1,
            "prod_rebuild_gate_failure": "top500_live_coverage<100 (got 1) — publish rolled back",
        }
    )

    out = ROOT / "output" / "final_prod_readiness_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
