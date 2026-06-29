"""
storelink_client.py
-------------------
The StoreLink HTTP boundary, as an interface plus an in-memory fake.

Why this shape
==============
* :class:`StoreLinkClient` is the interface the rest of the server depends on. It mirrors
  the 8 StoreLink endpoints (NOT the 5 MCP tools -- the tool surface is deliberately
  narrower and lives in ``server.py``).
* :class:`BaseStoreLinkClient` implements the transport-agnostic concerns once:
  per-store authentication, the 401 -> reload-once -> retry-once rotation dance, and
  pushing per-call detail into the debug trace. Subclasses only implement ``_request``.
* :class:`InMemoryStoreLinkClient` is the fake used for the demo. Swapping in a real
  ``httpx`` client is just another subclass of :class:`BaseStoreLinkClient` implementing
  ``_request`` against the live endpoint -- no other code changes. (See README "Transport
  swap".)

Auth model
==========
Store-scoped endpoints (inventory, POS, replenishment) require the per-store
``X-Korral-Store-Key``. Discovery/catalog endpoints (``/stores``, ``/skus``,
``/suppliers``) are treated as open in this fake -- they carry no store-specific data --
so ``list_stores`` and ``get_sku`` work without a key.
"""
from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from clock import Clock, SystemClock
from observability import current_trace
from secrets_loader import KeyRecord, KorralError, MissingStoreCredentialError

if TYPE_CHECKING:  # import only for type hints -- avoids any runtime import cycle
    from secrets_loader import KeyProvider


# --------------------------------------------------------------------------- #
# StoreLink-specific errors (extend the shared KorralError base)
# --------------------------------------------------------------------------- #
class StoreLinkError(KorralError):
    """Base for anything that goes wrong talking to StoreLink."""


class StoreKeyRotatedError(StoreLinkError):
    """Upstream returned 401: the store's key appears invalid or was rotated out.

    Tells the operator how to fix it. NOTE (MVP): the automatic reload-and-retry recovery
    is a deferred fast-follow; for now we surface this clear error on the first 401.
    """

    def __init__(self, store_id: Union[int, str], source_hint: str) -> None:
        self.store_id = store_id
        super().__init__(
            f"The StoreLink key for store {store_id} appears invalid or was rotated. "
            f"Update it in {source_hint}; the server reloads keys within the TTL window."
        )


class UpstreamStoreLinkError(StoreLinkError):
    """A non-auth upstream failure (4xx/5xx other than 401)."""

    def __init__(self, status: int, endpoint: str, message: Optional[str] = None) -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(message or f"StoreLink returned HTTP {status} for {endpoint}.")


class StoreLinkNotFoundError(UpstreamStoreLinkError):
    """A 404 from StoreLink (unknown store / sku / order)."""


class ReplenishmentValidationError(KorralError):
    """A replenishment request violated a business guardrail (reason/quantity/limit)."""


