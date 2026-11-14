"""Training rows from our *own* served traffic: what the practice round will give us.

The old carousel's log is thin (about 1 purchase per 80 impressions, a few thousand purchases
in 60 days), because it showed best sellers to everyone. During the 24-hour practice round we serve
~260,000 requests with personalised lists that convert ~4x better, and the feedback table tells us
what was clicked and bought. Those rows teach the ranker far more than the history can, and the
features are relations (no IDs), so what it learns on Saturday's shoppers transfers to Sunday's.

`collect` reproduces that on a simulator: serve with a policy, explore a little (so the model also
sees products the policy would not rank first, and every slot), judge each answer with the
simulator, deliver the feedback to the live state with a delay, and keep the features of every
shown (product, slot) with its click / purchase outcome. Only judged outcomes are used, never the
simulator's hidden probabilities: the same thing is possible on RawTree.
"""
from __future__ import annotations

import time

import numpy as np

from rtrec.contract import Policy, State
from rtrec.model.features import featurize
from rtrec.model.util import clean_id, to_epoch
from rtrec.validate import finalize

from .replay import LiveFeed


def explore(items: list[str], pool: list[str], rng: np.random.Generator, eps: float) -> list[str]:
    """With prob eps per slot (slots 3..10), swap in a random product from the policy's top-40."""
    out = list(items[:10])
    extra = [p for p in pool[:40] if p not in out]
    for k in range(3, len(out)):
        if extra and rng.random() < eps:
            j = int(rng.integers(len(extra)))
            out[k] = extra.pop(j)
    return out


def collect(sim, state: State, feed: LiveFeed, policy: Policy, minutes: float, eps: float = 0.2,
            seed: int = 0, step_s: float = 0.5, feedback_delay_s: float = 5.0, log=print):
    rng = np.random.default_rng(seed)
    Xs, ys, cs, gs = [], [], [], []
    pending: list[tuple[float, dict]] = []
    t = float(getattr(sim, "now", time.time()))
    t_end = t + 60 * minutes
    n = 0
    tic = time.time()
    while t < t_end:
        t += step_s
        tick = sim.advance(t)
        feed.on_catalog_updates(tick.catalog_updates)
        feed.on_events(tick.events)
        feed.on_requests(tick.requests)
        due = [fb for when, fb in pending if when <= t]
        if due:
            pending = [(w, fb) for w, fb in pending if w > t]
            feed.on_feedback(due)
        for req in tick.requests:
            ranked = list(policy.recommend(req, state))
            items = finalize(explore(ranked, ranked, rng, eps), state)
            now = to_epoch(req["ts"])
            Xs.append(featurize(req, state, items, slot=list(range(len(items))), now=now))
            received = now + 1.0
            fb = sim.judge({"request_id": req["request_id"], "items": items, "model_version": policy.name}, received)
            pending.append((received + feedback_delay_s, fb))
            pur, clk = clean_id(fb.get("purchased")), clean_id(fb.get("clicked"))
            ys.append(np.array([1.0 if pur == p else 0.0 for p in items]))
            cs.append(np.array([1.0 if clk == p else 0.0 for p in items]))
            gs.append(np.full(len(items), n))
            n += 1
    y = np.concatenate(ys) if ys else np.zeros(0)
    log(f"stream log: {n} requests, {int(y.sum())} purchases, {int(np.concatenate(cs).sum()) if cs else 0} clicks "
        f"in {time.time() - tic:.0f}s")
    if not Xs:
        from rtrec.model.features import FEATURES
        return np.zeros((0, len(FEATURES)), np.float32), y, y, np.zeros(0, int)
    return np.vstack(Xs), y, np.concatenate(cs), np.concatenate(gs)
