# Ghost detector precision probe

- generated_at_utc: 2026-09-25T12:33:24Z
- bound_kn (unchanged): 21.0
- spoofed_count (health): None
- spoofed_imos_n: 0
- speed_samples_n: 0
- speed_gt_21kn_n: n/a
- share_speed_gt_21kn: n/a

## Interpretation

Laden VLCC / Q-Max physical ceiling is locked at 21 kn in
`services/sentinel_analytics.py` (`LNG_PHYSICAL_SPEED_KN`).
This report **does not change** the threshold — it measures how often
flagged vessels exceed 21 kn (false-positive candidates among laden hulls
would require cargo/draught context not available in terrestrial AIS alone).

Requested sample window: last 44 flags — available n=0.

## Note

If speed_samples_n=0, health snapshot lacks per-vessel speeds;
re-run after a release that embeds spoof detail, or extend detector
export in a follow-up sprint. Threshold remains 21 kn.