# --------------------------------------------------------------------------- #
# Transport value object
# --------------------------------------------------------------------------- #
@dataclass
class UpstreamResponse:
    status: int
    body: Any
    latency_ms: float


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
class StoreLinkClient:
    """Interface mirroring the StoreLink endpoints. Implemented by the fake and, later,
    by a real httpx client."""

    def list_stores(self) -> List[dict]:
        raise NotImplementedError

    def get_store(self, store_id: Union[int, str]) -> dict:
        raise NotImplementedError

    def get_inventory(self, store_id: Union[int, str], sku: str) -> dict:
        raise NotImplementedError

    def get_pos(self, store_id: Union[int, str], sku: str, since: datetime) -> dict:
        raise NotImplementedError

    def create_replenishment(self, store_id: Union[int, str], payload: dict) -> dict:
        raise NotImplementedError

    def get_replenishment(self, store_id: Union[int, str], order_id: str) -> dict:
        raise NotImplementedError

    def get_sku(self, sku: str) -> dict:
        raise NotImplementedError

    def get_supplier(self, supplier_id: str) -> dict:
        raise NotImplementedError

    def ensure_store_credential(self, store_id: Union[int, str]) -> KeyRecord:
        """Resolve (but do not use) the store key, raising
        :class:`MissingStoreCredentialError` if absent. Lets tools fail fast before any
        upstream call."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Transport-agnostic base: auth, rotation, tracing
# --------------------------------------------------------------------------- #
class BaseStoreLinkClient(StoreLinkClient):
    """Implements authentication, the rotation retry, and debug tracing once. A concrete
    transport only needs to implement :meth:`_request`."""

    def __init__(self, key_provider: "KeyProvider") -> None:
        self._keys = key_provider

    # -- the single method a transport must implement -----------------------
    @abstractmethod
    def _request(
        self,
        method: str,
        path: str,
        key: Optional[str] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> UpstreamResponse:
        """Perform one HTTP request. ``key`` is ``None`` for open endpoints; otherwise it
        is sent as ``X-Korral-Store-Key``. Returns status + body + latency."""
        raise NotImplementedError

    # -- shared orchestration ----------------------------------------------
    def ensure_store_credential(self, store_id: Union[int, str]) -> KeyRecord:
        record = self._keys.get_key(store_id)  # raises MissingStoreCredentialError
        trace = current_trace()
        if trace is not None:
            trace.set_key(record.fingerprint, record.rotated_at)
        return record

    def _trace(self, method: str, path: str, resp: UpstreamResponse, retries: int) -> None:
        trace = current_trace()
        if trace is not None:
            trace.add_upstream(method, path, resp.status, resp.latency_ms, retries)

    def _open_call(self, method: str, path: str, params: Optional[dict] = None) -> Any:
        """Call a discovery/catalog endpoint (no per-store auth)."""
        resp = self._request(method, path, key=None, params=params)
        self._trace(method, path, resp, retries=0)
        self._raise_for_status(resp, path)
        return resp.body

    def _authed_call(
        self,
        store_id: Union[int, str],
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        """Call a store-scoped endpoint with the per-store key.

        MVP behaviour: on a 401 we surface a clear :class:`StoreKeyRotatedError`
        immediately. The automatic *reload-once-and-retry-once* recovery is a deliberate
        FAST-FOLLOW -- the hooks are already in place (``self._keys.reload()`` exists), so
        adding it later is a few lines right here:

            if resp.status == 401:
                self._keys.reload()                       # pick up a freshly rotated key
                record = self._keys.get_key(store_id)
                resp = self._request(method, path, key=record.key, params=params,
                                     json_body=json_body)
                self._trace(method, path, resp, retries=1)
                if resp.status == 401:
                    raise StoreKeyRotatedError(store_id, self._keys.source_hint())

        Keeping it out of the first slice keeps the request path trivial to reason about.
        """
        record = self.ensure_store_credential(store_id)  # fail fast if missing
        resp = self._request(method, path, key=record.key, params=params, json_body=json_body)
        self._trace(method, path, resp, retries=0)
        if resp.status == 401:
            raise StoreKeyRotatedError(store_id, self._keys.source_hint())
        self._raise_for_status(resp, path)
        return resp.body

    @staticmethod
    def _raise_for_status(resp: UpstreamResponse, path: str) -> None:
        if resp.status == 404:
            raise StoreLinkNotFoundError(resp.status, path, f"StoreLink resource not found: {path}")
        if resp.status >= 400:
            raise UpstreamStoreLinkError(resp.status, path)

    # -- endpoint methods (map interface -> paths) -------------------------
    def list_stores(self) -> List[dict]:
        return self._open_call("GET", "/v1/stores")

    def get_store(self, store_id: Union[int, str]) -> dict:
        return self._open_call("GET", f"/v1/stores/{store_id}")

    def get_inventory(self, store_id: Union[int, str], sku: str) -> dict:
        return self._authed_call(
            store_id, "GET", f"/v1/stores/{store_id}/inventory", params={"sku": sku}
        )

    def get_pos(self, store_id: Union[int, str], sku: str, since: datetime) -> dict:
        return self._authed_call(
            store_id,
            "GET",
            f"/v1/stores/{store_id}/pos",
            params={"sku": sku, "since": since.isoformat()},
        )

    def create_replenishment(self, store_id: Union[int, str], payload: dict) -> dict:
        return self._authed_call(
            store_id, "POST", f"/v1/stores/{store_id}/replenishment", json_body=payload
        )

    def get_replenishment(self, store_id: Union[int, str], order_id: str) -> dict:
        return self._authed_call(
            store_id, "GET", f"/v1/stores/{store_id}/replenishment/{order_id}"
        )

    def get_sku(self, sku: str) -> dict:
        return self._open_call("GET", f"/v1/skus/{sku}")

    def get_supplier(self, supplier_id: str) -> dict:
        return self._open_call("GET", f"/v1/suppliers/{supplier_id}")


# --------------------------------------------------------------------------- #
# In-memory fake (the demo backend) + seed data
# --------------------------------------------------------------------------- #
class InMemoryStoreLinkClient(BaseStoreLinkClient):
    """In-memory StoreLink fake. The integration plumbing is not what is being tested,
    so this stands in for real HTTP. It is intentionally faithful about *behaviour*:
    auth is enforced, 401s happen on key mismatch, latency is reported, and POS scales
    with the requested window.
    """

    def __init__(
        self,
        key_provider: "KeyProvider",
        clock: Optional[Clock] = None,
        base_latency_ms: float = 11.0,
        order_seq_start: int = 1043,
    ) -> None:
        super().__init__(key_provider)
        self._clock = clock or SystemClock()
        self._base_latency_ms = base_latency_ms
        self._order_counter = order_seq_start
        self._idem: Dict[tuple, str] = {}        # (store_id, idempotency_key) -> order_id
        self._orders: Dict[str, dict] = {}       # order_id -> order
        self._seed()

    # -- seed data ----------------------------------------------------------
    def _seed(self) -> None:
        # The fake's notion of the *currently valid* upstream key per store. The CLIENT
        # side key comes from secrets/keys.json via the key provider. For the demo they
        # match for 47 and 102. Store 5 has a server-side key but we are deliberately NOT
        # given it (no entry in keys.json) -> MissingStoreCredentialError.
        self._server_keys: Dict[str, str] = {
            "47": "sk_live_47_4f1c9a2b7e",
            "102": "sk_live_102_a83bd14c6f",
            "5": "sk_live_5_0099aa11bbcc",
        }
        self._stores: Dict[int, dict] = {
            47: {"store_id": 47, "name": "Korral Praha-Smíchov", "region": "CZ-Praha"},
            102: {"store_id": 102, "name": "Korral Brno-Královo Pole", "region": "CZ-Brno"},
            5: {"store_id": 5, "name": "Korral Ostrava", "region": "CZ-Ostrava"},
        }
        self._skus: Dict[str, dict] = {
            "8847291": {
                "sku": "8847291",
                "name": "Madeta butter 250g",
                "category": "Dairy",
                "supplier_id": "SUP-MADETA",
            },
        }
        self._suppliers: Dict[str, dict] = {
            "SUP-MADETA": {
                "supplier_id": "SUP-MADETA",
                "name": "Madeta a.s.",
                "lead_time_days": 2,
            },
        }
        self._inventory: Dict[tuple, int] = {
            (47, "8847291"): 8,
            (102, "8847291"): 14,
        }
        # POS seeded as units sold in a standard 24h window; scaled for other windows.
        self._pos_24h: Dict[tuple, int] = {
            (47, "8847291"): 19,
            (102, "8847291"): 18,
        }
        # POS feed freshness: how far behind real-time each store's feed is (minutes).
        # A healthy feed lags only a few minutes; a stalled feed grows stale over time.
        self._pos_lag_minutes: Dict[tuple, float] = {
            (47, "8847291"): 7,
            (102, "8847291"): 12,
        }

    # -- demo helper: simulate Korral rotating the upstream key ------------
    def rotate_server_key(self, store_id: Union[int, str], new_key: Optional[str] = None) -> str:
        """Simulate Korral rotating a store's key upstream. If ``secrets/keys.json`` is
        NOT also updated, the next authed call surfaces :class:`StoreKeyRotatedError`."""
        new = new_key or f"sk_live_{store_id}_rotated_{int(time.monotonic())}"
        self._server_keys[str(store_id)] = new
        return new

    def set_pos_lag_minutes(self, store_id: Union[int, str], sku: str, minutes: float) -> None:
        """Demo/test hook: push a store's POS feed further behind real-time so the
        stock-position tool flags it as stale."""
        self._pos_lag_minutes[(int(store_id), sku)] = minutes

    # -- transport ----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        key: Optional[str] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> UpstreamResponse:
        started = time.monotonic()
        status, body = self._route(method, path, key, params or {}, json_body or {})
        # Reported latency = a realistic fixed base + the (tiny) real routing time.
        latency_ms = self._base_latency_ms + (time.monotonic() - started) * 1000.0
        return UpstreamResponse(status=status, body=body, latency_ms=latency_ms)

    def _route(self, method, path, key, params, json_body):
        parts = [p for p in path.split("/") if p]  # e.g. ['v1','stores','47','inventory']

        # ---- open (discovery / catalog) endpoints ----
        if path == "/v1/stores" and method == "GET":
            return 200, [
                {"store_id": s["store_id"], "name": s["name"], "region": s["region"]}
                for s in self._stores.values()
            ]
        if len(parts) == 3 and parts[1] == "stores" and method == "GET":
            store = self._stores.get(_to_int(parts[2]))
            return (200, store) if store else (404, None)
        if len(parts) == 3 and parts[1] == "skus" and method == "GET":
            rec = self._skus.get(parts[2])
            return (200, rec) if rec else (404, None)
        if len(parts) == 3 and parts[1] == "suppliers" and method == "GET":
            rec = self._suppliers.get(parts[2])
            return (200, rec) if rec else (404, None)

        # ---- store-scoped (authenticated) endpoints ----
        if len(parts) >= 4 and parts[1] == "stores":
            sid = _to_int(parts[2])
            resource = parts[3]

            auth_fail = self._check_key(sid, key)
            if auth_fail is not None:
                return auth_fail  # (401, None)

            if resource == "inventory" and method == "GET":
                sku = params.get("sku")
                on_hand = self._inventory.get((sid, sku))
                if on_hand is None:
                    return 404, None
                return 200, {"store_id": sid, "sku": sku, "on_hand_units": on_hand}

            if resource == "pos" and method == "GET":
                sku = params.get("sku")
                base = self._pos_24h.get((sid, sku))
                if base is None:
                    return 404, None
                hours = self._window_hours(params.get("since"))
                units = int(round(base / 24.0 * hours))
                lag = self._pos_lag_minutes.get((sid, sku), 0)
                as_of = (self._clock.now() - timedelta(minutes=lag)).isoformat()
                return 200, {
                    "store_id": sid,
                    "sku": sku,
                    "units_sold": units,
                    "window_hours": round(hours, 2),
                    "as_of": as_of,  # when this POS data was last refreshed upstream
                }

            if resource == "replenishment" and method == "POST":
                return self._create_order(sid, json_body)

            if resource == "replenishment" and method == "GET" and len(parts) == 5:
                order = self._orders.get(parts[4])
                if not order or order["store_id"] != sid:
                    return 404, None
                return 200, dict(order)

        return 404, None

    def _check_key(self, store_id, key):
        valid = self._server_keys.get(str(store_id))
        if key is None or valid is None or key != valid:
            return 401, None
        return None

    def _window_hours(self, since) -> float:
        if since is None:
            return 24.0
        if isinstance(since, str):
            since = datetime.fromisoformat(since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return max((self._clock.now() - since).total_seconds() / 3600.0, 0.0001)

    def _create_order(self, store_id, payload):
        idem = payload.get("idempotency_key")
        if idem:
            existing = self._idem.get((store_id, idem))
            if existing:
                # Same idempotency key -> same order, no duplicate write.
                replay = dict(self._orders[existing])
                replay["idempotent_replay"] = True
                return 200, replay

        order_id = f"R-{self._order_counter}"
        self._order_counter += 1
        order = {
            "order_id": order_id,
            "store_id": store_id,
            "sku": payload["sku"],
            "quantity": payload["quantity"],
            "reason": payload["reason"],
            "status": "submitted",
            "created_at": payload.get("created_at"),
            "idempotent_replay": False,
        }
        self._orders[order_id] = order
        if idem:
            self._idem[(store_id, idem)] = order_id
        return 201, dict(order)


def _to_int(value: str) -> int:
    return int(value)
