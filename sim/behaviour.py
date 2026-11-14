"""How synthetic shoppers browse: session intent, which products they view, carts and purchases,
and the old "best sellers + randomness" carousel that produced shop.history_sessions.

All functions are vectorised over a batch of sessions and take an explicit RNG, so the same seed
always gives the same shop.
"""
from __future__ import annotations

import numpy as np

from .model import PurchaseModel, Sessions
from .world import World

MAX_VIEWS = 10


def pick_intent(w: World, m: PurchaseModel, user: np.ndarray, region: np.ndarray, t: np.ndarray,
                rng: np.random.Generator) -> np.ndarray:
    """Subcategory each session is really after: taste, regional taste and trends (hidden)."""
    logits = (m.h.w_taste * 1.4 * (w.users.theta[user] @ w.sub_z.T)
              + m.h.w_context * w.region_cat[region][:, w.sub_cat] + m.sub_trend(t))
    g = rng.gumbel(size=logits.shape)
    return (logits + g).argmax(1)


def sample_views(w: World, m: PurchaseModel, user: np.ndarray, region: np.ndarray, device: np.ndarray,
                 intent: np.ndarray, t: np.ndarray, n_views: np.ndarray, rng: np.random.Generator,
                 stray_share: float = 0.1) -> np.ndarray:
    """(B, MAX_VIEWS) viewed product indices (chronological, -1 padded).

    Sessions stay inside the category of their intent: views are drawn without replacement
    (Gumbel top-k) from a softmax over that category's available products, with a bonus for the
    intent subcategory. A share of views "strays" to a popular product anywhere in the shop.
    Working per category keeps this at B x ~500 instead of B x 5000."""
    h, P = m.h, w.products
    B, N = len(user), P.n
    out = np.full((B, MAX_VIEWS), -1, dtype=np.int64)
    avail = w.available(float(t.min()))  # products created later in the batch window are not viewed yet
    icat = w.sub_cat[intent]
    for c in np.unique(icat):
        rows = np.nonzero(icat == c)[0]
        items = np.nonzero(avail & (P.cat[:N] == c))[0]
        if len(items) < MAX_VIEWS:
            continue
        isub, ibrand = P.sub[items], P.brand[items]
        tt = t[rows]
        lg = (h.w_quality * P.q[items][None, :] + h.w_taste * (w.users.theta[user[rows]] @ P.z[items].T)
              + h.view_intent_sub * (isub[None, :] == intent[rows][:, None]) + h.view_intent_cat
              + h.w_context * (w.region_cat[region[rows], c] + w.device_cat[device[rows], c])[:, None]
              + m.trend_bonus_shared(tt, isub, ibrand))
        if np.any(tt >= w.cfg.stream_start):
            age = np.maximum(tt[:, None] - P.created[items][None, :], 0)
            lg = lg + h.w_new * np.exp(-age / h.tau_new_s)
        lg = lg + rng.gumbel(size=lg.shape)
        top = np.argpartition(-lg, MAX_VIEWS, axis=1)[:, :MAX_VIEWS]
        top = np.take_along_axis(top, np.take_along_axis(lg, top, 1).argsort(1)[:, ::-1], 1)
        out[rows] = items[top]
    # strays: a popular product from anywhere
    pop = np.nonzero(avail)[0]
    pw = np.exp(h.w_quality * P.q[pop])
    stray = rng.random(out.shape) < stray_share
    out = np.where(stray, rng.choice(pop, size=out.shape, p=pw / pw.sum()), out)
    keep = np.arange(MAX_VIEWS)[None, :] < n_views[:, None]
    return np.where(keep & (out >= 0), out, -1)


def view_times(start: np.ndarray, views: np.ndarray, rng: np.random.Generator, mean_gap_s: float = 25.0) -> np.ndarray:
    gaps = rng.exponential(mean_gap_s, size=views.shape)
    gaps[:, 0] = rng.uniform(0, 2, size=len(start))
    return np.where(views >= 0, start[:, None] + np.cumsum(gaps, 1), np.nan)


def organic_actions(m: PurchaseModel, s: Sessions, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """For every view of every organic session: (carted, purchased) booleans, (B, V)."""
    c, b = m.scores(s, s.views)
    cart = rng.random(c.shape) < np.minimum(0.9, m.h.organic_cart_mult * c)
    buy = cart & (rng.random(c.shape) < np.minimum(0.9, m.h.organic_buy_mult * b))
    return cart & (s.views >= 0), buy & (s.views >= 0)


def pick_page(n_views: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """0 = home, 1 = product, 2 = category (index into world.PAGES)."""
    r = rng.random(len(n_views))
    with_views = np.where(r < 0.6, 1, np.where(r < 0.85, 2, 0))
    without = np.where(r < 0.7, 0, 2)
    return np.where(n_views > 0, with_views, without)


class OldCarousel:
    """The logging policy of shop.history_sessions: Plackett-Luce over best-seller ranks plus a floor
    (so every available product has some chance). Records the exact propensity of each product in
    its slot given the slots above: w_i / (sum of w over available products not yet placed)."""

    def __init__(self, rank_power: float, floor: float):
        self.rank_power = rank_power
        self.floor = floor

    def weights(self, bestseller_score: np.ndarray, available: np.ndarray) -> np.ndarray:
        order = np.argsort(-np.where(available, bestseller_score, -np.inf), kind="stable")
        rank = np.empty(len(order))
        rank[order] = np.arange(1, len(order) + 1)
        return np.where(available, rank ** -self.rank_power + self.floor, 0.0)

    def sample(self, weights: np.ndarray, B: int, rng: np.random.Generator, k: int = 10,
               draws: int = 40) -> tuple[np.ndarray, np.ndarray]:
        """Plackett-Luce without replacement = sequential draws with replacement, skipping repeats
        (exact). Draw `draws` at once per row and keep the first k distinct."""
        cand = np.nonzero(weights > 0)[0]
        cdf = np.cumsum(weights[cand])
        W = cdf[-1]
        shown = np.empty((B, k), dtype=np.int64)
        draw = lambda n: cand[np.minimum(np.searchsorted(cdf, rng.uniform(0, W, (n, draws))), len(cand) - 1)]
        for r, row in enumerate(draw(B)):
            while True:
                _, first = np.unique(row, return_index=True)
                if len(first) >= k:
                    shown[r] = row[np.sort(first)[:k]]
                    break
                row = np.concatenate([row, draw(1)[0]])  # rare: keep drawing (stays exact)
        w = weights[shown]
        placed = np.concatenate([np.zeros((B, 1)), np.cumsum(w, 1)[:, :-1]], 1)
        return shown, w / (W - placed)
