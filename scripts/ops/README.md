# scripts/ops — operational / one-shot helpers

Transient lean-sync, bake-recover, and probe utilities used during Node A
hotfix / bake cycles. **Not** part of the production runtime path.

- Do not commit secrets, `.env`, or live probe dumps with credentials.
- Prefer `deploy_sentinel.py --bake-image` + `deploy/sentinel/deploy_korolev_sentinel.sh`
  for permanent delivery (image bake + `output_artifacts` seed).
