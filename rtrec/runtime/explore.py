"""Exploration for the practice round, and the log of what we served (training data for A3's ranker).

Why: A3's ranker only beats the hand-made v1 when it is trained on our own served stream *with
exploration* (otherwise it only ever sees v1's own top picks). So during the practice round:

  Explore(policy, eps=0.2): slots 4..10 are each, with probability eps, swapped for a random product
  from the policy's own top-40 (the same rule as A3's rtrec/offline/streamlog.explore). The random
  draw is seeded by request_id, so recommend() stays pure and every propensity is reproducible.
  Logged propensity of the item in slot k: (1-eps) if it is the policy's pick, eps/|pool| if swapped
  in; slots 1-3 are never explored (propensity 1).

  ServedLog (a runtime hook): on_served queues one compact row per answer, a background thread
  inserts them into team.scratch in batches (kind="served"), never on the serving thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import zlib
from collections import OrderedDict

import numpy as np

from rtrec.contract import Policy, State
from rtrec.schemas import Request

from .clock import to_iso

log = logging.getLogger("rtrec.explore")
FIRST_EXPLORED_SLOT = 3   # 0-based: slots 1-3 are never explored (they carry most of the conversion)
POOL = 40


class Explore:
    def __init__(self, policy: Policy, eps: float = 0.2, seed: int = 36, keep: int = 20_000):
        self.policy = policy
        self.eps = eps
        self.seed = seed
        self.name = f"{getattr(policy, 'name', 'policy')}+explore{int(round(eps * 100))}"
        self.meta: OrderedDict[str, dict] = OrderedDict()   # request_id -> {items, propensity, explored}
        self.keep = keep

    def recommend(self, req: Request, state: State) -> list[str]:
        ranked = list(self.policy.recommend(req, state) or [])
        avail = state.available
        ranked = [p for p in dict.fromkeys(ranked) if p in avail]   # what finalize would keep, in order
        out, prop, explored = ranked[:10], [1.0] * min(10, len(ranked)), [False] * min(10, len(ranked))
        pool = [p for p in ranked[:POOL] if p not in out]
        n_pool = len(pool)
        rng = np.random.default_rng([self.seed, zlib.crc32(req["request_id"].encode())])
        for k in range(len(out)):
            if k < FIRST_EXPLORED_SLOT:
                continue
            if pool and rng.random() < self.eps:
                out[k] = pool.pop(int(rng.integers(len(pool))))
                prop[k], explored[k] = self.eps / max(1, n_pool), True
            else:
                prop[k] = 1.0 - self.eps if n_pool else 1.0
        self.meta[req["request_id"]] = {"items": list(out), "propensity": prop, "explored": explored}
        while len(self.meta) > self.keep:
            self.meta.popitem(last=False)
        return out

    def info(self, request_id: str, final_items: list[str]) -> dict:
        """Propensities aligned with the items actually submitted (finalize may have padded)."""
        m = self.meta.get(request_id)
        if not m:
            return {"propensity": [None] * len(final_items), "explored": [False] * len(final_items)}
        by = {p: (pr, ex) for p, pr, ex in zip(m["items"], m["propensity"], m["explored"])}
        return {"propensity": [by.get(p, (None, False))[0] for p in final_items],
                "explored": [by.get(p, (None, False))[1] for p in final_items]}

    def version_for(self, req: Request) -> str:
        return self.name


class ServedLog:
    """Hook: every answer -> one kind="served" row in team.scratch (batched, background thread).

    Row: kind, ts (ISO, when served), request_id, request_ts, user_id, is_new_user, device, region, page,
    recent_views (JSON), items, propensity, explored, model_version. Join with team.feedback on
    request_id for clicked / purchased.
    """

    def __init__(self, store, get_policy=None, flush_s: float = 1.0, max_queue: int = 50_000):
        self.store = store
        self.get_policy = get_policy or (lambda: None)   # the live policy may be swapped at any time
        self.flush_s = flush_s
        self.max_queue = max_queue
        self._q: list[dict] = []
        self._lock = threading.Lock()
        self.written = self.errors = self.dropped = 0
        threading.Thread(target=self._loop, name="served-log", daemon=True).start()

    def on_served(self, answers, state: State) -> None:
        now = to_iso(time.time())
        rows = []
        for req, items, mv in answers:
            ctx = req.get("context") or {}
            pol = self.get_policy()
            info = pol.info(req["request_id"], items) if hasattr(pol, "info") else {}
            rows.append({"kind": "served", "ts": now, "request_id": req["request_id"], "request_ts": req.get("ts"),
                         "user_id": req.get("user_id"), "is_new_user": bool(req.get("is_new_user")),
                         "device": ctx.get("device"), "region": ctx.get("region"), "page": ctx.get("page"),
                         "recent_views": json.dumps(req.get("recent_views") or []), "items": list(items),
                         "propensity": info.get("propensity"), "explored": info.get("explored"),
                         "model_version": mv})
        with self._lock:
            self._q.extend(rows)
            if len(self._q) > self.max_queue:   # store down for a long time: keep the newest
                self.dropped += len(self._q) - self.max_queue
                self._q = self._q[-self.max_queue:]

    def _loop(self) -> None:
        while True:
            time.sleep(self.flush_s)
            with self._lock:
                batch, self._q = self._q[:2000], self._q[2000:]
            if not batch:
                continue
            try:
                self.store.insert_scratch(batch)
                self.written += len(batch)
            except Exception as e:  # noqa: BLE001 - put them back, try again next second
                self.errors += 1
                with self._lock:
                    self._q = batch + self._q
                if self.errors <= 3 or self.errors % 100 == 0:
                    log.warning("served log insert failed (%d): %s", self.errors, e)
