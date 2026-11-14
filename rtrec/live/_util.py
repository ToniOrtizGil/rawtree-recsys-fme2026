"""Small shared helpers for the live updaters: timestamp parsing, bounded dedupe, decayed counters.

Why decayed counters: the stream never stops, so every statistic must forget at a known rate and
be updatable in O(1) per row. We store (value, t_last) per key and decay lazily on read/write, which
also makes out-of-order rows exact: an old row is simply added with its own decay factor.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Hashable
from datetime import datetime, timezone


def to_epoch(ts) -> float:
    """ISO-8601 (with or without 'Z' / offset) or a number -> float epoch seconds (UTC)."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class RecentKeys:
    """Bounded set of recently seen keys: answers 'did I already apply this row?'.

    Stores rows re-read by the store's cursor overlap. Bounded so memory stays flat all night."""

    def __init__(self, maxlen: int = 200_000):
        self.maxlen = maxlen
        self._set: set = set()
        self._order: deque = deque()

    def add(self, key: Hashable) -> bool:
        """True if the key is new (and records it), False if it is a duplicate."""
        if key in self._set:
            return False
        self._set.add(key)
        self._order.append(key)
        if len(self._order) > self.maxlen:
            self._set.discard(self._order.popleft())
        return True

    def __contains__(self, key) -> bool:
        return key in self._set

    def __len__(self) -> int:
        return len(self._set)


class DecayedCounter:
    """Exponentially decayed sums per key, with half-life `half_life_s`.

    value(t) = sum_i w_i * 2^(-(t - t_i)/half_life). The steady-state value of a key receiving
    `r` units per second is r * tau, with tau = half_life / ln 2: `rate()` divides by tau and by the
    warm-up factor (1 - e^{-elapsed/tau}) so rates are unbiased from the first minute."""

    def __init__(self, half_life_s: float):
        self.half_life_s = float(half_life_s)
        self.k = math.log(2) / self.half_life_s
        self.tau = 1.0 / self.k
        self.d: dict = {}
        self.t0: float | None = None   # first timestamp ever seen (warm-up correction)

    def add(self, key, w: float, t: float) -> None:
        if self.t0 is None or t < self.t0:
            self.t0 = t
        cur = self.d.get(key)
        if cur is None:
            self.d[key] = [w, t]
        elif t >= cur[1]:
            cur[0] = cur[0] * math.exp(-(t - cur[1]) * self.k) + w
            cur[1] = t
        else:  # out-of-order row: add it already decayed to the key's clock
            cur[0] += w * math.exp(-(cur[1] - t) * self.k)

    def value(self, key, now: float) -> float:
        cur = self.d.get(key)
        if cur is None:
            return 0.0
        dt = now - cur[1]
        return cur[0] * math.exp(-dt * self.k) if dt > 0 else cur[0]

    def warmup(self, now: float) -> float:
        if self.t0 is None:
            return 1.0
        el = max(now - self.t0, 1.0)
        return max(1.0 - math.exp(-el / self.tau), 1e-6)

    def rate(self, key, now: float) -> float:
        """Estimated units per second for `key` around `now`."""
        return self.value(key, now) / (self.tau * self.warmup(now))

    def items(self, now: float):
        for key, (v, t) in self.d.items():
            dt = now - t
            yield key, (v * math.exp(-dt * self.k) if dt > 0 else v)

    def prune(self, now: float, min_value: float = 1e-3) -> int:
        """Drop keys whose decayed value fell below `min_value` (keeps memory flat)."""
        dead = [k for k, v in self.items(now) if v < min_value]
        for k in dead:
            del self.d[k]
        return len(dead)
