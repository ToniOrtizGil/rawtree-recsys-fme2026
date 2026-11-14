"""Master's arbiter: scores any Policy on the simulator exactly like the organisers' leaderboard.

    python scripts/arbiter.py rtrec.model.policies:SessionHeuristicPolicy --minutes 20 --seed 7

Why it exists: agents report their own numbers; promotion to production is decided here, with one
fixed procedure for every candidate:
  - a fresh simulator per policy (same seed), because what we sell changes later behaviour;
  - live hooks (rtrec.live) fed in event time, feedback delivered a few seconds after each answer;
  - baseline = all-time best sellers ranked ONCE at the start, filtered by current stock (= fallback);
  - score = 100 * (team - baseline) / (oracle - baseline), clipped to [0, 100], on expected purchases
    (sim.expected_purchase, the "no luck" metric), overall and on cold requests.
Timing/latency is NOT measured here (that is A1's load test): every answer is assumed on time.
"""
from __future__ import annotations

import argparse
import importlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field

from rtrec import live
from rtrec.contract import Policy, State
from rtrec.live._util import to_epoch
from rtrec.schemas import N_ITEMS
from rtrec.validate import finalize, is_valid


class BestSellerPolicy:
    """The organisers' baseline and fallback: fixed all-time best sellers, in stock."""
    name = "baseline"

    def __init__(self, ranking: list[str]):
        self.ranking = ranking

    def recommend(self, req, state: State) -> list[str]:
        return [p for p in self.ranking if p in state.available][:N_ITEMS]


def best_sellers(history_events) -> list[str]:
    """Same rule as the starter kit: purchase count desc, ties by higher product_id."""
    buys = history_events[history_events["event_type"] == "purchase"]["product_id"].astype(str)
    counts = Counter(buys)
    return [p for _, p in sorted(((n, p) for p, n in counts.items()), reverse=True)]


@dataclass
class Totals:
    n: int = 0
    team: float = 0.0
    base: float = 0.0
    oracle: float = 0.0

    def add(self, t: float, b: float, o: float) -> None:
        self.n += 1
        self.team += t
        self.base += b
        self.oracle += o

    def score(self) -> float:
        gap = self.oracle - self.base
        if self.n == 0 or gap <= 0:
            return float("nan")
        return max(0.0, min(100.0, 100.0 * (self.team - self.base) / gap))

    def per100(self, x: float) -> float:
        return 100.0 * x / self.n if self.n else float("nan")


@dataclass
class Report:
    policy: str
    overall: Totals = field(default_factory=Totals)
    cold: Totals = field(default_factory=Totals)
    invalid_before_finalize: int = 0
    errors: int = 0
    ms_per_request: float = 0.0

    def as_dict(self) -> dict:
        o, c = self.overall, self.cold
        return {
            "policy": self.policy, "requests": o.n, "cold_requests": c.n,
            "score": round(o.score(), 2), "cold_score": round(c.score(), 2),
            "purchases_per_100": {"team": round(o.per100(o.team), 3), "baseline": round(o.per100(o.base), 3),
                                  "oracle": round(o.per100(o.oracle), 3)},
            "cold_purchases_per_100": {"team": round(c.per100(c.team), 3), "baseline": round(c.per100(c.base), 3),
                                       "oracle": round(c.per100(c.oracle), 3)},
            "invalid_before_finalize": self.invalid_before_finalize, "policy_errors": self.errors,
            "ms_per_request": round(self.ms_per_request, 3),
        }


def run(policy_factory, sim, warmup_s: float, eval_s: float, start: float, step: float = 0.5,
        feedback_delay: float = 5.0) -> Report:
    """Replay the simulator in event time. policy_factory(state, dump) -> Policy."""
    dump = sim.history()
    state = State()
    live.init(state, dump.catalog)
    if hasattr(live, "seed_history"):   # long-run shares for group-trend lifts (A4)
        live.seed_history(state, dump.history_events)
    ranking = best_sellers(dump.history_events)
    state.safe_list = ranking[:200]
    baseline = BestSellerPolicy(ranking)
    policy: Policy = policy_factory(state, dump)
    rep = Report(policy=getattr(policy, "name", type(policy).__name__))
    pending: list[tuple[float, dict]] = []      # (deliver_at, feedback row)
    spent = 0.0
    now = start
    while now < start + warmup_s + eval_s:
        now += step
        tick = sim.advance(now)
        if tick.catalog_updates:
            live.on_catalog_updates(tick.catalog_updates, state)
        if tick.events:
            live.on_events(tick.events, state)
        if tick.requests:
            live.on_requests(tick.requests, state)
        due = [fb for t, fb in pending if t <= now]
        if due:
            pending = [(t, fb) for t, fb in pending if t > now]
            live.on_feedback(due, state)
        scoring = now > start + warmup_s
        for req in tick.requests:
            t0 = time.perf_counter()
            try:
                raw = policy.recommend(req, state)
            except Exception:  # noqa: BLE001 - a crash is a fallback, count it and continue
                rep.errors += 1
                raw = baseline.recommend(req, state)
            spent += time.perf_counter() - t0
            if not is_valid(list(raw or [])[:N_ITEMS], state):
                rep.invalid_before_finalize += 1
            items = finalize(raw, state)
            if scoring:
                base_items = finalize(baseline.recommend(req, state), state)
                t, b = sim.expected_purchase(req, items), sim.expected_purchase(req, base_items)
                o = sim.expected_purchase(req, sim.oracle(req))
                rep.overall.add(t, b, o)
                if state.is_cold(req):
                    rep.cold.add(t, b, o)
            sub = {"request_id": req["request_id"], "items": items, "model_version": rep.policy}
            received = to_epoch(req["ts"]) + 1.0
            pending.append((received + feedback_delay, sim.judge(sub, received)))
    n_all = rep.overall.n or 1
    rep.ms_per_request = 1000.0 * spent / n_all
    return rep


def load(spec: str):
    """'pkg.module:Name' -> factory(state, dump). Name may be a class taking (state, dump), () or nothing."""
    mod, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(mod), attr)

    def factory(state, dump):
        for args in ((state, dump), (state,), ()):
            try:
                return obj(*args)
            except TypeError:
                continue
        raise TypeError(f"cannot build {spec}")
    return factory


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("policies", nargs="+", help="module:Class, e.g. rtrec.model.policies:RankerPolicy")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--warmup-min", type=float, default=10)
    ap.add_argument("--minutes", type=float, default=20, help="scored window (final round = 120)")
    args = ap.parse_args()
    from sim.shop import make_simulator  # A2
    for spec in args.policies:
        sim = make_simulator(seed=args.seed)
        start = getattr(sim, "stream_start", None) or getattr(sim, "start_ts")
        rep = run(load(spec), sim, args.warmup_min * 60, args.minutes * 60, start)
        print(json.dumps(rep.as_dict()))


if __name__ == "__main__":
    main()
