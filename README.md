# Korral StoreLink MCP Server

An MCP server that lets a Duvo agent do a Korral category buyer's job: check on-hand vs
POS sell-through, judge stockout risk, and raise replenishment orders — all against
Korral's homegrown **StoreLink** ordering/stock system. The server runs **inside Korral's
GCP tenancy**; no data leaves their tenancy.

> **Status: MVP slice.** This first slice is a complete, runnable, tested vertical: the 5
> tools, seed data, POS-freshness signalling, both observability streams, file-based secrets
> with missing-credential fail-fast, and a smoke test. A few production concerns are
> deliberately out of scope for this first version — see
> [Known gaps](#known-gaps-not-in-this-first-version) and the
> [Roadmap](#roadmap--what-to-improve-next-after-the-first-client-test) at the bottom.

---

## Quick start

```bash
# 1. create a venv and install pinned deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. run the Step 2 "Madeta butter" task end-to-end (no MCP client needed)
python smoke_test.py

# 3. run the unit tests
python -m pytest -q

# 4. launch the MCP server (stdio)
python server.py
```

The smoke test proves the core outcome against the seed data: **store 47 orders, store 102
does not**, and the failure paths (missing key, rotated key) return clean errors.

---

## Command-line client (`cli.py`)

A tiny terminal client for asking the server questions without a browser. It goes through
the real MCP tool layer, so you see exactly what an agent would get.

```bash
python cli.py                 # interactive REPL (in-memory server)
python cli.py --docker        # interactive REPL against the Docker container
python cli.py stores          # one-shot command, then exit
python cli.py stock 47 8847291
python cli.py order 47 8847291 24 projected stockout before next delivery
```

REPL commands: `stores`, `sku <sku>`, `stock <store> <sku> [hours]`,
`order <store> <sku> <qty> <reason...>`, `dry <store> <sku> <qty> <reason...>`,
`status <store> <order_id>`, `help`, `quit`.

Tip: `--docker` passes arguments to `docker` as a list, so a space in your project path is
fine here — unlike the browser MCP Inspector, which splits the command on spaces.

---

## Tool surface — and the decisions behind it

The agent sees **exactly 5 tools**, not the 8 StoreLink endpoints. The endpoints are an
implementation detail; the tools are the buyer's job, shaped so the agent can't do the
wrong thing.

| Tool | Purpose |
|------|---------|
| `list_stores()` | Minimal discovery: `{store_id, name, region}`. |
| `get_sku(sku)` | `{sku, name, category, supplier_name, lead_time_days}` — supplier lead time **folded in**. |
| `get_stock_position(store_id, sku, window_hours=24)` | The hero tool: on-hand vs POS, velocity, projected stockout, shortfall — plus an `as_of` assessment timestamp and POS-data freshness (`age_minutes`, `stale`). |
| `raise_replenishment(store_id, sku, quantity, reason, idempotency_key=None, dry_run=False)` | The **only** mutation, with guardrails. |
| `get_replenishment_status(store_id, order_id)` | Order read-back. |

### What is deliberately NOT exposed, and why

- **No `get_supplier` tool.** Suppliers are infrastructure, not a buyer's vocabulary. The
  one thing the buyer needs — *lead time* — is folded into `get_sku`. Exposing suppliers
  separately would invite the agent to wander the supplier graph instead of doing its job.
- **No raw POS transaction dump.** `get_stock_position` returns the *aggregate* the
  decision needs (`units_sold` in the window). A transaction-level firehose is a privacy
  and token-cost liability the agent never needs.
- **No store-key / credential tool.** Credentials are invisible to the agent. The server
  resolves keys from the secret store internally; the agent never sees, sets, or names a
  key. (The agent *does* get a clean, actionable error if a store's key is missing.)
- **8 endpoints → 5 tools.** `get_store`, `get_inventory`, `get_pos`, and `get_supplier`
  are composed *inside* the tools (`get_stock_position` alone calls four endpoints). Fewer,
  higher-level tools = fewer ways for the agent to assemble a wrong answer.

### Why `shortfall` is computed server-side but the reorder threshold is NOT

This is the core operational-judgment call.

- **`shortfall_units = max(0, units_sold_in_window − on_hand)` is computed server-side.**
  It is a *fact about the data*: a deterministic arithmetic relationship between two numbers
  the server already holds. Computing it once, server-side, means every caller gets the same
  number and the agent can't fumble the arithmetic. Same for `velocity_units_per_hour` and
  `projected_hours_to_stockout`.
- **The reorder threshold (e.g. "order if the gap ≥ 6 units") is NOT in the server.** That
  is *business policy*, and it changes by category, season, promotion, supplier reliability,
  and shelf life. Baking a 6-unit rule into the infrastructure would freeze a business
  decision into a place only an engineer can change. The server reports the shortfall and the
  projection; the **agent/business layer decides what to do with them**.

You can see this split in `smoke_test.py`: `REORDER_GAP_THRESHOLD = 6` and the order-quantity
rule live in the *caller* (the agent policy), never in `server.py`.

### POS data freshness — knowing when *not* to trust the feed

Time is the core variable of this domain, so `get_stock_position` is explicit about *when*
its answer was true. The response carries:

- A top-level **`as_of`** — an ISO-8601 UTC timestamp for when the assessment was made.
- Inside the **`pos`** object: **`as_of`** (when the POS data was last refreshed upstream),
  **`age_minutes`** (how old that data is), and **`stale`** (a bool, `true` when the age
  exceeds the staleness threshold).

The threshold is `KORRAL_POS_STALE_AFTER_MINUTES` (default **120**). Why this matters: a
buyer making a call at 11pm against a POS feed that silently stopped refreshing at 6pm is
reading a five-hour-old picture of sell-through and could under- or over-order on stale
numbers. Surfacing `stale` (and the age) lets the agent — and the buyer reading the audit
log — discount a stalled feed instead of trusting it blindly. When the feed is stale, the
audit line gets an appended caveat, e.g. *"Heads-up: these POS figures are ~5h old and may
be out of date."*

Under the hood, "now" is no longer a hidden global: a `clock.py` module provides an
injectable `Clock` (interface), `SystemClock` (production default) and `FrozenClock` (tests).
The service and the StoreLink client share **one** clock instance, which keeps the freshness
arithmetic deterministic and removes sub-millisecond drift in the window math.

### Replenishment guardrails (`raise_replenishment`)

- `reason` is **required** and non-empty — it feeds the audit log the buyer reads.
- `quantity` must be `> 0` and `<= MAX_REPLENISHMENT_QTY` (default **500**, env-configurable).
  `True`/`False` are rejected (a bool is not a quantity).
- `idempotency_key` **dedupes retries**: the same key returns the same order, never a
  duplicate. (Agents retry; orders must not double.)
- `dry_run=True` returns exactly what *would* happen — including validation and a
  missing-credential check — **without writing** anything to StoreLink.

---

## Architecture

```
server.py            FastMCP app + 5 thin tool wrappers -> KorralService (pure, testable)
storelink_client.py  StoreLinkClient interface + in-memory fake + seed data + typed errors
clock.py             injectable time source: Clock interface + SystemClock + FrozenClock
secrets_loader.py    per-store key loader (file backend + Secret Manager stub), TTL cache
observability.py     DebugLogger (JSON, stderr) + AuditLogger (business sentences, file)
smoke_test.py        the Step 2 task end-to-end, with asserts
tests/test_korral.py 23 unit tests
secrets/keys.json    demo keys for stores 47 and 102 (NOT 5) — demo only, never real secrets
```

All logic lives in `KorralService`; the MCP tools just call it and translate typed errors
into clean `ToolError` messages so **tracebacks never reach the agent**. Tests and the smoke
script drive the service directly.

### Transport note (local demo vs. production)

The demo uses **stdio** (`mcp.run()`), which is what Claude Desktop speaks. In production the
agent and the server are **not co-located**, so you run over HTTP/SSE instead — a one-line
swap in `server.py`:

```python
mcp.run(transport="http", host="0.0.0.0", port=8080)   # instead of mcp.run()
```

Crucially, the **StoreLink client is the same** in both cases. The `StoreLinkClient`
interface + `BaseStoreLinkClient` (which owns auth and tracing) mean swapping the in-memory
fake for a real `httpx` implementation is just one new subclass implementing `_request` —
no change to tools, logging, or secrets handling.

---

## Observability — two streams, two audiences

**A) Debug log** — structured JSON, one line per tool call, to **stderr**.
For an FDE debugging at 11pm. Each line carries `request_id`, `tool_name`, redacted args, the
upstream endpoint(s) called, `upstream_latency_ms`, `upstream_status`, `retries`, the
`key_fingerprint` (first 8 chars of a sha256) + `key_rotated_at`, and an error+traceback on
failure. **It goes to stderr on purpose:** the stdio MCP transport uses stdout for the
JSON-RPC protocol, so logging there would corrupt it. The **raw key is never logged** — only
its fingerprint.

**B) Audit log** — plain business sentences, append-only, to a file
(`KORRAL_AUDIT_LOG`, default `./audit.log`). For the category buyer the next morning:

```
2026-06-29 14:03 — Checked Madeta butter 250g at Korral Praha-Smíchov: 8 on hand, 19 sold in last 24h, projected to run out in ~10h.
2026-06-29 14:03 — Raised replenishment order R-1043 for 24 units of Madeta butter 250g at Korral Praha-Smíchov. Reason: projected stockout before next delivery.
```

Every mutation writes an audit line (what/why/when); stock-position reads do too.

---

## Secrets

Per-store keys (`X-Korral-Store-Key`, scoped per store, rotated weekly) are loaded at runtime
from a mounted JSON file (`KORRAL_KEYS_FILE`, default `./secrets/keys.json`) via the
`KeyProvider` interface. **Keys are never baked into the image.** A `SecretManagerKeyProvider`
stub shows the GCP drop-in behind the same interface. A 300s TTL cache (`reload()` available)
is the hook the deferred rotation-retry will use.

- **Missing credential** (store has no key) → `MissingStoreCredentialError`, raised
  **before any StoreLink call**, with instructions to add the key. (Try store 5 in the smoke
  test.)
- **Invalid/rotated key** (upstream 401) → `StoreKeyRotatedError` with a clear operator
  message. *(This first version: detected and surfaced; the automatic reload-and-retry
  recovery is a known gap — see below.)*

---

## Connect to Claude Desktop

Add this to your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS), then restart
Claude Desktop. Paths are absolute on purpose.

