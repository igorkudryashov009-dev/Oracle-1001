# Sentinel pilot offer

Measured readiness **72 / 100** on 2026-10-04. Public health was NOMINAL and acceptance was GREEN. The number is the sum of channels that are actually on. Parked channels stay at zero. That is the product, not a discount.

| Component | Points | What is measured |
|---|---|---|
| Pipeline NOMINAL | 40 | `pipeline_health_status` is NOMINAL |
| Terrestrial AIS | 10 | Live G3 sample meets the LIMITED gate |
| GFW | 10 | Settled archive day has verified events |
| VesselFinder | 0 | Commercial key is not valid |
| LLM | 0 | Not activated. This letter does not sell a digest |
| Satellite AIS | 0 | Adapter is off |
| Stability | 7 | 6 of 7 green acceptance days, weight 8, rounded |
| Pilot clients | 5 | Active clients at the cap of 5 |
| **Total** | **72** | |

A later `paid` event records the first invoice. It does not add points and it does not rewrite this score.

## Tariffs (USD)

The 30-day pilot is **$0**. List price starts after the pilot.

| Tariff | Pilot (30 days) | List price | Includes |
|---|---|---|---|
| Readonly HUD | $0 | $490 / month | HUD, 10 languages, slim health, GIS map |
| Readonly HUD + API | $0 | $1,400 / month | HUD plus readonly `/api/v1/*` at 60 req/min, fleet archive |
| Readonly HUD + API + LLM daily brief | $0 | $2,200 / month | The API tariff, plus the daily brief only after activation |

The LLM daily brief is **not a current feature**. It turns on in the $2,200 tariff after activation (a valid key and a working path). This offer sells the GIS map, the fleet archive, and the readiness score.

The brief, once activated, is not translated. Vessel names, MMSI, IMO, and provider names stay as recorded.

## Service levels the pilot can check

These figures are the system's own gates. They are not a separate promise.

- AIS lag at or below **300** seconds (`services.dual_gate.PIPELINE_LIVE_LAG_SEC`). Above that, `pipeline_health_status` leaves NOMINAL and publish stops.
- Public health stays under **1.0** seconds, including a burst of eight overlapping requests. The node serves the last document at once and rebuilds it in the background.
- HUD quiet p95 under **1.5** seconds. The page is gzip-compressed when the client asks.
- Compressor-station registry quiet p95 under **0.3** seconds. The 185 stations are a static in-memory catalog.
- Archive completeness **100%** of the registry day: every vessel has a `vessel_daily_archive` row. A missing position is stored as `source=none`. The archive does not drop the row and does not invent a position.

Disk headroom follows the same gate: below 20% is DEGRADED, below 10% is CRITICAL.

## What the pilot does not include

VesselFinder commercial REST and satellite AIS stay off until their keys are installed. The readiness score does not award those channels while `verified_n` is zero. The LLM daily brief stays off until activation.
