"""Incremental readers with overlap + dedupe.

Why: RawTree rows can become visible slightly out of `ts` order (ingestion lag), so every store
reader re-reads a small overlap window behind the cursor. That returns some rows twice; this
class keeps the keys seen inside the window and passes only new rows on. Memory stays bounded:
keys older than the window are forgotten.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from .clock import parse_ts


class Cursor:
    def __init__(self, key: Callable[[dict], object], start: float = 0.0, window: float = 30.0):
        self.key = key
        self.position = float(start)     # max ts seen so far (epoch s)
        self.window = window             # how long to remember keys behind `position`
        self._seen: dict[object, float] = {}
        self._prune_at = 4096
        self._lock = threading.Lock()   # two hedged pollers may share one cursor

    def filter(self, rows: Iterable[dict]) -> list[dict]:
        """Rows not seen before, in input order; advances the position."""
        with self._lock:
            return self._filter(rows)

    def _filter(self, rows: Iterable[dict]) -> list[dict]:
        fresh = []
        for r in rows:
            k = self.key(r)
            if k in self._seen:
                continue
            t = parse_ts(r.get("ts"))
            self._seen[k] = t
            fresh.append(r)
            if t > self.position:
                self.position = t
        self._prune()
        return fresh

    def mark(self, key: object, t: float) -> None:
        """Remember a key without receiving it (e.g. answered before a restart)."""
        self._seen[key] = t

    def _prune(self) -> None:
        if len(self._seen) < self._prune_at:
            return
        cut = self.position - self.window
        self._seen = {k: t for k, t in self._seen.items() if t >= cut}
        self._prune_at = max(4096, 2 * len(self._seen))   # amortised O(1) per row

    def __contains__(self, key: object) -> bool:
        return key in self._seen


def event_key(e: dict):
    return (e.get("ts"), e.get("event_type"), e.get("user_id"), e.get("session_id"), e.get("product_id"))


def update_key(u: dict):
    return (u.get("ts"), u.get("product_id"), u.get("change"), u.get("price_eur"))


def request_key(r: dict):
    return r.get("request_id")
