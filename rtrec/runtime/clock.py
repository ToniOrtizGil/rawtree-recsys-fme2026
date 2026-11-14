"""Clocks and timestamp helpers shared by the runtime and the stores.

Why a clock object: the same serving code runs (a) in real time against RawTree or a real-time
FakeStore and (b) on a simulated clock in tests, where a whole hour of traffic replays in
milliseconds and every timing assertion is deterministic.

RawTree timestamps are ISO-8601 UTC strings ("2026-11-14T16:02:11.482Z"); depending on the
query they may come back as "2026-11-14 16:02:11.482". Inside the process we use float epoch seconds.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


class RealClock:
    """Wall clock. `sleep` really sleeps."""

    def now(self) -> float:
        return time.time()

    def sleep(self, dt: float) -> None:
        if dt > 0:
            time.sleep(dt)


class SimClock:
    """Manually advanced clock for deterministic tests. `sleep` advances time instantly."""

    def __init__(self, start: float = 1_763_110_800.0):  # 2025-11-14 09:00 UTC, any fixed value works
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> float:
        with self._lock:
            self._t += max(0.0, dt)
            return self._t

    def sleep(self, dt: float) -> None:
        self.advance(dt)


def to_iso(t: float) -> str:
    """Epoch seconds -> '2026-11-14T16:02:11.482Z' (millisecond precision, like RawTree)."""
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_sql_dt(t: float) -> str:
    """Epoch seconds -> 'YYYY-MM-DD hh:mm:ss.sss' (the literal ClickHouse's toDateTime64 parses)."""
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def parse_ts(x) -> float:
    """Tolerant parser: ISO with or without 'T'/'Z'/offset, or a number (epoch s or ms)."""
    if x is None or x == "":
        return 0.0
    if isinstance(x, (int, float)):
        v = float(x)
        return v / 1000.0 if v > 1e11 else v
    s = str(x).strip()
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
