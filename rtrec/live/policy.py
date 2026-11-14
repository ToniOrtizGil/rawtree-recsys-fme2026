"""Policies built only from live state, and a wrapper that adds live learning to any base policy.

- `LiveSessionPolicy`: a model-free session heuristic (viewed items, live co-views, new look-alikes,
  subcategory trends, context popularity). Used as a cold-start specialist and as the reference
  policy to measure the live signals on the simulator before A3's ranker is plugged in.
- `LiveWrapper(base)`: takes any Policy (A3's ranker) and applies, in order:
    1. cold requests: merges `cold_candidates` into the base ranking (new products reachable);
    2. re-ranks the head by  rank prior x calibration^alpha x trend lift^beta x repeat penalty;
    3. Thompson exploration on the last slots (optional).
  Every step is switchable, so each one is A/B-tested on its own.

Both are pure: they only read State (the explorer appends to its own bounded log).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rtrec.contract import State
from rtrec.schemas import Request

from .bandit import ThompsonExplorer
from .coldstart import cold_candidates


def _bought(req: Request, state: State) -> set:
    prof = state.users.get(req.get("user_id") or "")
    return {p for _, p in prof.purchases} if prof is not None else set()


@dataclass
class LiveSessionPolicy:
    name: str = "live-session"
    k: int = 40

    def recommend(self, req: Request, state: State) -> list[str]:
        bought = _bought(req, state)
        return [p for p in cold_candidates(req, state, k=self.k + len(bought)) if p not in bought][:self.k]


@dataclass
class LiveWrapper:
    base: object
    name: str = ""
    cold_merge: bool = True
    rerank: bool = True
    explore: bool = False
    alpha: float = 1.0          # weight of the calibration multiplier
    beta: float = 0.5           # weight of the trend lift
    gamma: float = 1.0          # rank prior 1 / (rank + 1)^gamma
    repeat_penalty: float = 0.2
    depth: int = 40             # how much of the base ranking is re-ranked
    lift_cap: tuple = (0.5, 3.0)
    explorer: ThompsonExplorer = field(default_factory=ThompsonExplorer)

    def __post_init__(self):
        if not self.name:
            flags = "".join(c for c, on in (("c", self.cold_merge), ("r", self.rerank), ("x", self.explore)) if on)
            self.name = f"{getattr(self.base, 'name', 'base')}+live-{flags or 'off'}"

    def recommend(self, req: Request, state: State) -> list[str]:
        ranked = list(self.base.recommend(req, state) or [])[: self.depth]
        if self.cold_merge and state.is_cold(req):
            ranked = _interleave(ranked, cold_candidates(req, state, k=self.depth))[: self.depth]
        if self.rerank:
            ranked = self._rerank(req, ranked, state)
        if self.explore:
            ranked = self.explorer.apply(req, ranked, state)
        return ranked

    def _rerank(self, req: Request, ranked: list[str], state: State) -> list[str]:
        live = state.live
        calib = live.get("calib_product") or {}
        calib_sub = live.get("calib_subcat") or {}
        lift = live.get("trend_lift") or {}
        cat = state.catalog
        bought = _bought(req, state)
        lo, hi = self.lift_cap
        scored = []
        for r, p in enumerate(ranked):
            s = (r + 1.0) ** -self.gamma
            m = calib.get(p)
            if m is None:
                m = calib_sub.get((cat.get(p) or {}).get("subcategory"), 1.0)
            s *= m ** self.alpha
            s *= min(max(lift.get(p, 1.0), lo), hi) ** self.beta
            if p in bought:
                s *= self.repeat_penalty
            scored.append((s, -r, p))
        scored.sort(reverse=True)
        return [p for _, _, p in scored]


def _interleave(a: list[str], b: list[str]) -> list[str]:
    out, seen = [], set()
    for i in range(max(len(a), len(b))):
        for lst in (a, b):
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                out.append(lst[i])
    return out
