"""
observability.py
----------------
Two independent streams, two audiences.

A) :class:`DebugLogger` -- structured JSON, one line per tool call, to **stderr**.
   For an FDE debugging at 11pm. We log to STDERR (not stdout) because the stdio MCP
   transport carries the JSON-RPC protocol on stdout; writing logs there would corrupt
   the protocol. Over HTTP/SSE stdout is free, but stderr is always safe, so it is the
   default. The stream is injectable for tests.

B) :class:`AuditLogger` -- plain business sentences, append-only, to a file
   (default ``./audit.log``). For a Korral category buyer reading it the next morning.
   No jargon, no protocol noise.

The raw store key is NEVER written to either stream -- only the key fingerprint and the
rotation timestamp.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TextIO


# --------------------------------------------------------------------------- #
# Per-tool-call debug trace
# --------------------------------------------------------------------------- #
@dataclass
class UpstreamCall:
    """One StoreLink HTTP call made while serving a tool call."""

    method: str
    endpoint: str
    status: Any  # int HTTP status (or a string for transport-level failures)
    latency_ms: float
    retries: int = 0


@dataclass
class DebugTrace:
    """Mutable accumulator for one tool call. The client appends upstream calls to it
    via the current-trace context variable; the logger emits one line when it exits."""

    request_id: str
    tool_name: str
    args: Dict[str, Any]
    upstream_calls: List[UpstreamCall] = field(default_factory=list)
    key_fingerprint: Optional[str] = None
    key_rotated_at: Optional[str] = None
    _start: float = field(default_factory=time.monotonic)

    def add_upstream(
        self, method: str, endpoint: str, status: Any, latency_ms: float, retries: int = 0
    ) -> None:
        self.upstream_calls.append(
            UpstreamCall(method, endpoint, status, round(latency_ms, 2), retries)
        )

    def set_key(self, fingerprint: Optional[str], rotated_at: Optional[str]) -> None:
        self.key_fingerprint = fingerprint
        self.key_rotated_at = rotated_at


# Context variable so the StoreLink client can attach upstream-call detail to the
# in-flight tool call without the tool having to thread a trace object through every
# method signature. Keeps the StoreLinkClient interface clean.
_current_trace: contextvars.ContextVar[Optional[DebugTrace]] = contextvars.ContextVar(
    "korral_debug_trace", default=None
)


def current_trace() -> Optional[DebugTrace]:
    """Return the debug trace for the in-flight tool call, or ``None`` outside one."""
    return _current_trace.get()


# --------------------------------------------------------------------------- #
# Argument redaction
# --------------------------------------------------------------------------- #
_SECRET_HINTS = ("key", "secret", "token", "password", "authorization", "credential")
# Argument names that contain a hint substring but are NOT secrets and are useful in logs.
_SAFE_ARGS = ("idempotency_key",)


def redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Mask any argument whose name looks like a secret. Tool args never carry store
    keys (credentials are invisible to the agent), but this is belt-and-braces."""
    out: Dict[str, Any] = {}
    for k, v in args.items():
        lower = k.lower()
        if lower in _SAFE_ARGS:
            out[k] = v
        elif any(hint in lower for hint in _SECRET_HINTS):
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Debug logger
# --------------------------------------------------------------------------- #
class DebugLogger:
    """Emits one structured JSON line per tool call to ``stream`` (default stderr)."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def tool_call(self, tool_name: str, args: Dict[str, Any]):
        """Context manager wrapping a tool call. Yields a :class:`DebugTrace`.

        On exit it writes one JSON line. On exception it records the error + traceback,
        writes the line, and re-raises (so the tool layer can map it to a clean
        ``ToolError`` for the agent)."""
        trace = DebugTrace(
            request_id=str(uuid.uuid4()),
            tool_name=tool_name,
            args=redact_args(dict(args or {})),
        )
        token = _current_trace.set(trace)
        try:
            yield trace
        except BaseException as exc:  # noqa: BLE001 - we re-raise after logging
            self._emit(trace, error=exc)
            raise
        else:
            self._emit(trace, error=None)
        finally:
            _current_trace.reset(token)

    def _emit(self, trace: DebugTrace, error: Optional[BaseException]) -> None:
        total_latency_ms = round((time.monotonic() - trace._start) * 1000, 2)
        calls = trace.upstream_calls

        # Detailed per-call list plus convenience summary fields. A single line per tool
        # call, but get_stock_position fans out to several endpoints so we keep the list.
        if calls:
            upstream_endpoint: Any = (
                calls[-1].endpoint if len(calls) == 1 else [c.endpoint for c in calls]
            )
            upstream_latency_ms: Optional[float] = round(
                sum(c.latency_ms for c in calls), 2
            )
            # "ok" if every call returned < 400, else the first failing status.
            failing = [c.status for c in calls if isinstance(c.status, int) and c.status >= 400]
            upstream_status: Any = failing[0] if failing else "ok"
            retries = sum(c.retries for c in calls)
        else:
            # No upstream call at all -> we failed fast (e.g. missing credential).
            upstream_endpoint = None
            upstream_latency_ms = None
            upstream_status = None
            retries = 0

        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": trace.request_id,
            "tool_name": trace.tool_name,
            "args": trace.args,
            "key_fingerprint": trace.key_fingerprint,
            "key_rotated_at": trace.key_rotated_at,
            "upstream_endpoint": upstream_endpoint,
            "upstream_latency_ms": upstream_latency_ms,
            "upstream_status": upstream_status,
            "retries": retries,
            "upstream_calls": [vars(c) for c in calls],
            "tool_latency_ms": total_latency_ms,
            "status": "error" if error is not None else "ok",
        }
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }

        line = json.dumps(record, default=str)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


# --------------------------------------------------------------------------- #
# Audit logger
# --------------------------------------------------------------------------- #
class AuditLogger:
    """Append-only, human-readable business log for the category buyer.

    Timestamps are UTC, minute precision (matching the deliverable examples). Every
    mutation writes a line here; stock-position reads do too.
    """

    def __init__(self, path: str = "./audit.log") -> None:
        self._path = path
        self._lock = threading.Lock()

    # -- internal -----------------------------------------------------------
    def _write(self, sentence: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"{ts} — {sentence}"
        with self._lock:
            # Open per-write in append mode: truly append-only and safe if an operator
            # rotates/truncates the file underneath us.
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return line

    @staticmethod
    def _as_sentence(text: str) -> str:
        text = text.strip()
        if text and text[-1] not in ".!?":
            text += "."
        return text

    # -- business events ----------------------------------------------------
    @staticmethod
    def _age_phrase(minutes: float) -> str:
        if minutes >= 60:
            return f"~{round(minutes / 60.0, 1)}h"
        return f"~{round(minutes)}min"

    def stock_position_checked(
        self,
        store_name: str,
        sku_name: str,
        on_hand: int,
        units_sold: int,
        window_hours: int,
        projected_hours: Optional[float],
        pos_age_minutes: Optional[float] = None,
        pos_stale: bool = False,
    ) -> str:
        if projected_hours is None:
            outlook = "no recent sales, so no stockout is projected"
        else:
            outlook = f"projected to run out in ~{round(projected_hours)}h"
        sentence = (
            f"Checked {sku_name} at {store_name}: {on_hand} on hand, "
            f"{units_sold} sold in last {window_hours}h, {outlook}."
        )
        if pos_stale and pos_age_minutes is not None:
            # The buyer must know the figures came from a stalled feed.
            sentence += (
                f" Heads-up: these POS figures are {self._age_phrase(pos_age_minutes)} "
                f"old and may be out of date."
            )
        return self._write(sentence)

    def replenishment_raised(
        self,
        order_id: str,
        quantity: int,
        sku_name: str,
        store_name: str,
        reason: str,
    ) -> str:
        return self._write(
            f"Raised replenishment order {order_id} for {quantity} units of {sku_name} "
            f"at {store_name}. Reason: {self._as_sentence(reason)}"
        )

    def replenishment_dry_run(
        self, quantity: int, sku_name: str, store_name: str, reason: str
    ) -> str:
        return self._write(
            f"Dry run only (no order placed): would raise replenishment for {quantity} "
            f"units of {sku_name} at {store_name}. Reason: {self._as_sentence(reason)}"
        )

    def replenishment_replayed(self, order_id: str, store_name: str) -> str:
        return self._write(
            f"Replenishment request for {store_name} matched an existing order "
            f"({order_id}); no duplicate was created."
        )

    def note(self, sentence: str) -> str:
        """Free-form business note (e.g. an order that could not be placed)."""
        return self._write(self._as_sentence(sentence))
