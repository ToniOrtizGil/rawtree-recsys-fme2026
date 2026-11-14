"""LiveLearner: one object holding every live updater; the hooks in rtrec.live delegate to it."""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from rtrec.contract import State

from ._util import RecentKeys, to_epoch
from .catalog import CatalogTracker
from .profiles import CoviewTracker, ProfileTracker
from .trends import TrendTracker


@dataclass
class ReqInfo:
    ts: float
    user_id: str
    is_new_user: bool
    cold: bool
    device: str
    region: str
    page: str


@dataclass
class LiveLearner:
    catalog: CatalogTracker = field(default_factory=CatalogTracker)
    trends: TrendTracker = field(default_factory=TrendTracker)
    profiles: ProfileTracker = field(default_factory=ProfileTracker)
    coview: CoviewTracker = field(default_factory=CoviewTracker)
    feedback: object | None = None          # FeedbackLearner, set in __post_init__ (avoids import cycle)
    seen_events: RecentKeys = field(default_factory=lambda: RecentKeys(500_000))
    requests: OrderedDict = field(default_factory=OrderedDict)   # request_id -> ReqInfo (bounded)
    versions: dict = field(default_factory=dict)                  # request_id -> model_version served
    max_requests: int = 100_000
    prune_every_s: float = 600.0
    _last_prune: float = 0.0
    stats: dict = field(default_factory=lambda: {"events": 0, "dup_events": 0, "updates": 0,
                                                 "feedback": 0, "requests": 0, "cpu_s": 0.0})

    def __post_init__(self):
        self._wrap()
        if self.feedback is None:
            from .feedback import FeedbackLearner
            self.feedback = FeedbackLearner()

    _HOOKS = ("init", "seed_history", "on_catalog_updates", "on_events", "on_requests", "on_feedback", "flush")

    def _wrap(self):
        # hooks may be called from several runtime threads: serialise them (readers never lock)
        self._lock = threading.RLock()
        for name in self._HOOKS:
            setattr(self, name, self._locked(getattr(type(self), name).__get__(self)))

    # pickling (A1's runtime snapshots state.live, which holds this object): drop lock + wrappers
    def __getstate__(self):
        with self._lock:
            d = {k: v for k, v in self.__dict__.items() if k != "_lock" and k not in self._HOOKS}
            return copy.deepcopy(d) if self._deep_copy_on_pickle else d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._wrap()

    _deep_copy_on_pickle = False

    def _locked(self, fn):
        def wrapper(*a, **kw):
            with self._lock:
                return fn(*a, **kw)
        wrapper.__name__ = fn.__name__
        return wrapper

    # ------------------------------------------------------------------ hooks
    def init(self, state: State, catalog_rows) -> None:
        self.catalog.init(state, catalog_rows)
        from .coldstart import refresh_new_product_neighbours
        refresh_new_product_neighbours(state)

    def seed_history(self, state: State, history_events) -> None:
        """Optional, at start-up: long-run subcategory/brand shares for sharper trend lifts."""
        self.trends.seed_baseline(history_events, state.catalog)

    def on_catalog_updates(self, rows, state: State) -> None:
        t0 = time.perf_counter()
        rows = list(rows)
        self.stats["updates"] += len(rows)
        if self.catalog.apply(rows, state):
            from .coldstart import refresh_new_product_neighbours
            refresh_new_product_neighbours(state, only=[str(r["product_id"]) for r in rows
                                                        if r.get("change") == "new_product"])
        state.updated_at = time.time()
        self.stats["cpu_s"] += time.perf_counter() - t0

    def on_events(self, rows, state: State) -> None:
        t0 = time.perf_counter()
        n = 0
        for ev in rows:
            t = to_epoch(ev["ts"])   # key on epoch: robust to ts formatting differences on re-read
            key = (t, ev.get("event_type"), ev.get("user_id"), ev.get("session_id"), ev.get("product_id"))
            if not self.seen_events.add(key):
                self.stats["dup_events"] += 1
                continue
            self.trends.add(ev, t, state)
            self.profiles.add(ev, t, state)
            self.coview.add(ev, t)
            n += 1
        self.stats["events"] += n
        if n:
            if self.trends.maybe_publish(state):
                self.coview.publish(state, self.trends.now)
            if self.trends.now - self._last_prune > self.prune_every_s:
                self._last_prune = self.trends.now
                self.trends.prune()
        state.updated_at = time.time()
        self.stats["cpu_s"] += time.perf_counter() - t0

    def on_requests(self, rows, state: State) -> None:
        for r in rows:
            rid = r.get("request_id")
            if not rid or rid in self.requests:
                continue
            ctx = r.get("context") or {}
            self.requests[rid] = ReqInfo(to_epoch(r["ts"]), r.get("user_id") or "",
                                         bool(r.get("is_new_user")), state.is_cold(r),
                                         ctx.get("device", ""), ctx.get("region", ""), ctx.get("page", ""))
            self.stats["requests"] += 1
            if len(self.requests) > self.max_requests:
                self.requests.popitem(last=False)

    def on_feedback(self, rows, state: State) -> None:
        t0 = time.perf_counter()
        self.stats["feedback"] += self.feedback.apply(rows, state, self)
        self.stats["cpu_s"] += time.perf_counter() - t0

    def flush(self, state: State) -> None:
        """Force every snapshot now (tests, end of a replay, before a state dump)."""
        self.trends.publish(state)
        self.coview.publish(state, self.trends.now)
        self.feedback.publish(state)
