"""Plug-in points for live learning (A4) and anything else that must see the stream.

A hook object may implement any subset of:

    on_start(state)                      once, after the catalogue is loaded, before serving
    on_catalog_updates(rows, state)      new shop.catalog_updates rows (already applied to state.catalog)
    on_events(rows, state)               new shop.events rows (deduplicated, ts order)
    on_feedback(rows, state)             new team.feedback rows ("__init__" removed)
    on_served(answers, state)            our own answers just queued: [(request, items, model_version)]
    on_tick(state, now)                  about once a second (decay, periodic refits, ...)

All hooks run on the serving thread *between* batches, so they never race with a policy and need no
locks. The price: a slow hook delays the next batch. Keep each call well under ~50 ms; rows are
delivered in chunks of at most `chunk` rows. Exceptions are caught, counted and logged; a hook that
keeps failing never stops serving.
"""
from __future__ import annotations

import logging
import time
from collections import Counter

log = logging.getLogger("rtrec.hooks")

HOOK_NAMES = ("on_start", "on_catalog_updates", "on_events", "on_feedback", "on_served", "on_tick")


class HookSet:
    def __init__(self, hooks: list | None = None, slow_ms: float = 50.0):
        self.hooks = list(hooks or [])
        self.slow_ms = slow_ms
        self.errors: Counter = Counter()
        self.slow: Counter = Counter()
        self.time_ms: Counter = Counter()

    def add(self, hook) -> None:
        self.hooks.append(hook)

    def call(self, name: str, *args) -> None:
        for h in self.hooks:
            fn = getattr(h, name, None)
            if fn is None:
                continue
            t0 = time.perf_counter()
            try:
                fn(*args)
            except Exception:  # never let learning code stop serving
                self.errors[f"{type(h).__name__}.{name}"] += 1
                if self.errors[f"{type(h).__name__}.{name}"] <= 5:
                    log.exception("hook %s.%s failed", type(h).__name__, name)
            dt = (time.perf_counter() - t0) * 1000
            self.time_ms[name] += dt
            if dt > self.slow_ms:
                self.slow[f"{type(h).__name__}.{name}"] += 1
