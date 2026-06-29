"""
Unit tests for the Korral StoreLink MCP server (service layer + supporting modules).

These drive KorralService directly (no MCP protocol), with isolated temp paths so they
never touch the real ./secrets/keys.json or ./audit.log.

Run:  python -m pytest -q
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from clock import FrozenClock
from observability import AuditLogger, DebugLogger, redact_args
from secrets_loader import (
    FileKeyProvider,
    MissingStoreCredentialError,
    key_fingerprint,
)
from server import KorralService
from storelink_client import (
    InMemoryStoreLinkClient,
    ReplenishmentValidationError,
    StoreKeyRotatedError,
    StoreLinkNotFoundError,
)

SKU = "8847291"

# The fake's seeded server-side keys must match the client keys file for 47 and 102.
KEYS_47 = "sk_live_47_4f1c9a2b7e"
KEYS_102 = "sk_live_102_a83bd14c6f"


@pytest.fixture()
def env(tmp_path):
    """Build an isolated service: temp keys file, temp audit log, in-memory debug stream."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "rotated_at": "2026-06-23T00:00:00Z",
                "keys": {"47": KEYS_47, "102": KEYS_102},
            }
        ),
        encoding="utf-8",
    )
    audit_path = tmp_path / "audit.log"
    debug_stream = io.StringIO()
    # Frozen, shared clock -> time math is exact and deterministic (no rounding luck).
    clock = FrozenClock(datetime(2026, 6, 29, 14, 3, tzinfo=timezone.utc))
    client = InMemoryStoreLinkClient(FileKeyProvider(str(keys_file)), clock=clock)
    service = KorralService(
        client=client,
        debug_logger=DebugLogger(stream=debug_stream),
        audit_logger=AuditLogger(str(audit_path)),
        clock=clock,
    )
    return {
        "service": service,
        "client": client,
        "debug": debug_stream,
        "audit_path": audit_path,
        "clock": clock,
    }


def debug_records(stream: io.StringIO):
    return [json.loads(line) for line in stream.getvalue().strip().splitlines() if line]


# --------------------------------------------------------------------------- #
# Discovery + SKU folding
# --------------------------------------------------------------------------- #
def test_list_stores(env):
    stores = env["service"].list_stores()
    ids = {s["store_id"] for s in stores}
    assert ids == {47, 102, 5}
    assert all(set(s) == {"store_id", "name", "region"} for s in stores)


def test_get_sku_folds_supplier_lead_time(env):
    sku = env["service"].get_sku(SKU)
    assert sku == {
        "sku": "8847291",
        "name": "Madeta butter 250g",
        "category": "Dairy",
        "supplier_name": "Madeta a.s.",
        "lead_time_days": 2,
    }


# --------------------------------------------------------------------------- #
# The hero tool: shortfall, velocity, projection
# --------------------------------------------------------------------------- #
def test_stock_position_store_47_orders(env):
    pos = env["service"].get_stock_position(47, SKU)
    assert pos["on_hand_units"] == 8
    assert pos["pos"]["units_sold"] == 19
    assert pos["pos"]["window_hours"] == 24
    assert pos["shortfall_units"] == 11           # max(0, 19 - 8)
    assert pos["velocity_units_per_hour"] == round(19 / 24, 3)
    assert pos["projected_hours_to_stockout"] == 10.1  # 8 / (19/24)


def test_stock_position_store_102_no_shortfall_pressure(env):
    pos = env["service"].get_stock_position(102, SKU)
    assert pos["on_hand_units"] == 14
    assert pos["pos"]["units_sold"] == 18
    assert pos["shortfall_units"] == 4            # max(0, 18 - 14); below a 6-unit gap


def test_stock_position_window_scaling(env):
    # POS scales with the requested window: 48h ~= double the 24h sell-through.
    pos = env["service"].get_stock_position(47, SKU, window_hours=48)
    assert pos["pos"]["window_hours"] == 48
    assert pos["pos"]["units_sold"] == 38         # round(19/24 * 48)
    assert pos["shortfall_units"] == 30           # max(0, 38 - 8)


def test_pos_freshness_fields_fresh(env):
    pos = env["service"].get_stock_position(47, SKU)
    assert pos["as_of"] is not None                  # assessment timestamp anchor
    assert pos["pos"]["as_of"] is not None           # when POS data was last refreshed
    assert pos["pos"]["age_minutes"] == 7.0          # seeded healthy lag for store 47
    assert pos["pos"]["stale"] is False


def test_pos_marked_stale_and_audited(env):
    env["client"].set_pos_lag_minutes(47, SKU, 200)  # > default 120-min threshold
    pos = env["service"].get_stock_position(47, SKU)
    assert pos["pos"]["age_minutes"] == 200.0
    assert pos["pos"]["stale"] is True
    assert "may be out of date" in env["audit_path"].read_text(encoding="utf-8")


def test_stock_position_does_not_decide_to_reorder(env):
    # The server returns the shortfall but never a reorder flag/threshold.
    pos = env["service"].get_stock_position(47, SKU)
    assert "shortfall_units" in pos
    assert not any(
        k in pos for k in ("should_reorder", "reorder", "threshold", "needs_order")
    )


# --------------------------------------------------------------------------- #
# Replenishment: guardrails, idempotency, dry run, read-back
# --------------------------------------------------------------------------- #
def test_raise_replenishment_happy_path(env):
    order = env["service"].raise_replenishment(47, SKU, 24, reason="restock")
    assert order["order_id"] == "R-1043"
    assert order["status"] == "submitted"
    assert order["quantity"] == 24
    assert order["idempotent_replay"] is False