```json
{
  "mcpServers": {
    "korral-storelink": {
      "command": "/Users/alvaroferreira/Documents/= Projectos/Duvo.ai/1.AsyncTask-AlvaroFerreira/.venv/bin/python",
      "args": [
        "/Users/alvaroferreira/Documents/= Projectos/Duvo.ai/1.AsyncTask-AlvaroFerreira/server.py"
      ],
      "env": {
        "KORRAL_KEYS_FILE": "/Users/alvaroferreira/Documents/= Projectos/Duvo.ai/1.AsyncTask-AlvaroFerreira/secrets/keys.json",
        "KORRAL_AUDIT_LOG": "/Users/alvaroferreira/Documents/= Projectos/Duvo.ai/1.AsyncTask-AlvaroFerreira/audit.log",
        "MAX_REPLENISHMENT_QTY": "500"
      }
    }
  }
}
```

Then ask the agent, e.g.: *"Check Madeta butter (SKU 8847291) at store 47 and order if it'll
stock out before the next delivery."*

---

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `KORRAL_KEYS_FILE` | `./secrets/keys.json` | Path to the mounted key map. |
| `KORRAL_AUDIT_LOG` | `./audit.log` | Path to the business audit log. |
| `MAX_REPLENISHMENT_QTY` | `500` | Hard cap on a single replenishment quantity. |
| `KORRAL_POS_STALE_AFTER_MINUTES` | `120` | POS-data staleness threshold; older than this flips `pos.stale` to `true`. |
| `KORRAL_KEY_TTL_SECONDS` | `300` | Key cache TTL (rotation pickup window). |
| `KORRAL_SECRETS_BACKEND` | `file` | `file` or `gcp` (Secret Manager). |

