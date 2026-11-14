"""Training rows for the ranker: every logged (impression, slot, product) with its purchase label.

Features are computed with `rtrec.model.features.featurize` on a State replayed *up to the
impression's timestamp* (events before it, nothing after), so training sees exactly what the live
system would have seen. Offline artefacts (priors, co-views) must come from an earlier window than
the impressions used here, otherwise the priors already "know" the labels (leakage).

Split used by scripts/train_ranker.py (fractions of the history span):
    [0, a)   artefacts window: priors and co-views
    [a, b)   ranker training impressions (features as of each impression)
    [b, 1]   test: estimator checks and simulator-free validation
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from rtrec.contract import State
from rtrec.model.features import featurize
from rtrec.model.util import clean_id, to_epoch
from rtrec.schemas import Request

from .replay import LiveFeed


def session_to_request(row: dict, state: State) -> Request:
    """history_sessions row -> Request. is_new_user is not logged: a user with no profile yet counts as new."""
    return {"request_id": str(row["request_id"]), "ts": row["ts"], "user_id": str(row["user_id"]),
            "is_new_user": str(row["user_id"]) not in state.users,
            "context": row["context"], "recent_views": list(row["recent_views"] or [])}


def time_cuts(sessions: pd.DataFrame, a: float = 0.5, b: float = 0.85) -> tuple[float, float, float, float]:
    e = sessions["ts"].map(to_epoch)
    t0, t1 = float(e.min()), float(e.max())
    return t0, t0 + a * (t1 - t0), t0 + b * (t1 - t0), t1


def build_rows(state: State, feed: LiveFeed, sessions: pd.DataFrame, events: pd.DataFrame,
               t_from: float, t_to: float, sample: float = 1.0, seed: int = 0,
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Replay events in time order and featurize sessions with t_from <= ts < t_to.

    `state`/`feed` must already contain everything before t_from. Returns X, y (purchase), group
    (index of the impression, for per-impression metrics)."""
    rng = np.random.default_rng(seed)
    ev = events.assign(_t=events["ts"].map(to_epoch))
    ev = ev[(ev._t >= t_from) & (ev._t < t_to)].sort_values("_t")
    se = sessions.assign(_t=sessions["ts"].map(to_epoch))
    se = se[(se._t >= t_from) & (se._t < t_to)].sort_values("_t")
    ev_recs = ev.drop(columns="_t").to_dict("records")
    ev_t = ev._t.to_numpy()
    Xs, ys, gs = [], [], []
    j = 0
    for g, row in enumerate(se.to_dict("records")):
        t = row["_t"]
        k = int(np.searchsorted(ev_t, t, side="left"))
        if k > j:
            feed.on_events(ev_recs[j:k])
            j = k
        if sample < 1.0 and rng.random() > sample:
            continue
        req = session_to_request(row, state)
        shown = [str(p) for p in list(row["shown"])[:10]]
        X = featurize(req, state, shown, slot=list(range(len(shown))), now=t)
        purchased = clean_id(row.get("purchased"))
        Xs.append(X)
        ys.append(np.array([1.0 if purchased and p == purchased else 0.0 for p in shown]))
        gs.append(np.full(len(shown), g))
    if j < len(ev_recs):
        feed.on_events(ev_recs[j:])
    if not Xs:
        from rtrec.model.features import FEATURES
        return np.zeros((0, len(FEATURES)), np.float32), np.zeros(0), np.zeros(0, int)
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(gs)
