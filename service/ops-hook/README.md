# windy-search ops-hook — vendored from windy-contracts (ADR-060 §3.6)

`hook.py` is **vendored byte-identical** from the fleet-canonical ops-hook
in `sneakyfree/windy-contracts` (`ops-hook/hook.py`). Do not edit it here —
change the canon, re-run its tests there, then re-vendor. `test_ops_hook_drift.py`
in this repo fails if the two ever diverge (the ecosystem's vendor+drift-guard
pattern).

The hook is the host-side build for Search's mutating baseline knobs
(`restart_app`, `apply_update`, `set_setting`) — a stdlib-only systemd unit
running OUTSIDE the `windysearch-prod` compose project so a dead/wedged
`search-api` is fixable without SSH. All Search-specific facts (compose
invocation, image ref, allowlist, patient port) live in
`deploy/ops-hook.env.example`, verified against `SUBSTRATE.md`.

## Re-vendor

    cp ~/windy-contracts/ops-hook/hook.py service/ops-hook/hook.py
    # drift test then passes again

## Install (Grant-gated)

See the `.service` header. Verify `OPS_HOOK_IMAGE_REF` on the host first
(`docker compose -p windysearch-prod images`), mint `OPS_HOOK_TOKEN` to the
unit env + lockbox, add the Caddy `/hook/*` route, smoke `/hook/health` then
a quiet-minute confirm+restart. Then bind `restart_app`/`apply_update`/
`set_setting` in `contracts/ops.mcp.v1.json` and re-weave.