def test_idempotency_dedupes_retries(env):
    svc = env["service"]
    first = svc.raise_replenishment(47, SKU, 24, reason="restock", idempotency_key="k1")
    second = svc.raise_replenishment(47, SKU, 24, reason="restock", idempotency_key="k1")
    assert first["order_id"] == second["order_id"] == "R-1043"
    assert second["idempotent_replay"] is True
    # A different key creates a distinct order (no accidental collapse).
    third = svc.raise_replenishment(47, SKU, 12, reason="restock", idempotency_key="k2")
    assert third["order_id"] == "R-1044"


def test_dry_run_does_not_write(env):
    svc = env["service"]
    preview = svc.raise_replenishment(47, SKU, 24, reason="restock", dry_run=True)
    assert preview["status"] == "dry_run"
    assert preview["order_id"] is None
    # Nothing was persisted: a real order would have been R-1043.
    with pytest.raises(StoreLinkNotFoundError):
        svc.get_replenishment_status(47, "R-1043")


def test_reason_required(env):
    with pytest.raises(ReplenishmentValidationError):
        env["service"].raise_replenishment(47, SKU, 10, reason="   ")


@pytest.mark.parametrize("qty", [0, -5])
def test_quantity_must_be_positive(env, qty):
    with pytest.raises(ReplenishmentValidationError):
        env["service"].raise_replenishment(47, SKU, qty, reason="restock")


def test_quantity_must_not_exceed_max(env):
    with pytest.raises(ReplenishmentValidationError):
        env["service"].raise_replenishment(47, SKU, 501, reason="restock")  # default max 500


def test_bool_quantity_rejected(env):
    # True is an int subclass; make sure it is not accepted as a quantity.
    with pytest.raises(ReplenishmentValidationError):
        env["service"].raise_replenishment(47, SKU, True, reason="restock")


def test_get_replenishment_status_readback(env):
    svc = env["service"]
    svc.raise_replenishment(47, SKU, 24, reason="restock")
    status = svc.get_replenishment_status(47, "R-1043")
    assert status["order_id"] == "R-1043"
    assert status["status"] == "submitted"
    assert status["quantity"] == 24


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #
def test_missing_credential_fails_fast(env):
    # Store 5 has no key -> error BEFORE any upstream call (no upstream entries logged).
    with pytest.raises(MissingStoreCredentialError) as exc:
        env["service"].get_stock_position(5, SKU)
    assert "store 5" in str(exc.value)
    rec = debug_records(env["debug"])[-1]
    assert rec["tool_name"] == "get_stock_position"
    assert rec["status"] == "error"
    assert rec["upstream_calls"] == []          # fail-fast: nothing was called upstream


def test_rotated_key_detected(env):
    svc = env["service"]
    svc.get_stock_position(47, SKU)              # works first
    env["client"].rotate_server_key(47)          # Korral rotates the key upstream
    with pytest.raises(StoreKeyRotatedError) as exc:
        svc.get_stock_position(47, SKU)
    assert "store 47" in str(exc.value)


# --------------------------------------------------------------------------- #
# Observability: audit content + debug never leaks the raw key
# --------------------------------------------------------------------------- #
def test_audit_log_written(env):
    svc = env["service"]
    svc.get_stock_position(47, SKU)
    svc.raise_replenishment(47, SKU, 24, reason="projected stockout before next delivery")
    text = env["audit_path"].read_text(encoding="utf-8")
    assert "Checked Madeta butter 250g at Korral Praha-Smíchov: 8 on hand, 19 sold" in text
    assert "Raised replenishment order R-1043 for 24 units of Madeta butter 250g" in text
    assert "Reason: projected stockout before next delivery." in text


def test_debug_never_logs_raw_key_but_logs_fingerprint(env):
    env["service"].get_stock_position(47, SKU)
    raw = env["debug"].getvalue()
    assert KEYS_47 not in raw                    # raw key NEVER appears
    assert key_fingerprint(KEYS_47) in raw       # fingerprint does


def test_debug_has_required_fields(env):
    env["service"].get_stock_position(47, SKU)
    rec = debug_records(env["debug"])[-1]
    for field in (
        "timestamp",
        "request_id",
        "tool_name",
        "args",
        "key_fingerprint",
        "key_rotated_at",
        "upstream_calls",
        "retries",
        "status",
    ):
        assert field in rec
    assert rec["key_fingerprint"] == key_fingerprint(KEYS_47)
    assert rec["key_rotated_at"] == "2026-06-23T00:00:00Z"


def test_redact_args_masks_secrets_keeps_idempotency_key():
    out = redact_args(
        {"store_id": 47, "api_key": "shh", "idempotency_key": "k1", "password": "p"}
    )
    assert out["store_id"] == 47
    assert out["idempotency_key"] == "k1"        # useful, not secret -> kept
    assert out["api_key"] == "***redacted***"
    assert out["password"] == "***redacted***"


def test_key_fingerprint_is_sha256_prefix():
    import hashlib

    fp = key_fingerprint("abc")
    assert fp == hashlib.sha256(b"abc").hexdigest()[:8]
    assert len(fp) == 8


# --------------------------------------------------------------------------- #
# TTL cache reload (the hook the deferred rotation retry will use)
# --------------------------------------------------------------------------- #
def test_file_key_provider_reload_picks_up_new_key(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"keys": {"47": "old"}}), encoding="utf-8")
    provider = FileKeyProvider(str(keys_file), ttl_seconds=300)
    assert provider.get_key(47).key == "old"
    # Update the file; with a long TTL the cache still serves the old value...
    keys_file.write_text(json.dumps({"keys": {"47": "new"}}), encoding="utf-8")
    assert provider.get_key(47).key == "old"
    # ...until reload() forces a refresh.
    provider.reload()
    assert provider.get_key(47).key == "new"
