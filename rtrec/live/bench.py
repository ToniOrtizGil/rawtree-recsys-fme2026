"""Replay a simulator through the live hooks and score policies the way the organisers do.

For every request each policy answers from the *same* State; answers go through finalize() and are
scored with the simulator's expected purchase probability (no luck), against
  baseline = all-time best sellers from history, ordered once at the start, filtered to in-stock,
  oracle   = sim.oracle(req),
as  score = 100 * (team - baseline) / (oracle - baseline), globally and on cold requests
(new shopper or a viewed product created during the event). Only the `serve` policy's answers are
judged by the simulator, so only they produce feedback (exactly like the live event).

    python -m rtrec.live.bench --minutes 30          # needs sim.shop.make_simulator (A2)
"""
from __future__ import annotations

import argparse
import heapq
import time
from collections import Counter
from dataclasses import dataclass, field

from rtrec import live
from rtrec.contract import State
from rtrec.validate import finalize

from ._util import to_epoch


@dataclass
class Score:
    n: int = 0
    team: float = 0.0
    base: float = 0.0
    oracle: float = 0.0

    def add(self, t: float, b: float, o: float) -> None:
        self.n += 1
        self.team += t
        self.base += b
        self.oracle += o

    @property
    def normalised(self) -> float:
        d = self.oracle - self.base
        return 100.0 * (self.team - self.base) / d if d > 0 else 0.0

    @property
    def per_100(self) -> float:
        return 100.0 * self.team / self.n if self.n else 0.0


@dataclass
class BenchResult:
    minutes: float
    n_requests: int
    scores: dict = field(default_factory=dict)        # name -> {"all": Score, "cold": Score}
    latency_ms: dict = field(default_factory=dict)    # name -> p99 ms of recommend()
    hook_cpu_s: float = 0.0

    def table(self) -> str:
        lines = [f"{self.n_requests} requests, {self.minutes:.0f} min, hooks cpu {self.hook_cpu_s:.2f}s",
                 f"{'policy':38s} {'score':>7s} {'cold':>7s} {'buy/100':>8s} {'base/100':>8s} "
                 f"{'orc/100':>8s} {'n_cold':>6s} {'p99ms':>6s}"]
        for name, s in self.scores.items():
            a, c = s["all"], s["cold"]
            lines.append(f"{name:38s} {a.normalised:7.1f} {c.normalised:7.1f} {a.per_100:8.2f} "
                         f"{100 * a.base / max(a.n, 1):8.2f} {100 * a.oracle / max(a.n, 1):8.2f} "
                         f"{c.n:6d} {self.latency_ms.get(name, 0):6.2f}")
        return "\n".join(lines)


def best_sellers(history_events, k: int = 200) -> list[str]:
    """All-time best sellers (organic purchases) from the history dump: the organisers' baseline."""
    if hasattr(history_events, "loc"):
        ev = history_events
        vc = ev.loc[ev["event_type"] == "purchase", "product_id"].astype(str).value_counts()
        return list(vc.index[:k])
    c = Counter(str(e["product_id"]) for e in history_events if e["event_type"] == "purchase")
    return [p for p, _ in c.most_common(k)]


def run(sim, policies: dict, minutes: float = 30.0, serve: str | None = None, history_days: int = 7,
        step_s: float = 0.5, feedback_delay_s: float = 4.0, start: float | None = None,
        state: State | None = None, on_tick=None) -> BenchResult:
    dump = sim.history(days=history_days)
    state = state or State()
    live.init(state, dump.catalog)
    safe = best_sellers(dump.history_events)
    state.safe_list = safe
    serve = serve or next(iter(policies))
    if start is None:
        start = getattr(sim, "start", None) or getattr(sim, "start_ts", None) or time.time()
    start = to_epoch(start)
    res = BenchResult(minutes=minutes, n_requests=0)
    for name in policies:
        res.scores[name] = {"all": Score(), "cold": Score()}
    lat: dict[str, list[float]] = {n: [] for n in policies}
    pending: list = []                               # (due, seq, feedback) heap
    seq = 0
    now = start
    end = start + minutes * 60.0
    lr = live.learner(state)
    while now < end:
        now += step_s
        tick = sim.advance(now)
        live.on_catalog_updates(tick.catalog_updates, state)
        live.on_events(tick.events, state)
        live.on_requests(tick.requests, state)
        due = []
        while pending and pending[0][0] <= now:
            due.append(heapq.heappop(pending)[2])
        if due:
            live.on_feedback(due, state)
        for req in tick.requests:
            res.n_requests += 1
            base_items = finalize(safe, state)
            o_items = finalize(sim.oracle(req), state)
            eb = sim.expected_purchase(req, base_items)
            eo = sim.expected_purchase(req, o_items)
            cold = state.is_cold(req)
            for name, pol in policies.items():
                t0 = time.perf_counter()
                items = finalize(pol.recommend(req, state), state)
                lat[name].append(1000 * (time.perf_counter() - t0))
                et = sim.expected_purchase(req, items)
                res.scores[name]["all"].add(et, eb, eo)
                if cold:
                    res.scores[name]["cold"].add(et, eb, eo)
                if name == serve:
                    mv = pol.version_for(req) if hasattr(pol, "version_for") else pol.name
                    fb = sim.judge({"request_id": req["request_id"], "items": items, "model_version": mv},
                                   to_epoch(req["ts"]) + 1.0)
                    seq += 1
                    heapq.heappush(pending, (now + feedback_delay_s, seq, fb))
        if on_tick is not None:
            on_tick(now, state)
    for name, xs in lat.items():
        xs.sort()
        res.latency_ms[name] = xs[int(0.99 * (len(xs) - 1))] if xs else 0.0
    res.hook_cpu_s = lr.stats["cpu_s"]
    return res


def default_policies():
    from .policy import LiveSessionPolicy, LiveWrapper

    class SafeList:
        name = "best-sellers"

        def recommend(self, req, state):
            return state.safe_list

    base = LiveSessionPolicy()
    return {
        "live-session+cx": LiveWrapper(base, rerank=True, explore=True),
        "best-sellers": SafeList(),
        "live-session": base,
        "live-session+c": LiveWrapper(base, rerank=False),
        "live-session+cr": LiveWrapper(base, rerank=True),
        "best-sellers+trend": LiveWrapper(SafeList(), cold_merge=False, beta=1.0, alpha=0.0, gamma=0.3),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--history-days", type=int, default=7)
    a = ap.parse_args(argv)
    from sim.shop import make_simulator  # A2
    sim = make_simulator(seed=a.seed)
    print(run(sim, default_policies(), minutes=a.minutes, history_days=a.history_days).table())


if __name__ == "__main__":
    main()