---

## Known gaps (not in this first version)

This first version is an honest MVP slice. The structure for each of these is in place; the
behaviour is what is still missing. Nothing here is hidden — it is listed so the client knows
exactly what they are testing.

1. **401 reload-and-retry rotation recovery is not wired yet.** A 401 *is* detected and
   surfaced as a clean `StoreKeyRotatedError`, but the automatic reload-once-then-retry-once
   recovery is not in place: a freshly rotated key only recovers after the 300s TTL expires.
   The hooks exist — `KeyProvider.reload()` and a commented stub in
   `BaseStoreLinkClient._authed_call` — so this is a wiring job, not a redesign.
2. **GCP Secret Manager backend is a stub.** `SecretManagerKeyProvider` is documented but
   raises `NotImplementedError`. The file backend is fully functional.
3. **No MCP-protocol-level tests.** The 23 unit tests drive the service layer directly. The
   `@mcp.tool` wrappers and their `KorralError -> ToolError` translation — the actual
   agent-facing contract — are not yet exercised through the protocol.
4. **Unexpected (non-typed) errors rely on FastMCP's default masking.** There is no explicit
   catch-all that returns a clean *"internal error, see debug log request_id=X"* message; an
   unforeseen error is masked by FastMCP rather than shaped by us.
5. **No CI pipeline file yet.** There is no build -> test -> tag-by-git-SHA -> push-to-Artifact-Registry
   pipeline committed, although `DEPLOYMENT.md` describes the Duvo-owned CI it should become.
