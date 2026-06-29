"""
clock.py
--------
An injectable time source.

Time is the core variable of this domain -- stockout projection, POS sell-through windows,
and data freshness are all "now"-relative -- so the current time is a *dependency*, not a
hidden global. Inject :class:`SystemClock` in production and :class:`FrozenClock` in tests
for deterministic, drift-free time math.

Sharing ONE clock instance between the service and the StoreLink client also removes the
sub-millisecond drift you get from two independent ``datetime.now()`` calls, which is what
made the window-scaling arithmetic rely on rounding luck before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    """Interface for a time source."""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    """Wall-clock UTC time. The production default."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """A clock fixed at a given instant; ``advance()`` moves it forward. For tests."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, **timedelta_kwargs) -> None:
        """e.g. ``clock.advance(hours=3)`` or ``clock.advance(minutes=200)``."""
        self._instant = self._instant + timedelta(**timedelta_kwargs)
