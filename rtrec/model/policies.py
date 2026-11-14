"""The three policies we serve, all implementing `rtrec.contract.Policy` (pure, State-only).

- BaselinePolicy          the organisers' baseline: all-time best sellers in stock. Score 0 by definition;
                          useful as a sanity check and as the anchor of the normalised score.
- SessionHeuristicPolicy  v1: hand-weighted score over the same candidates and features as v2. No
                          training needed, so it is also the safety net when the model is missing.
- RankerPolicy            v2: LightGBM P(purchase) over ~300 candidates, slot fixed to 0, then a light
                          re-rank (already-bought penalty, live calibration). Falls back to v1.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from rtrec.contract import State
from rtrec.schemas import Request

from . import ranker as rk
from .candidates import generate
from .features import F_INDEX as F
from .features import featurize


DEFAULT_MODEL = Path(os.environ.get("RTREC_MODEL", "artifacts/ranker.txt"))


def ensure_offline(state: State | None, dump=None, model_path: str | Path | None = None) -> None:
    """Fill state.offline once at start-up (never inside recommend): artefacts from the history dump
    if missing, and the ranker from `model_path` if it exists. Lets every policy be built as
    Policy(state, dump), the factory signature of scripts/arbiter.py."""
    if state is None:
        return
    if dump is not None and "prod" not in state.offline:
        from .artefacts import build_offline
        state.offline.update(build_offline(dump.catalog, dump.history_sessions, dump.history_events))
        if not state.safe_list:
            state.safe_list = [p for p in state.offline["best_sellers"] if p in state.available][:200]
        if not state.users:  # warm shoppers: profiles from the history (cheap path, no trends)
            from rtrec import live
            from .util import to_epoch
            prof = live.learner(state).profiles
            for ev in dump.history_events.to_dict("records"):
                prof.add(ev, to_epoch(ev["ts"]), state)
    path = Path(model_path) if model_path else DEFAULT_MODEL
    if "ranker" not in state.offline and path.exists():
        state.offline["ranker"] = rk.load(path)


class BaselinePolicy:
    name = "baseline"

    def __init__(self, state: State | None = None, dump=None):
        ensure_offline(state, dump, model_path="/nonexistent")

    def recommend(self, req: Request, state: State) -> list[str]:
        return list(state.offline.get("best_sellers") or state.safe_list)[:60]


def heuristic_scores(X: np.ndarray) -> np.ndarray:
    """log P(purchase) up to a constant, by hand: product quality x session affinity x trend.

    The weights are round numbers chosen from first principles (and checked on the simulator),
    not fitted; the ranker (v2) learns the real ones."""
    c = lambda name: X[:, F[name]]  # noqa: E731
    s = np.log(np.maximum(c("conv"), 1e-6)) + 0.7 * np.log(np.maximum(c("car_lift"), 1e-3))
    s += 1.2 * np.minimum(c("sub_views_w"), 2.0) + 0.4 * np.minimum(c("cat_views_w"), 2.0)
    s += 0.6 * (c("viewed_count") > 0) + 0.3 * np.log1p(c("coview")) + 0.2 * np.minimum(c("brand_views_w"), 2.0)
    s += 1.0 * c("user_sub_aff") + 0.5 * c("user_cat_aff") + 0.3 * c("user_carted")
    s -= 2.0 * c("user_bought")
    s += 0.3 * np.log(np.maximum(c("trend_prod"), 0.1)) + 0.2 * np.log(np.maximum(c("trend_sub"), 0.1))
    s -= 0.3 * np.abs(c("price_ratio_session")) * (c("n_views") > 0)
    return s


class SessionHeuristicPolicy:
    name = "v1-session-heuristic"

    def __init__(self, state: State | None = None, dump=None):
        ensure_offline(state, dump, model_path="/nonexistent")

    def recommend(self, req: Request, state: State) -> list[str]:
        cands = generate(req, state)
        if not cands:
            return list(state.safe_list)
        pids = list(cands)
        order = np.argsort(-heuristic_scores(featurize(req, state, cands)), kind="stable")
        return [pids[i] for i in order[:20]]


class RankerPolicy:
    """`state.offline["ranker"]` holds the LightGBM booster (see scripts/train_ranker.py)."""

    def __init__(self, state: State | None = None, dump=None, model_path: str | Path | None = None,
                 name: str = "v2-lgbm"):
        ensure_offline(state, dump, model_path)
        self.name = name
        self.fallback = SessionHeuristicPolicy()

    def scores(self, req: Request, state: State) -> tuple[list[str], np.ndarray]:
        booster = state.offline.get("ranker")
        cands = generate(req, state)
        pids = list(cands)
        X = featurize(req, state, cands)
        p = rk.predict(booster, X) if booster is not None else np.exp(heuristic_scores(X))
        # A4: observed/expected purchases from our own feedback, per product (shrunk to its subcategory)
        cp, cs = state.live.get("calib_product") or {}, state.live.get("calib_subcat") or {}
        if cp or cs:
            cat = state.catalog
            p = p * np.array([cp.get(q) or cs.get((cat.get(q) or {}).get("subcategory"), 1.0) for q in pids])
        return pids, p

    def recommend(self, req: Request, state: State) -> list[str]:
        if state.offline.get("ranker") is None:
            return self.fallback.recommend(req, state)
        pids, p = self.scores(req, state)
        if not pids:
            return list(state.safe_list)
        order = np.argsort(-p, kind="stable")
        return [pids[i] for i in order[:20]]