6. **Dockerfile and HTTP/SSE transport are written but not run end-to-end.** The Dockerfile
   exists but has not been `docker build`-verified, and the HTTP/SSE transport is documented
   as a one-line swap (see above) but has not been exercised end-to-end.
7. **Audit-log timestamps are UTC, not store-local.** A Czech buyer reading the log "the next
   morning" expects Europe/Prague local time; the timezone should become configurable.

---

## Roadmap — what to improve next (after the first client test)

The plan is deliberate: **validate this first version with Korral, then close these in
order.** Priorities reflect what blocks a wider rollout versus what waits on real usage.

| Priority | Item |
|----------|------|
| **P0** — close before wider rollout | **401 reload-and-retry recovery** — the one functional-spec item still absent (gap #1). |
| **P0** — close before wider rollout | **MCP-protocol-level tests** via FastMCP's in-memory `Client`, plus an explicit catch-all error handler so **no traceback can ever reach the agent** (gaps #3, #4). |
| **P1** | **GCP Secret Manager backend** (gap #2). |
| **P1** | **Store-local (Europe/Prague) configurable audit timestamps** (gap #7). |
| **P1** | **CI pipeline** — build, test, tag by git SHA, push to Korral's Artifact Registry, including a `docker build` verification step (gaps #5, #6). |
| **P2** — driven by client feedback | **Real `httpx` `StoreLinkClient`** — swap the in-memory fake for a live implementation against StoreLink's real endpoints (one new `_request` subclass; tools, logging and secrets are unchanged). |
| **P2** — driven by client feedback | **Confirm the 5 Day-1 integration questions** from `DEPLOYMENT.md`: where the agent runs vs the tenancy boundary; StoreLink's internal hostname / firewall; the Secret Manager rotation mechanism; Artifact Registry + deploy permissions; log retention / PII policy for the audit log. |
| **P2** — optional | **Delivery-schedule awareness** — compare projected stockout against the next *scheduled delivery*, not just supplier lead time (needs a delivery calendar from StoreLink). |
| **P2** — optional | **Advisory "suggested quantity"** as a *separate* tool if the buyer wants it — still policy, deliberately kept out of the core data tools. |
