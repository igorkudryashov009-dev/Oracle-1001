import json, pathlib, sys

data = json.loads(pathlib.Path("output/fleet_database.json").read_text(encoding="utf-8"))

TZ = [
    "vessel_name","imo","mmsi","call_sign","vessel_type","built_year","age_years",
    "flag","dwt_tons","gt","loa_m","beam_m","draft_m","nav_status","speed_knots",
    "destination_port","destination_context","departure_port","arrival_datetime",
    "compliance_risk_level"
]

EMPTY = {"", "\u2014", "-", "none", "\u043d\u0435 \u0438\u0437\u0432\u043b\u0435\u0447\u0435\u043d\u043e", "nan", "n/a"}

def is_present(v):
    if v is None: return False
    return str(v).strip().lower() not in EMPTY

vessels = data if isinstance(data, list) else data.get("vessels", [])
total = len(vessels)

col_counts = {}
for c in TZ:
    col_counts[c] = sum(1 for v in vessels if is_present(v.get(c)))

synth_counts = {}
for v in vessels:
    for f in str(v.get("synthetic_fields") or "").split(";"):
        f = f.strip()
        if f:
            synth_counts[f] = synth_counts.get(f, 0) + 1

avg = sum(col_counts.values()) / (len(TZ) * total) * 100
print(f"\nVessels: {total}  |  AVG FILL RATE: {avg:.1f}%")
green  = sum(1 for n in col_counts.values() if n/total >= .80)
yellow = sum(1 for n in col_counts.values() if .50 <= n/total < .80)
red    = sum(1 for n in col_counts.values() if n/total < .50)
print(f"Green (>=80%): {green}  Yellow (50-79%): {yellow}  Red (<50%): {red}")
print(f"\n{'COLUMN':<32} {'PCT':>7}  {'N':>6}/{total}  STATUS")
print("-"*70)
for c in TZ:
    n   = col_counts[c]
    pct = n / total * 100
    s   = "G" if pct >= 80 else ("Y" if pct >= 50 else "R")
    print(f"[{s}] {c:<30} {pct:6.1f}%  {n:6d}")

print(f"\n-- Synthetic fields injected --")
for f, cnt in sorted(synth_counts.items(), key=lambda x: -x[1]):
    print(f"  {f:<28} {cnt:5d} / {total} ({cnt/total*100:.1f}%)")
