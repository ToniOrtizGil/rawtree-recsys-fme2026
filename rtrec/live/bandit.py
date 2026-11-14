"""Thompson-sampling exploration on the last carousel slots, with logged propensities.

Why only the last slots: the first slots are looked at far more, so exploring there costs real
purchases; slots 9-10 are cheap to experiment with and still produce feedback on new or uncertain
products (the ones we know least about in the final round).

How: candidate pool = the base ranking just below the explored slots + new products related to the
session. For each candidate we draw a multiplier from its calibration posterior
Gamma(shape, rate) (rtrec.live.feedback; products never shown get their subcategory's prior with
high variance, so they get explored) and multiply by a relevance prior that decays with the base
rank. Highest draws fill the explored slots. The RNG is seeded by request_id, so a decision is
reproducible and the policy stays pure. The propensity of the chosen item (probability that the
sampler would have chosen it) is estimated by re-sampling and kept in `explorer.log`, so that
exploration data can be evaluated off-policy (IPS) later.
"""
from __future__ import annotations

import random
import zlib
from collections import deque
from dataclasses import dataclass, field

from rtrec.contract import State
from rtrec.schemas import Request


@dataclass
class ThompsonExplorer:
    slots: tuple[int, ...] = (8, 9)    # 0-based: slots 9 and 10
    pool_size: int = 12                # base-ranking items considered below the first explored slot
    n_new: int = 6                     # related new products added to the pool
    rank_decay: float = 0.85           # relevance prior: rank_decay ** (rank - first explored slot)
    new_prior: float = 0.6             # relevance prior of a related new product
    n_mc: int = 24                     # re-samples for the propensity estimate
    log: deque = field(default_factory=lambda: deque(maxlen=50_000))

    def apply(self, req: Request, ranked: list[str], state: State) -> list[str]:
        """Return `ranked` with the explored slots re-filled. Never shortens the list."""
        first = self.slots[0]
        if len(ranked) <= first:
            return ranked
        avail = state.available
        head = ranked[:first]
        taken = set(head)
        pool: list[tuple[str, float]] = []
        for i, p in enumerate(ranked[first:first + self.pool_size]):
            if p not in taken and p in avail:
                pool.append((p, self.rank_decay ** i))
                taken.add(p)
        for p in self._related_new(req, state):
            if len(pool) >= self.pool_size + self.n_new:
                break
            if p not in taken and p in avail:
                pool.append((p, self.new_prior))
                taken.add(p)
        if len(pool) < len(self.slots):
            return ranked
        rng = random.Random(zlib.crc32(str(req.get("request_id", "")).encode()))
        params = [(p, w, *self._posterior(p, state)) for p, w in pool]

        def draw():
            return sorted(((w * rng.gammavariate(a, 1.0 / b), p) for p, w, a, b in params), reverse=True)

        chosen = [p for _, p in draw()[:len(self.slots)]]
        # propensity: how often each chosen item lands in its slot under the same sampler
        hits = [0] * len(chosen)
        for _ in range(self.n_mc):
            d = [p for _, p in draw()[:len(chosen)]]
            for j, p in enumerate(chosen):
                hits[j] += d[j] == p
        rid = req.get("request_id", "")
        for j, p in enumerate(chosen):
            self.log.append((rid, self.slots[j], p, (hits[j] + 0.5) / (self.n_mc + 1.0)))
        out = head + chosen
        used = set(out)
        out += [p for p in ranked[first:] if p not in used]
        return out

    @staticmethod
    def _posterior(pid: str, state: State) -> tuple[float, float]:
        post = (state.live.get("calib_posterior") or {}).get(pid)
        if post is not None:
            return post
        sub = (state.catalog.get(pid) or {}).get("subcategory")
        m = (state.live.get("calib_subcat") or {}).get(sub, 1.0)
        return (2.0 * m, 2.0)            # same prior as feedback.FeedbackLearner (a_prod = 2)

    @staticmethod
    def _related_new(req: Request, state: State) -> list[str]:
        live = state.live
        new = state.new_products
        if not new:
            return []
        out: list[str] = []
        sim_new = live.get("similar_new") or {}
        subs = set()
        for v in req.get("recent_views") or []:
            p = v["product_id"]
            out.extend(sim_new.get(p, ()))
            subs.add((state.catalog.get(p) or {}).get("subcategory"))
        by_sub = live.get("trend_top_by_subcat") or {}
        for s in subs:
            out.extend(p for p in by_sub.get(s, ()) if p in new)
        out.extend(p for p in (live.get("trend_top") or [])[:100] if p in new)
        return out
