"""
server.py
---------
Korral StoreLink MCP server. Exposes exactly FIVE tools that let a Duvo agent do a
category buyer's job. Runs over stdio for local/demo use.

Tool surface (deliberately narrower than the 8 StoreLink endpoints)
===================================================================
    list_stores()                 -> minimal discovery
    get_sku(sku)                  -> sku info + folded-in supplier name & lead time
    get_stock_position(...)       -> the hero tool (on-hand vs POS, stockout risk)
    raise_replenishment(...)      -> the ONLY mutation, with guardrails
    get_replenishment_status(...) -> order read-back

Why the surface is 5 and not 8 -- and why shortfall is computed here but the reorder
threshold is NOT -- is documented in README.md.

Architecture note
==================
All logic lives in :class:`KorralService` (plain, testable, no MCP). The FastMCP tools
are thin wrappers that call the service and translate typed errors into clean MCP
``ToolError`` messages, so tracebacks never reach the agent. The smoke test and unit
tests drive :class:`KorralService` directly.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from clock import Clock, SystemClock
from observability import AuditLogger, DebugLogger
from secrets_loader import KorralError, build_key_provider
from storelink_client import (
    InMemoryStoreLinkClient,
    ReplenishmentValidationError,
    StoreLinkClient,
)

# --------------------------------------------------------------------------- #
# Configuration (env-driven; safe defaults for the demo)
# --------------------------------------------------------------------------- #
MAX_REPLENISHMENT_QTY = int(os.environ.get("MAX_REPLENISHMENT_QTY", "500"))
AUDIT_LOG_PATH = os.environ.get("KORRAL_AUDIT_LOG", "./audit.log")
POS_STALE_AFTER_MINUTES = int(os.environ.get("KORRAL_POS_STALE_AFTER_MINUTES", "120"))


# --------------------------------------------------------------------------- #
# Service layer (pure logic; no MCP, fully testable)
# --------------------------------------------------------------------------- #
class KorralService:
    def __init__(
        self,
        client: StoreLinkClient,
        debug_logger: DebugLogger,
        audit_logger: AuditLogger,
        max_replenishment_qty: int = MAX_REPLENISHMENT_QTY,
        clock: Optional[Clock] = None,
        pos_stale_after_minutes: int = POS_STALE_AFTER_MINUTES,
    ) -> None:
        self.client = client
        self.debug = debug_logger
        self.audit = audit_logger
        self.max_qty = max_replenishment_qty
        self.clock = clock or SystemClock()
        self.pos_stale_after_minutes = pos_stale_after_minutes

    # -- 1. discovery -------------------------------------------------------
    def list_stores(self) -> list:
        with self.debug.tool_call("list_stores", {}):
            stores = self.client.list_stores()
            return [
                {"store_id": s["store_id"], "name": s["name"], "region": s["region"]}
                for s in stores
            ]

    # -- 2. sku + folded supplier lead time --------------------------------
    def get_sku(self, sku: str) -> dict:
        with self.debug.tool_call("get_sku", {"sku": sku}):
            rec = self.client.get_sku(sku)
            # Fold supplier lead time in here -- there is deliberately NO get_supplier tool.
            supplier = self.client.get_supplier(rec["supplier_id"])
            return {
                "sku": rec["sku"],
                "name": rec["name"],
                "category": rec["category"],
                "supplier_name": supplier["name"],
                "lead_time_days": supplier["lead_time_days"],
            }

    # -- 3. the hero tool ---------------------------------------------------
    def get_stock_position(self, store_id: int, sku: str, window_hours: int = 24) -> dict:
        with self.debug.tool_call(
            "get_stock_position",
            {"store_id": store_id, "sku": sku, "window_hours": window_hours},
        ):
            # Fail fast if this store has no key, BEFORE any StoreLink call.
            self.client.ensure_store_credential(store_id)

            now = self.clock.now()
            sku_rec = self.client.get_sku(sku)
            inv = self.client.get_inventory(store_id, sku)
            since = now - timedelta(hours=window_hours)
            pos = self.client.get_pos(store_id, sku, since)
            store = self.client.get_store(store_id)

            on_hand = inv["on_hand_units"]
            units_sold = pos["units_sold"]
            velocity = (units_sold / window_hours) if window_hours > 0 else 0.0
            projected = (on_hand / velocity) if velocity > 0 else None
            # shortfall is computed SERVER-SIDE; the reorder THRESHOLD is not (see README).
            shortfall = max(0, units_sold - on_hand)

            # POS freshness: how old is the sell-through data we just used? An 11pm buyer
            # decision must know if the feed has stalled. as_of/age/stale are DATA, not a
            # decision -- the agent decides whether stale data is good enough to act on.
            pos_as_of = pos.get("as_of")
            pos_age_minutes: Optional[float] = None
            pos_stale = False
            if pos_as_of:
                age = (now - datetime.fromisoformat(pos_as_of)).total_seconds() / 60.0
                pos_age_minutes = round(age, 1)
                pos_stale = pos_age_minutes > self.pos_stale_after_minutes

            self.audit.stock_position_checked(
                store["name"], sku_rec["name"], on_hand, units_sold, window_hours, projected,
                pos_age_minutes=pos_age_minutes, pos_stale=pos_stale,
            )

            return {
                "store_id": store_id,
                "sku": sku,
                "name": sku_rec["name"],
                "on_hand_units": on_hand,
                "as_of": now.isoformat(),
                "pos": {
                    "units_sold": units_sold,
                    "window_hours": window_hours,
                    "as_of": pos_as_of,
                    "age_minutes": pos_age_minutes,
                    "stale": pos_stale,
                },
                "velocity_units_per_hour": round(velocity, 3),
                "projected_hours_to_stockout": (
                    round(projected, 1) if projected is not None else None
                ),
                "shortfall_units": shortfall,
            }

    # -- 4. the only mutation ----------------------------------------------
    def raise_replenishment(
        self,
        store_id: int,
        sku: str,
        quantity: int,
        reason: str,
        idempotency_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        args = {
            "store_id": store_id,
            "sku": sku,
            "quantity": quantity,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "dry_run": dry_run,
        }
        with self.debug.tool_call("raise_replenishment", args):
            # ---- business guardrails ----
            if not reason or not str(reason).strip():
                raise ReplenishmentValidationError(
                    "A non-empty 'reason' is required; it is written to the audit log."
                )
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise ReplenishmentValidationError(
                    f"'quantity' must be a positive integer (got {quantity!r})."
                )
            if quantity > self.max_qty:
                raise ReplenishmentValidationError(
                    f"'quantity' {quantity} exceeds MAX_REPLENISHMENT_QTY "
                    f"({self.max_qty}). Split the order or raise the limit deliberately."
                )

            # Fail fast on a missing credential, even for a dry run.
            self.client.ensure_store_credential(store_id)
            sku_rec = self.client.get_sku(sku)
            store = self.client.get_store(store_id)

            if dry_run:
                # Returns what WOULD happen without writing anything to StoreLink.
                self.audit.replenishment_dry_run(quantity, sku_rec["name"], store["name"], reason)
                return {
                    "order_id": None,
                    "status": "dry_run",
                    "store_id": store_id,
                    "sku": sku,
                    "quantity": quantity,
                    "reason": reason,
                    "dry_run": True,
                    "idempotent_replay": False,
                }

            payload = {
                "sku": sku,
                "quantity": quantity,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            order = self.client.create_replenishment(store_id, payload)

            if order.get("idempotent_replay"):
                self.audit.replenishment_replayed(order["order_id"], store["name"])
            else:
                self.audit.replenishment_raised(
                    order["order_id"], quantity, sku_rec["name"], store["name"], reason
                )

            return {
                "order_id": order["order_id"],
                "status": order["status"],
                "store_id": store_id,
                "sku": sku,
                "quantity": order["quantity"],
                "reason": order["reason"],
                "dry_run": False,
                "idempotent_replay": bool(order.get("idempotent_replay")),
            }

    # -- 5. order read-back -------------------------------------------------
    def get_replenishment_status(self, store_id: int, order_id: str) -> dict:
        with self.debug.tool_call(
            "get_replenishment_status", {"store_id": store_id, "order_id": order_id}
        ):
            order = self.client.get_replenishment(store_id, order_id)
            return {
                "order_id": order["order_id"],
                "store_id": order["store_id"],
                "sku": order["sku"],
                "quantity": order["quantity"],
                "status": order["status"],
                "reason": order["reason"],
                "created_at": order.get("created_at"),
            }


def build_service() -> KorralService:
    """Wire the default (demo) service: in-memory StoreLink fake, file-based keys,
    debug log to stderr, audit log to the configured path."""
    key_provider = build_key_provider()
    clock = SystemClock()  # ONE clock shared by service + client = drift-free time math
    client = InMemoryStoreLinkClient(key_provider, clock=clock)
    return KorralService(
        client=client,
        debug_logger=DebugLogger(),  # -> stderr (keeps stdout clean for MCP stdio)
        audit_logger=AuditLogger(AUDIT_LOG_PATH),
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# MCP app: thin tool wrappers over the service
# --------------------------------------------------------------------------- #
mcp = FastMCP("korral-storelink")
service = build_service()


@mcp.tool
def list_stores() -> list:
    """List the Korral stores this server can serve (minimal discovery).

    Returns a list of {store_id, name, region}.
    """
    try:
        return service.list_stores()
    except KorralError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def get_sku(sku: str) -> dict:
    """Look up a SKU, with the supplier's name and lead time folded in.

    Returns {sku, name, category, supplier_name, lead_time_days}.
    """
    try:
        return service.get_sku(sku)
    except KorralError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def get_stock_position(store_id: int, sku: str, window_hours: int = 24) -> dict:
    """Assess stockout risk for a SKU at a store: on-hand vs POS sell-through.

    Compares units on hand against units sold over the last ``window_hours`` and returns
    the velocity, projected hours to stockout, and the shortfall (computed server-side as
    max(0, units_sold_in_window - on_hand)).

    This tool deliberately does NOT decide whether to reorder: the reorder threshold is
    business/agent policy, not infrastructure. Use the returned shortfall + projection to
    apply your own policy.
    """
    try:
        return service.get_stock_position(store_id, sku, window_hours)
    except KorralError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def raise_replenishment(
    store_id: int,
    sku: str,
    quantity: int,
    reason: str,
    idempotency_key: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Raise a replenishment order (the only mutating tool).

    Guardrails: ``reason`` is required (it feeds the audit log); ``quantity`` must be > 0
    and <= MAX_REPLENISHMENT_QTY; ``idempotency_key`` dedupes retries (same key -> same
    order, no duplicate); ``dry_run=True`` returns what WOULD happen without writing.
    """
    try:
        return service.raise_replenishment(
            store_id, sku, quantity, reason, idempotency_key, dry_run
        )
    except KorralError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def get_replenishment_status(store_id: int, order_id: str) -> dict:
    """Read back the status of a replenishment order."""
    try:
        return service.get_replenishment_status(store_id, order_id)
    except KorralError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    # stdio transport for local/demo. To run over HTTP instead (prod agent + server are
    # NOT co-located), swap to: mcp.run(transport="http", host="0.0.0.0", port=8080)
    mcp.run()
