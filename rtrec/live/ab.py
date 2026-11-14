"""A/B testing: deterministic split by hash(request_id), per-version results from feedback, promotion rule.

- Split: bucket = crc32(salt + request_id) mod 10,000; arms own contiguous bucket ranges by weight.
  Deterministic, so the feedback (which has no model_version) is attributed by recomputing the arm:
  intention-to-treat, fallbacks count against the arm that caused them.
- Metric: purchases per 100 requests (the scored metric), plus cold-only, clicks, revenue. The
  feedback's `purchased` is a draw from the hidden model's probability, so it is noisy but unbiased.
- Rule: a challenger is promoted only if both arms have >= `min_n` answers and the one-sided 95 %
  lower bound of (challenger - champion) purchases/100 is above 0 (two-proportion z-test).
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field

from rtrec.contract import State
from rtrec.schemas import Request

N_BUCKETS = 10_000


def bucket(request_id: str, salt: str = "") -> int:
    return zlib.crc32((salt + str(request_id)).encode()) % N_BUCKETS


@dataclass
class _Arm:
    n: int = 0
    buys: int = 0
    clicks: int = 0
    revenue: float = 0.0
    team: int = 0
    n_cold: int = 0
    buys_cold: int = 0

    def rate(self, cold: bool = False) -> tuple[float, float]:
        n, b = (self.n_cold, self.buys_cold) if cold else (self.n, self.buys)
        if n == 0:
            return 0.0, float("inf")
        p = b / n
        return 100.0 * p, 100.0 * math.sqrt(max(p * (1 - p), 1e-12) / n)


@dataclass
class ABTest:
    """Arms in order; the first one is the champion. weights need not sum to 1."""
    arms: list[tuple[str, float]]
    salt: str = "ab1"
    min_n: int = 2000
    z: float = 1.645
    stats: dict[str, _Arm] = field(default_factory=dict)

    def __post_init__(self):
        tot = sum(w for _, w in self.arms)
        edges, acc = [], 0.0
        for name, w in self.arms:
            acc += w / tot
            edges.append((round(acc * N_BUCKETS), name))
        self._edges = edges
        for name, _ in self.arms:
            self.stats.setdefault(name, _Arm())

    def assign(self, request_id: str) -> str:
        b = bucket(request_id, self.salt)
        for edge, name in self._edges:
            if b < edge:
                return name
        return self._edges[-1][1]

    def update(self, request_id: str, fb: dict, info=None) -> None:
        a = self.stats[self.assign(request_id)]
        bought = bool(fb.get("purchased"))
        a.n += 1
        a.buys += bought
        a.clicks += bool(fb.get("clicked"))
        a.revenue += float(fb.get("revenue_eur") or 0.0)
        a.team += fb.get("served_by", "team") == "team"
        if info is not None and info.cold:
            a.n_cold += 1
            a.buys_cold += bought

    def report(self) -> dict:
        champ = self.arms[0][0]
        c = self.stats[champ]
        out = {"champion": champ, "arms": {}, "promote": None}
        best_lb = 0.0
        for name, _ in self.arms:
            a = self.stats[name]
            m, se = a.rate()
            mc, sec = a.rate(cold=True)
            row = {"n": a.n, "purchases_per_100": m, "ci95": (m - 1.96 * se, m + 1.96 * se),
                   "cold_n": a.n_cold, "cold_purchases_per_100": mc,
                   "clicks_per_100": 100.0 * a.clicks / a.n if a.n else 0.0,
                   "revenue_per_request": a.revenue / a.n if a.n else 0.0,
                   "team_rate": a.team / a.n if a.n else 0.0}
            if name != champ and a.n and c.n:
                diff = m - c.rate()[0]
                se_d = math.sqrt(se ** 2 + c.rate()[1] ** 2)
                lb = diff - self.z * se_d
                row.update(diff_vs_champion=diff, diff_lower_bound=lb)
                if a.n >= self.min_n and c.n >= self.min_n and lb > best_lb:
                    best_lb, out["promote"] = lb, name
            out["arms"][name] = row
        return out


@dataclass
class ABRouter:
    """A Policy that routes each request to one arm. The runtime should write
    `router.version_for(req)` as model_version (falls back to `name`)."""
    test: ABTest
    policies: dict
    name: str = "ab"

    def version_for(self, req: Request) -> str:
        return self.test.assign(req["request_id"])

    def recommend(self, req: Request, state: State) -> list[str]:
        return self.policies[self.version_for(req)].recommend(req, state)


def install(state: State, test: ABTest) -> None:
    """Register the test so on_feedback attributes every answer to its arm (state.live['ab'])."""
    state.live["ab"] = test
