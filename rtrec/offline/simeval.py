"""Ground-truth evaluation of policies on a simulator: the challenge's score, without luck.

    score = 100 * (team - baseline) / (oracle - baseline)

where each term is the mean of `sim.expected_purchase(req, items)` over the same requests, the
baseline is the all-time best-seller list ordered once at stream start (filtered to what is in stock),
and the oracle is `sim.oracle(req)`. Reported overall and on cold requests (organisers' definition,
`State.is_cold`). The stream is replayed through `LiveFeed`, so policies see a State that evolves
like the live one (new products, stock-outs, trends, profiles).

Only the simulator knows `expected_purchase`; on real logs use rtrec.offline.estimators instead.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from rtrec.contract import Policy, State
from rtrec.model.util import to_epoch
from rtrec.validate import finalize, is_valid

from .replay import LiveFeed


@dataclass
class PolicyResult:
    name: str
    ep: list[float] = field(default_factory=list)       # expected purchases per request
    cold: list[bool] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    invalid: int = 0


def _norm(team: float, base: float, orac: float) -> float:
    d = orac - base
    return float(100.0 * (team - base) / d) if d > 1e-12 else float("nan")


def evaluate_on_sim(sim, state: State, feed: LiveFeed, policies: list[Policy], minutes: float = 10.0,
                    max_requests: int | None = None, oracle: bool = True, judge_with: int | None = 0,
                    step_s: float = 0.5, feedback_delay_s: float = 5.0) -> dict:
    """Replay `minutes` of live stream; every request is answered by every policy (same State).

    judge_with: index of the policy whose answer is sent to sim.judge (so purchases happen and the
    "already bought" effect plays out); None to judge nothing."""
    baseline_order = list(state.offline.get("best_sellers") or state.safe_list)  # fixed at stream start
    res = [PolicyResult(p.name) for p in policies]
    base_ep, orac_ep, cold_flags = [], [], []
    pending: list[tuple[float, dict]] = []  # feedback rows, delivered feedback_delay_s after the answer
    t = float(getattr(sim, "now", None) or getattr(sim, "start_ts", time.time()))
    t_end = t + 60.0 * minutes
    n = 0
    while t < t_end and (max_requests is None or n < max_requests):
        t += step_s
        tick = sim.advance(t)
        feed.on_catalog_updates(tick.catalog_updates)
        feed.on_events(tick.events)
        feed.on_requests(tick.requests)
        due = [fb for when, fb in pending if when <= t]
        if due:
            pending = [(when, fb) for when, fb in pending if when > t]
            feed.on_feedback(due)
        state.safe_list = [p for p in baseline_order if p in state.available][:200]
        for req in tick.requests:
            if max_requests is not None and n >= max_requests:
                break
            n += 1
            cold = state.is_cold(req)
            cold_flags.append(cold)
            base_ep.append(sim.expected_purchase(req, finalize(baseline_order, state)))
            orac_ep.append(sim.expected_purchase(req, sim.oracle(req)) if oracle else float("nan"))
            for j, (pol, r) in enumerate(zip(policies, res)):
                t0 = time.perf_counter()
                items = finalize(pol.recommend(req, state), state)
                r.latency_ms.append(1000 * (time.perf_counter() - t0))
                r.invalid += not is_valid(items, state)
                r.ep.append(sim.expected_purchase(req, items))
                r.cold.append(cold)
                if judge_with == j:
                    received = to_epoch(req["ts"]) + 1.0
                    sub = {"request_id": req["request_id"], "items": items, "model_version": pol.name}
                    pending.append((received + feedback_delay_s, sim.judge(sub, received)))
    return summarise(res, np.array(base_ep), np.array(orac_ep), np.array(cold_flags, bool))


def summarise(res: list[PolicyResult], base: np.ndarray, orac: np.ndarray, cold: np.ndarray) -> dict:
    out = {"n_requests": int(len(base)), "n_cold": int(cold.sum()),
           "baseline_per100": float(100 * base.mean()) if len(base) else float("nan"),
           "oracle_per100": float(100 * np.nanmean(orac)) if np.isfinite(orac).any() else float("nan"), "policies": {}}
    for r in res:
        ep = np.array(r.ep)
        row = {"per100": float(100 * ep.mean()), "score": _norm(ep.mean(), base.mean(), np.nanmean(orac)),
               "p50_ms": float(np.percentile(r.latency_ms, 50)), "p99_ms": float(np.percentile(r.latency_ms, 99)),
               "invalid": r.invalid}
        # 95% CI of the score from the paired per-request differences (same requests for every policy)
        d = orac.mean() - base.mean()
        if len(ep) > 1 and d > 0:
            se = (ep - base).std(ddof=1) / np.sqrt(len(ep))
            row["score_ci95"] = float(100 * 1.96 * se / d)
        if cold.any():
            row["cold_per100"] = float(100 * ep[cold].mean())
            row["cold_score"] = _norm(ep[cold].mean(), base[cold].mean(), np.nanmean(orac[cold]))
        if (~cold).any():
            row["warm_score"] = _norm(ep[~cold].mean(), base[~cold].mean(), np.nanmean(orac[~cold]))
        out["policies"][r.name] = row
    return out


def format_report(rep: dict) -> str:
    lines = [f"requests={rep['n_requests']} cold={rep['n_cold']}  baseline={rep['baseline_per100']:.3f}/100  "
             f"oracle={rep['oracle_per100']:.3f}/100",
             f"{'policy':28s} {'per100':>8s} {'score':>7s} {'±95%':>6s} {'cold':>7s} {'warm':>7s} {'p50ms':>6s} {'p99ms':>6s} inv"]
    for name, r in rep["policies"].items():
        lines.append(f"{name:28s} {r['per100']:8.3f} {r['score']:7.1f} {r.get('score_ci95', float('nan')):6.1f} "
                     f"{r.get('cold_score', float('nan')):7.1f} {r.get('warm_score', float('nan')):7.1f} "
                     f"{r['p50_ms']:6.2f} {r['p99_ms']:6.2f} {r['invalid']}")
    return "\n".join(lines)
