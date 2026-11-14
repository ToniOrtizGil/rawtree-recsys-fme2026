"""End-to-end offline pipeline on a history dump: artefacts -> training rows -> ranker -> evaluation.

Used by scripts/train_ranker.py and scripts/eval_offline.py; works the same on the simulator's dump
and on the real Parquet download (same table shapes), so the numbers we show the jury are produced
by the code that serves.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score

from rtrec.contract import Policy, State
from rtrec.model import ranker as rk
from rtrec.model.artefacts import build_offline
from rtrec.model.features import F_INDEX, featurize
from rtrec.model.util import clean_id, to_epoch
from rtrec.validate import finalize

from . import estimators as est
from .replay import LiveFeed, state_from_history
from .trainset import build_rows, session_to_request, time_cuts


def window(df: pd.DataFrame, t_from: float, t_to: float) -> pd.DataFrame:
    e = df["ts"].map(to_epoch)
    return df[(e >= t_from) & (e < t_to)]


@dataclass
class Prepared:
    state: State             # as of the start of the test window (artefacts from window A)
    feed: LiveFeed
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    c: np.ndarray            # click labels (y = purchase labels)
    cuts: tuple[float, float, float, float]
    test_sessions: pd.DataFrame
    test_events: pd.DataFrame
    val_mask: np.ndarray | None = None   # rows of the validation window (early stopping only)
    w: np.ndarray | None = None          # inverse-propensity row weights (None = unweighted)


def prepare(catalog: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame, a: float = 0.35, v: float = 0.70,
            b: float = 0.80, sample: float = 1.0, ipw_clip: float = 0.0, log=print) -> Prepared:
    """Chronological split of the history (time order, so nothing from the future leaks backwards):

      train 70 %  = [0, a) offline artefacts (popularity, co-views, view->buy...) + [a, v) training rows
      valid 10 %  = [v, b) rows used only for early stopping / model selection
      test  20 %  = [b, 1] never touched until the final offline evaluation

    Features of every row are computed with what was known at that moment (the feed replays events)."""
    t0, ta, tb_, t1 = time_cuts(sessions, a, b)
    tv = t0 + v * (t1 - t0)
    tic = time.time()
    s_a, e_a = window(sessions, t0, ta), window(events, t0 - 1e9, ta)
    state, feed = state_from_history(catalog, s_a, e_a)
    log(f"artefacts from {len(s_a)} sessions / {len(e_a)} events in {time.time() - tic:.1f}s")
    X1, y1, c1, g1, p1 = build_rows(state, feed, sessions, events, ta, tv, sample=sample, return_props=True)
    X2, y2, c2, g2, p2 = (build_rows(state, feed, sessions, events, tv, tb_, sample=sample, seed=1, return_props=True)
                          if tb_ > tv else (np.zeros((0, X1.shape[1]), np.float32), np.zeros(0), np.zeros(0),
                                            np.zeros(0, int), np.zeros(0)))
    off = int(g1.max()) + 1 if len(g1) else 0
    X, y, c = np.vstack([X1, X2]), np.concatenate([y1, y2]), np.concatenate([c1, c2])
    g = np.concatenate([g1, g2 + off])
    val = np.concatenate([np.zeros(len(y1), bool), np.ones(len(y2), bool)])
    log(f"train rows {len(y1)} (buys {int(y1.sum())}) · valid rows {len(y2)} (buys {int(y2.sum())}) "
        f"in {time.time() - tic:.1f}s")
    w = ipw_weights(np.concatenate([p1, p2]), ipw_clip) if ipw_clip > 0 else None
    return Prepared(state, feed, X, y, g, c, (t0, ta, tb_, t1), window(sessions, tb_, t1 + 1),
                    window(events, tb_, t1 + 1), val_mask=val, w=w)


def ipw_weights(props: np.ndarray, clip: float) -> np.ndarray:
    """Correct the old carousel's bias: an item it rarely showed counts more, w = min(1/propensity, clip),
    normalised to mean 1. Clipping keeps the variance bounded (most propensities are tiny). Rows without a
    logged propensity (our own practice-stream answers) get the mean weight."""
    w = np.minimum(1.0 / np.clip(props, 1e-9, None), clip)
    w[~np.isfinite(props) | (props <= 0)] = np.nan
    w = np.where(np.isnan(w), np.nanmean(w) if np.isfinite(w).any() else 1.0, w)
    return w / w.mean()


def fit(p: Prepared, val_frac: float = 0.15, rounds: int = 600, fixed_rounds=None, log=print):
    """Train on the earlier part of the training window, early-stop on its last `val_frac` (by time)."""
    if p.val_mask is not None:
        va = p.val_mask
    else:
        n_imp = int(p.groups.max()) + 1 if len(p.groups) else 0
        va = p.groups >= int(n_imp * (1 - val_frac))
    model, metrics = rk.train(p.X, p.y, p.c, va, rounds=rounds, fixed_rounds=fixed_rounds, weight=p.w)
    metrics.update(train_rows=int((~va).sum()), val_rows=int(va.sum()))
    pv = model.predict(p.X[va]) if va.any() else np.array([])
    if va.any() and 0 < p.y[va].sum() < va.sum():
        metrics["val_auc"] = float(roc_auc_score(p.y[va], pv))
        metrics["val_logloss"] = float(log_loss(p.y[va], np.clip(pv, 1e-6, 1 - 1e-6)))
        metrics["val_pred_mean"], metrics["val_obs_mean"] = float(pv.mean()), float(p.y[va].mean())
    booster = model
    for part in ("click", "buy"):
        b = getattr(model, part)
        if b is not None:
            imp = b.feature_importance("gain")
            metrics[f"top_{part}_features"] = [f for _, f in sorted(zip(imp, b.feature_name()), reverse=True)[:10]]
    log(f"ranker: {metrics}")
    return booster, metrics


def offline_eval(p: Prepared, policies: list[Policy], booster=None, sim=None, max_impressions: int = 20000,
                 log=print) -> dict:
    """Replay the test window: every policy answers every logged impression with the State as of then.

    Reports IPS/SNIPS/DM/DR purchases per 100 (with CI and match counts), hit@10 and, when a simulator
    is given, the ground truth mean of sim.expected_purchase over the same impressions."""
    state, feed = copy.deepcopy((p.state, p.feed))  # one deepcopy keeps feed.s is state
    if booster is not None:
        state.offline["ranker"] = booster
    ses = p.test_sessions.assign(_t=p.test_sessions["ts"].map(to_epoch)).sort_values("_t").head(max_impressions)
    ev = p.test_events.assign(_t=p.test_events["ts"].map(to_epoch)).sort_values("_t")
    ev_t, ev_recs = ev._t.to_numpy(), ev.drop(columns="_t").to_dict("records")
    lists = {pol.name: [] for pol in policies}
    q_new = {pol.name: [] for pol in policies}
    truth = {pol.name: [] for pol in policies}
    q_logged, shown, props, purchased, clicked, cold, newu = [], [], [], [], [], [], []
    j = 0
    slot_idx = F_INDEX["slot"]
    for row in ses.to_dict("records"):
        k = int(np.searchsorted(ev_t, row["_t"]))
        if k > j:
            feed.on_events(ev_recs[j:k])
            j = k
        req = session_to_request(row, state)
        sh = [str(x) for x in list(row["shown"])[:10]]
        shown.append(sh)
        props.append([float(x) for x in list(row["propensity"])[:10]])
        purchased.append(clean_id(row.get("purchased")))
        clicked.append(clean_id(row.get("clicked")))
        cold.append(state.is_cold(req))
        newu.append(bool(req["is_new_user"]))
        if booster is not None:
            q_logged.append(rk.predict(booster, featurize(req, state, sh, slot=list(range(len(sh))), now=row["_t"])))
        for pol in policies:
            items = finalize(pol.recommend(req, state), state)
            lists[pol.name].append(items)
            if booster is not None:
                Xn = featurize(req, state, items, slot=list(range(len(items))), now=row["_t"])
                q_new[pol.name].append(rk.predict(booster, Xn))
            if sim is not None:
                truth[pol.name].append(sim.expected_purchase(req, items))
    marg = est.empirical_marginals(shown)
    out = {"n": len(shown), "observed_per100": 100 * float(np.mean([x is not None for x in purchased])),
           "policies": {}}
    for pol in policies:
        name = pol.name
        qn = np.array(q_new[name]) if booster is not None else None
        ql = np.array(q_logged) if booster is not None else None
        r_cond = est.estimate(lists[name], shown, props, purchased, qn, ql, marginals=None)
        r_emp = est.estimate(lists[name], shown, props, purchased, qn, ql, marginals=marg)
        row = {"ips_cond": r_cond["ips"], "snips_cond": r_cond["snips"], "ips_emp": r_emp["ips"],
               "ips_emp_ci": r_emp["ips_ci"], "snips_emp": r_emp["snips"], "snips_emp_ci": r_emp["snips_ci"],
               "ips_slot1": r_cond["ips_slot1"], "matches": r_emp["matches"],
               "hit@10_click": est.hit_rate(lists[name], clicked)[0]}
        if booster is not None:
            row.update(dm=r_emp["dm"], dr_emp=r_emp["dr"], dr_emp_ci=r_emp["dr_ci"], dr_cond=r_cond["dr"])
        if sim is not None:
            row["truth"] = 100 * float(np.mean(truth[name]))
        # slices: new shoppers (not seen before this moment), cold (organisers' definition), warm
        for sl, mask in (("new_user", newu), ("cold", cold), ("warm", [not x for x in cold])):
            idx = [i for i, m in enumerate(mask) if m]
            if not idx:
                continue
            sub = lambda xs: [xs[i] for i in idx]  # noqa: E731
            rs = est.estimate(sub(lists[name]), sub(shown), sub(props), sub(purchased), None, None, marginals=None)
            row[f"{sl}_n"] = len(idx)
            row[f"{sl}_ips_cond"] = rs["ips"]
            row[f"{sl}_ips_slot1"] = rs["ips_slot1"]
            row[f"{sl}_hit@10_click"] = est.hit_rate(sub(lists[name]), sub(clicked))[0]
        out["policies"][name] = row
    return out


def format_offline(rep: dict) -> str:
    cols = ["truth", "ips_cond", "ips_emp", "snips_emp", "dm", "dr_emp", "ips_slot1", "matches", "hit@10_click"]
    lines = [f"impressions={rep['n']} observed={rep['observed_per100']:.3f}/100 (old carousel)",
             f"{'policy':24s} " + " ".join(f"{c:>10s}" for c in cols)]
    for name, r in rep["policies"].items():
        lines.append(f"{name:24s} " + " ".join(f"{r.get(c, float('nan')):10.3f}" for c in cols))
    return "\n".join(lines)


def full_artefacts(catalog: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame) -> dict:
    """Artefacts from the whole history: what the live system loads (the ranker is trained separately)."""
    return build_offline(catalog, sessions, events)
