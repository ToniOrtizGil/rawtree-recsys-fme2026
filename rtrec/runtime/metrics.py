"""In-process counters and latency windows, exported to stdout, a status file and team.scratch.

Two latencies are tracked because they answer different questions:
  * `ack_s`  = our insert acknowledged - request ts, on our clock (what we control);
  * `fb_s`   = latency_ms from team.feedback, the organisers' own measure (the truth for scoring).
"""
from __future__ import annotations

import threading
from collections import Counter, deque


class Window:
    """Written by the insert/feedback threads, read by the status thread: every access takes the lock
    (iterating a deque while another thread appends raised `deque mutated during iteration` and killed
    the server once in the practice round)."""

    def __init__(self, maxlen: int = 20_000):
        self.xs: deque[tuple[float, float]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, t: float, v: float) -> None:
        with self._lock:
            self.xs.append((t, v))

    def pcts(self, since: float | None = None) -> dict:
        with self._lock:
            xs = list(self.xs)
        vals = sorted(v for t, v in xs if since is None or t >= since)
        if not vals:
            return {"n": 0}
        q = lambda p: round(vals[min(len(vals) - 1, int(p * len(vals)))], 3)  # noqa: E731
        return {"n": len(vals), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": round(vals[-1], 3)}


class Metrics:
    def __init__(self) -> None:
        self.c: Counter = Counter()
        self.ack_s = Window()
        self.fb_s = Window()
        self.compute_ms = Window()
        self.visible_lag_s = Window(2000)   # now - ts when we first see a request
        self.fb_reasons: Counter = Counter()
        self._lock = threading.Lock()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.c[key] += n

    def snapshot(self, now: float, window_s: float = 300.0) -> dict:
        since = now - window_s
        with self._lock:
            counters = dict(self.c)
        return {
            "counters": counters,
            "ack_latency_s": self.ack_s.pcts(since),
            "feedback_latency_s": self.fb_s.pcts(since),
            "compute_ms_per_batch": self.compute_ms.pcts(since),
            "visible_lag_s": self.visible_lag_s.pcts(since),
            "feedback_reasons": dict(self.fb_reasons),
        }
