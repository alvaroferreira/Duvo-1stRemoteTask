"""
smoke_test.py
-------------
Runs the Step 2 "Madeta butter" task end-to-end against the seed data WITHOUT an MCP
client, so correctness is verifiable in seconds:

    python smoke_test.py

It exercises the service layer exactly as the MCP tools do, and asserts the headline
outcomes:

    * Store 47  -> shortfall 11 -> agent policy says ORDER -> order R-1043 raised
    * Store 102 -> shortfall  4 -> agent policy says DO NOT ORDER
    * Store 5   -> no key       -> clean MissingStoreCredentialError (fail-fast)
    * Idempotency: a retried order with the same key returns the same order, no duplicate
    * Invalid/rotated key       -> clean StoreKeyRotatedError

IMPORTANT: the reorder THRESHOLD and the order QUANTITY below are AGENT / BUSINESS POLICY.
They live here, in the caller -- NOT in the MCP server. The server only reports the
shortfall and the projection; deciding what to do with them is the buyer's job.
"""
from __future__ import annotations

import io
import math

from clock import SystemClock
from observability import AuditLogger, DebugLogger
from secrets_loader import FileKeyProvider, MissingStoreCredentialError
from server import KorralService
from storelink_client import InMemoryStoreLinkClient, StoreKeyRotatedError

# --------------------------------------------------------------------------- #
# Agent / business policy (NOT part of the MCP server)
# --------------------------------------------------------------------------- #
REORDER_GAP_THRESHOLD = 6   # order only if the shortfall is at least this many units
CASE_PACK = 12              # supplier ships in 12-unit cases


def agent_should_reorder(position: dict) -> bool:
    return position["shortfall_units"] >= REORDER_GAP_THRESHOLD


def agent_order_quantity(position: dict) -> int:
    """Round the shortfall up to a full case pack, then add one case as buffer."""
    cases = math.ceil(position["shortfall_units"] / CASE_PACK)
    return (cases + 1) * CASE_PACK


SKU = "8847291"
AUDIT_PATH = "./audit.log"


def build_demo_service():
    """Same wiring as server.build_service(), but with the debug stream captured in
    memory so the smoke output stays readable."""
    debug_stream = io.StringIO()
    clock = SystemClock()  # one shared clock = drift-free time math
    service = KorralService(
        client=InMemoryStoreLinkClient(FileKeyProvider("./secrets/keys.json"), clock=clock),
        debug_logger=DebugLogger(stream=debug_stream),
        audit_logger=AuditLogger(AUDIT_PATH),
        clock=clock,
    )
    return service, debug_stream


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    # Start the audit log fresh so the demo output is clean.
    open(AUDIT_PATH, "w", encoding="utf-8").close()
    service, debug_stream = build_demo_service()

    hr("Discovery")
    stores = service.list_stores()
    for s in stores:
        print(f"  store {s['store_id']:>4}  {s['name']}  ({s['region']})")
    sku = service.get_sku(SKU)
    print(
        f"\n  SKU {sku['sku']} = {sku['name']} | {sku['category']} | "
        f"supplier {sku['supplier_name']} | lead time {sku['lead_time_days']}d"
    )
    assert sku["supplier_name"] == "Madeta a.s." and sku["lead_time_days"] == 2

    # ---- Store 47: should ORDER ----
    hr("Store 47 — Korral Praha-Smíchov")
    pos47 = service.get_stock_position(47, SKU)
    print(f"  {pos47}")
    assert pos47["on_hand_units"] == 8
    assert pos47["pos"]["units_sold"] == 19
    assert pos47["shortfall_units"] == 11
    assert pos47["projected_hours_to_stockout"] == 10.1
    decision47 = agent_should_reorder(pos47)
    print(f"  agent policy (shortfall {pos47['shortfall_units']} >= {REORDER_GAP_THRESHOLD}) -> ORDER = {decision47}")
    assert decision47 is True

    qty = agent_order_quantity(pos47)
    order = service.raise_replenishment(
        47, SKU, qty,
        reason="projected stockout before next delivery",
        idempotency_key="butter-47-2026-06-29",
    )
    print(f"  raised: {order}")
    assert order["order_id"] == "R-1043"
    assert order["quantity"] == 24 and order["status"] == "submitted"

    # ---- Idempotency: retry the SAME request -> same order, no duplicate ----
    retry = service.raise_replenishment(
        47, SKU, qty,
        reason="projected stockout before next delivery",
        idempotency_key="butter-47-2026-06-29",
    )
    print(f"  retry (same idempotency_key): order_id={retry['order_id']} replay={retry['idempotent_replay']}")
    assert retry["order_id"] == "R-1043" and retry["idempotent_replay"] is True

    status = service.get_replenishment_status(47, order["order_id"])
    print(f"  status read-back: {status['order_id']} -> {status['status']}, {status['quantity']} units")
    assert status["status"] == "submitted"

    # ---- Store 102: should NOT order ----
    hr("Store 102 — Korral Brno-Královo Pole")
    pos102 = service.get_stock_position(102, SKU)
    print(f"  {pos102}")
    assert pos102["on_hand_units"] == 14
    assert pos102["pos"]["units_sold"] == 18
    assert pos102["shortfall_units"] == 4
    decision102 = agent_should_reorder(pos102)
    print(f"  agent policy (shortfall {pos102['shortfall_units']} < {REORDER_GAP_THRESHOLD}) -> ORDER = {decision102}")
    assert decision102 is False

    # ---- POS data freshness: can the agent trust the numbers? ----
    hr("POS data freshness")
    print(
        f"  store 47 feed: pos.as_of={pos47['pos']['as_of']} "
        f"age_min={pos47['pos']['age_minutes']} stale={pos47['pos']['stale']}"
    )
    assert pos47["pos"]["stale"] is False
    # Simulate store 102's POS feed stalling (>120-min threshold) and re-check.
    service.client.set_pos_lag_minutes(102, SKU, 200)
    stale_pos = service.get_stock_position(102, SKU)
    print(
        f"  store 102 after feed stalls: age_min={stale_pos['pos']['age_minutes']} "
        f"stale={stale_pos['pos']['stale']} -> audit log gets a 'may be out of date' caveat"
    )
    assert stale_pos["pos"]["stale"] is True

    # ---- Store 5: missing credential, fail fast ----
    hr("Store 5 — Korral Ostrava (no key configured)")
    try:
        service.get_stock_position(5, SKU)
        raise AssertionError("expected MissingStoreCredentialError for store 5")
    except MissingStoreCredentialError as exc:
        print(f"  clean tool error -> {exc}")

    # ---- Invalid / rotated key (MVP: detected, retry is fast-follow) ----
    hr("Invalid / rotated key detection")
    service.client.rotate_server_key(47)  # simulate Korral rotating the key upstream
    try:
        service.get_stock_position(47, SKU)
        raise AssertionError("expected StoreKeyRotatedError after key rotation")
    except StoreKeyRotatedError as exc:
        print(f"  clean tool error -> {exc}")

    # ---- Show the two observability streams ----
    hr("Audit log (./audit.log) — what the category buyer reads")
    with open(AUDIT_PATH, "r", encoding="utf-8") as fh:
        print(fh.read().rstrip())

    hr("Debug log (stderr in production) — sample lines for the FDE")
    for line in debug_stream.getvalue().strip().splitlines()[:3]:
        print("  " + line)

    hr("RESULT")
    print("  ALL CHECKS PASSED ✅  (47 ordered, 102 did not, failure paths clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
