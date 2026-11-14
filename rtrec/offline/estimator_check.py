"""Are our off-policy estimators unbiased? Check them where the truth is known: on a simulator.

On RawTree we will only have logs (history_sessions, and our own feedback), so every offline number
we trust comes from IPS / SNIPS / DM / DR. The simulator lets us measure their bias first:

1. Run the live stream; for every request a *logging policy* with known randomness (the old carousel's
   "best sellers + a bit of randomness", sampled slot by slot) picks the 10 products shown, and the
   simulator judges that slate (click / purchase). We record the per-slot propensities exactly like
   `history_sessions.propensity`.
2. For the same requests, each *target policy* proposes its list; the simulator tells us its true
   expected purchases (`sim.expected_purchase`), which no real log would give us.
3. The estimators see only the logs, and we compare them to the truth.

The answer tells us which estimator to believe on real data, and how much evidence (matches) a
personalised policy needs before an estimate means anything.
"""
from __future__ import annotations

import time

import numpy as np

from rtrec.contract import Policy, State
from rtrec.model import ranker as rk
from rtrec.model.features import featurize
from rtrec.model.util import to_epoch
from rtrec.validate import finalize

from . import estimators as est
from .replay import LiveFeed


class PopularityLogger:
    """The old carousel: Plackett-Luce over best sellers (weight ~ rank^-power), mixed with uniform."""

    def __init__(self, ranking: list[str], power: float = 1.0, epsilon: float = 0.1, top: int = 500, seed: int = 0):
        self.ranking, self.power, self.eps, self.top = ranking, power, epsilon, top
        self.rng = np.random.default_rng(seed)

    def slate(self, state: State, n: int = 10) -> tuple[list[str], list[float]]:
        pool = [p for p in self.ranking if p in state.available][: self.top]
        w = (np.arange(1, len(pool) + 1, dtype=float)) ** -self.power
        w = (1 - self.eps) * w / w.sum() + self.eps / len(pool)
        shown, props = [], []
        mask = np.ones(len(pool), bool)
        for _ in range(min(n, len(pool))):
            ww = np.where(mask, w, 0.0)
            ww /= ww.sum()
            j = int(self.rng.choice(len(pool), p=ww))
            shown.append(pool[j])
            props.append(float(ww[j]))
            mask[j] = False
        return shown, props


def check(sim, state: State, feed: LiveFeed, policies: list[Policy], logger: PopularityLogger,
          minutes: float = 30.0, booster=None, step_s: float = 0.5) -> dict:
    lists = {p.name: [] for p in policies}
    truth = {p.name: [] for p in policies}
    q_new = {p.name: [] for p in policies}
    shown, props, purchased, q_logged, logged_truth = [], [], [], [], []
    t = float(getattr(sim, "now", None) or getattr(sim, "start_ts", time.time()))
    t_end = t + 60 * minutes
    while t < t_end:
        t += step_s
        tick = sim.advance(t)
        feed.on_catalog_updates(tick.catalog_updates)
        feed.on_events(tick.events)
        for req in tick.requests:
            now = to_epoch(req["ts"])
            for pol in policies:  # targets first: judging the logged slate changes the simulator
                items = finalize(pol.recommend(req, state), state)
                lists[pol.name].append(items)
                truth[pol.name].append(sim.expected_purchase(req, items))
                if booster is not None:
                    q_new[pol.name].append(rk.predict(booster, featurize(req, state, items, slot=list(range(10)), now=now)))
            sl, pr = logger.slate(state)
            logged_truth.append(sim.expected_purchase(req, sl))
            if booster is not None:
                q_logged.append(rk.predict(booster, featurize(req, state, sl, slot=list(range(len(sl))), now=now)))
            fb = sim.judge({"request_id": req["request_id"], "items": sl, "model_version": "logger"}, now + 1)
            shown.append(sl)
            props.append(pr)
            purchased.append(fb["purchased"] or None)
    marg = est.empirical_marginals(shown)
    out = {"n": len(shown), "logged_truth_per100": 100 * float(np.mean(logged_truth)),
           "logged_observed_per100": 100 * float(np.mean([y is not None for y in purchased])), "policies": {}}
    for pol in policies:
        name = pol.name
        qn = np.array(q_new[name]) if booster is not None else None
        ql = np.array(q_logged) if booster is not None else None
        rc = est.estimate(lists[name], shown, props, purchased, qn, ql, marginals=None)
        re = est.estimate(lists[name], shown, props, purchased, qn, ql, marginals=marg)
        row = {"truth": 100 * float(np.mean(truth[name])), "matches": re["matches"],
               "ips_cond": rc["ips"], "ips_cond_ci": rc["ips_ci"], "ips_emp": re["ips"], "ips_emp_ci": re["ips_ci"],
               "snips_emp": re["snips"], "snips_emp_ci": re["snips_ci"]}
        if booster is not None:
            row.update(dm=re["dm"], dr_emp=re["dr"], dr_emp_ci=re["dr_ci"])
        out["policies"][name] = row
    return out


def format_check(rep: dict) -> str:
    cols = ["truth", "ips_cond", "ips_emp", "ips_emp_ci", "snips_emp", "snips_emp_ci", "dm", "dr_emp", "dr_emp_ci",
            "matches"]
    lines = [f"logged requests={rep['n']}  logging policy: truth={rep['logged_truth_per100']:.3f} "
             f"observed={rep['logged_observed_per100']:.3f} per 100",
             f"{'policy':24s} " + " ".join(f"{c:>12s}" for c in cols)]
    for name, r in rep["policies"].items():
        lines.append(f"{name:24s} " + " ".join(f"{r.get(c, float('nan')):12.3f}" for c in cols))
    return "\n".join(lines)
