# contracts/ — Windy Search's agent-OPS manifest

`ops.mcp.v1.json` is the **canonical source of truth** for Windy Search's
remote agent-ops surface, governed by the Agent Control Doctrine (**ADR-060**
in `sneakyfree/windy-contracts`). The Loom weaves an MCP server (stdio +
Streamable HTTP at `POST /mcp`) + conformance driver from it.

**`ops`, not `control`, on purpose (§2):** Search's product is its search API
(`/v1/search`, `/fetch`, `/extract`), which stays OUT of this ops surface.
This contract is health + agent-self-diagnostics (whoami, integrity budget).

- Remote agents attach over Streamable HTTP; the shim forwards the caller's
  **EPT** verbatim — Search's passport wall is the single authority.
- First pass = 5 implemented READ routes. The config/logs/redeploy gaps are
  the punch list (same shape as Mind's §7 'urgent').
- Change control: additive → `v1.1` via PR; breaking → new `v2` + tell Grant.
