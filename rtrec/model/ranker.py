"""v2 model: P(purchase | request, product, slot) = P(click | ..., slot) x P(buy | click, ...).

Why two LightGBM models instead of one: purchases are rare (about 1 impression in 80 on the
simulator), clicks are ~10x more common. The click model learns "would they pick it" from many
positives, including the position bias through the `slot` feature; the buy model learns price
sensitivity, quality and the like from clicked rows only, where the slot no longer matters.
Serving fixes slot = 0 for the click model, so products are ranked by their own merit and the best
go where attention is highest.

Why point-wise: the score is expected purchases = sum over slots of attention x P(purchase), so
sorting by P(purchase) is the optimal order whenever items do not compete much.

Artefact on disk: `ranker.txt` (click model) + `ranker.buy.txt` (buy model) + `ranker.txt.json`
(feature list + training metrics). The feature list is checked at load time against the code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np

from .features import CATEGORICAL, F_INDEX, FEATURES

DEFAULT_PARAMS = {
    "objective": "binary", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 100,
    "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0,
    "verbose": -1, "num_threads": 4,
}
SLOT = F_INDEX["slot"]


def train_booster(X: np.ndarray, y: np.ndarray, X_val: np.ndarray | None = None, y_val: np.ndarray | None = None,
                  params: dict | None = None, rounds: int = 600, weight: np.ndarray | None = None) -> lgb.Booster:
    p = {**DEFAULT_PARAMS, **(params or {})}
    dtrain = lgb.Dataset(X, y, weight=weight, feature_name=FEATURES, categorical_feature=CATEGORICAL,
                         free_raw_data=False)
    valid, cbs = [], []
    if X_val is not None and len(X_val) and 0 < y_val.sum() < len(y_val):
        valid = [lgb.Dataset(X_val, y_val, reference=dtrain)]
        cbs = [lgb.early_stopping(50, verbose=False)]
    return lgb.train(p, dtrain, num_boost_round=rounds, valid_sets=valid, callbacks=cbs)


@dataclass
class Ranker:
    click: lgb.Booster
    buy: lgb.Booster | None = None    # None: `click` was trained on purchases directly

    def predict(self, X: np.ndarray) -> np.ndarray:
        """P(purchase) per row. Rows carry their slot in the `slot` column (serving: 0)."""
        # num_threads=1: a single request is tiny, thread start-up would cost more than it saves
        pc = self.click.predict(X, num_threads=1)
        if self.buy is None:
            return pc
        Xb = X
        if X[:, SLOT].any():
            Xb = X.copy()
            Xb[:, SLOT] = 0.0
        return pc * self.buy.predict(Xb, num_threads=1)


def train(X: np.ndarray, y_buy: np.ndarray, y_click: np.ndarray, val: np.ndarray, rounds: int = 600,
          fixed_rounds: tuple[int, int] | None = None, weight: np.ndarray | None = None) -> tuple[Ranker, dict]:
    """val: boolean mask of validation rows (the latest impressions). With no validation rows (final refit
    on everything), `fixed_rounds` = (click, buy) iterations found by early stopping in the validated run."""
    tr = ~val
    rc, rb = fixed_rounds if (fixed_rounds and not val.any()) else (rounds, rounds)
    w = weight if weight is not None else None
    click = train_booster(X[tr], y_click[tr], X[val], y_click[val], rounds=rc,
                          weight=None if w is None else w[tr])
    cl = y_click > 0
    Xc = X[cl].copy()
    Xc[:, SLOT] = 0.0
    buy = train_booster(Xc[tr[cl]], y_buy[cl][tr[cl]], Xc[val[cl]], y_buy[cl][val[cl]], rounds=rb,
                        weight=None if w is None else w[cl][tr[cl]],
                        params={"min_data_in_leaf": 30, "num_leaves": 15})
    return Ranker(click, buy), {"click_best_iter": click.best_iteration or rc, "buy_best_iter": buy.best_iteration or rb,
                                "clicked_rows": int(cl.sum())}


def save(model: Ranker, path: str | Path, meta: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    model.click.save_model(str(path))
    if model.buy is not None:
        model.buy.save_model(str(_buy_path(path)))
    Path(str(path) + ".json").write_text(json.dumps({"features": FEATURES, "features_code": features_code_hash(),
                                                     "two_stage": model.buy is not None,
                                                     **(meta or {})}, indent=2, default=float))


def load(path: str | Path) -> Ranker:
    path = Path(path)
    meta = json.loads(Path(str(path) + ".json").read_text())
    if meta["features"] != FEATURES:
        raise ValueError(f"model {path} was trained with different features; retrain it")
    if meta.get("features_code") and meta["features_code"] != features_code_hash():
        raise ValueError(f"model {path} was trained with a different featurize() code; retrain it")
    buy = lgb.Booster(model_file=str(_buy_path(path))) if meta.get("two_stage") else None
    return Ranker(lgb.Booster(model_file=str(path)), buy)


def predict(model: Ranker, X: np.ndarray) -> np.ndarray:
    return model.predict(X)


def features_code_hash() -> str:
    """Same feature names can hide a changed computation: pin the code that builds the rows too."""
    import hashlib
    here = Path(__file__).parent
    return hashlib.sha256(b"".join((here / f).read_bytes() for f in ("features.py", "candidates.py"))).hexdigest()[:16]


def _buy_path(path: Path) -> Path:
    return path.with_name(path.stem + ".buy" + path.suffix)
