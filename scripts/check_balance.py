"""Quick Balance sheet verification."""
import json, sys
sys.path.insert(0, ".")
from build_sentinel_dashboard import write_sentinel_dashboard
from pathlib import Path

p = write_sentinel_dashboard()
bal = p.get("balance", {})
print("=== BALANCE PAYLOAD SUMMARY ===")
print(f"Status:     {bal.get('status')}")
print(f"Fleet size: {bal.get('fleet_size')}")
cl = bal.get("cargo_load", {})
print(f"Cargo:  laden={cl.get('laden_count')} ballast={cl.get('ballast_count')} M3={cl.get('total_m3_transit')}")
print(f"DAR:    {bal.get('dar', {}).get('dar_pct')}%  severity={bal.get('dar',{}).get('severity')}")
print(f"ΔV:     index={bal.get('delta_v',{}).get('delta_v_index')}  sts={bal.get('delta_v',{}).get('sts_cluster_count')}")
print(f"DFS:    grade={bal.get('dfs',{}).get('grade')}  score={bal.get('dfs',{}).get('dfs_score')}%")
print(f"PIL:    status={bal.get('pil',{}).get('status')}  lag={bal.get('pil',{}).get('lag_minutes')}m")
print(f"LSSI:   signal={bal.get('lssi',{}).get('signal')}  index={bal.get('lssi',{}).get('lssi_index')}")
print(f"Table:  {len(bal.get('anomaly_table', []))} anomaly rows")

print()
print("=== HEALTH /api/v1/health ===")
h = json.loads(Path("output/api/v1/health.json").read_text())
print(json.dumps(h.get("top500_balance_status", {}), indent=2))

print()
print("=== output/js/balance_engine.js ===")
p2 = Path("output/js/balance_engine.js")
print(f"Exists: {p2.exists()}  bytes={p2.stat().st_size if p2.exists() else 'N/A'}")
