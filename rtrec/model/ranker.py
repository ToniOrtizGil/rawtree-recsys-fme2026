"""LightGBM point-wise model of P(purchase | request, product, slot), trained on carousel logs.

Why point-wise purchase probability: the score is expected purchases, the first slots are looked at
most, so sorting candidates by P(purchase) puts the best where the attention is. The slot is a
feature during training (the log shows items in all 10 slots) and is fixed to 0 when serving, so
the position bias is learnt instead of polluting the product signal.

The exported artefact is LightGBM's text model plus a small JSON with the feature list, so the
live process can check it was trained with the same features it computes.
"""
from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np

from .features import CATEGORICAL, FEATURES

DEFAULT_PARAMS = {
    "objective": "binary", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 50,
    "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
    "verbose": -1, "num_threads": 4,
}


def train(X: np.ndarray, y: np.ndarray, X_val: np.ndarray | None = None, y_val: np.ndarray | None = None,
          params: dict | None = None, rounds: int = 400, weight: np.ndarray | None = None) -> lgb.Booster:
    p = {**DEFAULT_PARAMS, **(params or {})}
    dtrain = lgb.Dataset(X, y, weight=weight, feature_name=FEATURES, categorical_feature=CATEGORICAL,
                         free_raw_data=False)
    valid, cbs = [], []
    if X_val is not None and len(X_val):
        valid = [lgb.Dataset(X_val, y_val, reference=dtrain)]
        cbs = [lgb.early_stopping(40, verbose=False)]
    return lgb.train(p, dtrain, num_boost_round=rounds, valid_sets=valid, callbacks=cbs)


def save(booster: lgb.Booster, path: str | Path, meta: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(path))
    Path(str(path) + ".json").write_text(json.dumps({"features": FEATURES, **(meta or {})}, indent=2))


def load(path: str | Path) -> lgb.Booster:
    meta = json.loads(Path(str(path) + ".json").read_text())
    if meta["features"] != FEATURES:
        raise ValueError(f"model {path} was trained with different features; retrain it")
    return lgb.Booster(model_file=str(path))


def predict(booster: lgb.Booster, X: np.ndarray) -> np.ndarray:
    # num_threads=1: a single request is tiny, thread start-up would cost more than it saves
    return booster.predict(X, num_threads=1)
