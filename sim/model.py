"""The hidden purchase model: P(click), P(purchase | click) for (request, product) pairs.

Carousel model ("examination x attraction", at most one click per impression):

    P(click on slot k) = examine[k] * c_i,   c_i = sigmoid(u_i)      (sum of examine <= 1)
    P(purchase | click on i) = b_i,          b_i = sigmoid(v_i)
    expected purchases of a list = sum_k examine[k] * c_{i_k} * b_{i_k}

The expectation is linear in per-item scores p_i = c_i * b_i with decreasing slot weights, so the
oracle (best possible list) is simply the 10 available products with the largest p_i, sorted.
u_i mixes: product appeal, long-term taste, the session's hidden intent, observable recent views,
region/device tastes, novelty, relative price, price drops, trends and a repeat-purchase penalty.
v_i carries the price sensitivity, which depends on device, region and the shopper.
All weights live in sim/params.py (HiddenModel).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .params import HiddenModel
from .world import World


@dataclass
class Sessions:
    """A batch of B shopper sessions (the hidden side of a request). views are product indices, -1 = pad,
    in chronological order (last = most recent)."""
    user: np.ndarray        # (B,) int
    region: np.ndarray      # (B,) int
    device: np.ndarray      # (B,) int  index into DEVICES
    page: np.ndarray        # (B,) int  index into PAGES
    intent: np.ndarray      # (B,) int  subcategory the shopper is really after (hidden)
    t: np.ndarray           # (B,) float epoch seconds
    views: np.ndarray       # (B, V) int

    def row(self, i: int) -> "Sessions":
        return Sessions(*(getattr(self, f)[i: i + 1] for f in self.__dataclass_fields__))

    @staticmethod
    def concat(parts: list["Sessions"]) -> "Sessions":
        V = max(p.views.shape[1] for p in parts)
        views = [np.pad(p.views, ((0, 0), (0, V - p.views.shape[1])), constant_values=-1) for p in parts]
        return Sessions(*(np.concatenate([getattr(p, f) for p in parts]) for f in
                          ("user", "region", "device", "page", "intent", "t")), np.concatenate(views))


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


class PurchaseModel:
    def __init__(self, world: World, hidden: HiddenModel):
        self.w = world
        self.h = hidden
        self.examine = np.asarray(hidden.examine)
        self._page_mult = np.array([dict(hidden.page_intent_mult)[p] for p in ("home", "product", "category")])
        self._alpha_dev = np.array([dict(hidden.alpha_device)[d] for d in ("web", "ios", "android")])
        # user -> {product index: first purchase ts}; fed by history, live organic purchases and judged carousel purchases
        self.ledger: dict[int, dict[int, float]] = {}

    # ------------------------------------------------------------------ ledger
    def record_purchase(self, user: int, item: int, t: float) -> None:
        d = self.ledger.setdefault(int(user), {})
        if item not in d or t < d[item]:
            d[int(item)] = float(t)

    def _bought_before(self, s: Sessions, items: np.ndarray) -> np.ndarray:
        out = np.zeros(items.shape, bool)
        for b in range(items.shape[0]):
            d = self.ledger.get(int(s.user[b]))
            if not d:
                continue
            tb = s.t[b]
            bought = [i for i, ts in d.items() if ts < tb]
            if bought:
                out[b] = np.isin(items[b], bought)
        return out

    # ------------------------------------------------------------------ pieces
    def trend_bonus(self, t: np.ndarray, sub: np.ndarray, brand: np.ndarray) -> np.ndarray:
        """Sum of active trend log-lifts for items with subcategory `sub` and brand `brand` (B, M)."""
        tr = self.w.trends
        out = np.zeros(sub.shape)
        for j in tr.active_between(float(t.min()), float(t.max())):
            amp = tr.amplitude(j, t)[:, None]
            out += amp * ((sub if tr.kind[j] == 0 else brand) == tr.target[j])
        return out

    def trend_bonus_shared(self, t: np.ndarray, sub: np.ndarray, brand: np.ndarray) -> np.ndarray:
        """Same as trend_bonus when all B sessions score the same M items (sub, brand: (M,))."""
        tr = self.w.trends
        js = tr.active_between(float(t.min()), float(t.max()))
        if len(js) == 0:
            return np.zeros((len(t), len(sub)))
        amp = np.stack([tr.amplitude(j, t) for j in js], 1)                        # (B, J)
        match = np.stack([(sub if tr.kind[j] == 0 else brand) == tr.target[j] for j in js]).astype(float)
        return amp @ match

    def sub_trend(self, t: np.ndarray) -> np.ndarray:
        """(B, n_sub) trend log-lift per subcategory (drives which subcategory shoppers come for)."""
        tr = self.w.trends
        out = np.zeros((len(t), len(self.w.sub_names)))
        for j in tr.active_between(float(t.min()), float(t.max())):
            if tr.kind[j] == 0:
                out[:, tr.target[j]] += tr.amplitude(j, t)
        return out

    def prices(self, t: np.ndarray, items: np.ndarray) -> np.ndarray:
        P = self.w.products
        live = (t >= self.w.cfg.stream_start)[:, None]
        return np.where(live, P.price[items], P.price0[items])

    # ------------------------------------------------------------------ the model
    def scores(self, s: Sessions, items: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(c, b): click attraction and purchase-given-click probabilities, both (B, M).
        items: (B, M) product indices; entries < 0 get c = b = 0."""
        h, w, P = self.h, self.w, self.w.products
        valid = items >= 0
        it = np.where(valid, items, 0)
        sub, cat, brand = P.sub[it], P.cat[it], P.brand[it]
        th = w.users.theta[s.user]                                    # (B, D)
        taste = np.einsum("bd,bmd->bm", th, P.z[it])
        isub = sub == s.intent[:, None]
        icat = cat == w.sub_cat[s.intent][:, None]
        pm = self._page_mult[s.page][:, None]
        # observable session: recency-weighted shares of recent views in the same subcategory / brand
        V = s.views
        vmask = V >= 0
        Vc = np.where(vmask, V, 0)
        rank_from_end = np.cumsum(vmask[:, ::-1], axis=1)[:, ::-1] - 1  # 0 for most recent view
        rw = np.where(vmask, 0.7 ** rank_from_end, 0.0)
        rw = rw / np.maximum(rw.sum(1, keepdims=True), 1e-9)
        vsub, vbrand = P.sub[Vc], P.brand[Vc]
        share_sub = np.einsum("bv,bmv->bm", rw, (sub[:, :, None] == vsub[:, None, :]))
        share_brand = np.einsum("bv,bmv->bm", rw, (brand[:, :, None] == vbrand[:, None, :]))
        viewed = ((it[:, :, None] == V[:, None, :]) & vmask[:, None, :]).any(-1)
        ctx = w.region_cat[s.region[:, None], cat] + w.device_cat[s.device[:, None], cat]
        age = np.maximum(s.t[:, None] - P.created[it], 0.0)
        novelty = np.exp(-age / h.tau_new_s)
        price = self.prices(s.t, it)
        relp = np.log(price / w.sub_median_price[sub])
        drop = np.maximum(np.log(P.price0[it] / price), 0.0)
        rep = self._bought_before(s, it)
        u = (h.click_bias + h.w_quality * P.q[it] + h.w_taste * taste
             + pm * (h.w_intent_sub * isub + h.w_intent_cat * icat)
             + h.w_view_sub * share_sub + h.w_view_brand * share_brand + h.w_viewed * viewed
             + h.w_context * ctx + h.w_new * novelty - h.w_price_click * relp + h.w_price_drop * drop
             + h.w_repeat * rep + self.trend_bonus(s.t, sub, brand))
        alpha = (h.alpha_base + self._alpha_dev[s.device] + w.alpha_region[s.region]
                 + w.users.alpha_noise[s.user])[:, None]
        v = h.buy_bias + h.w_buy_quality * P.q[it] - alpha * np.log(price / h.price_ref_eur) + h.w_buy_price_drop * drop
        c, b = sigmoid(u), sigmoid(v)
        return np.where(valid, c, 0.0), np.where(valid, b, 0.0)

    def expected(self, s: Sessions, items: np.ndarray) -> np.ndarray:
        """Expected purchases of each (ordered) list of up to 10 items, (B,)."""
        items = items[:, : len(self.examine)]
        c, b = self.scores(s, items)
        return (c * b * self.examine[: items.shape[1]]).sum(1)

    def best(self, s: Sessions, available: np.ndarray, k: int = 10) -> np.ndarray:
        """Oracle: top-k available products by p = c*b for each session, (B, k) product indices."""
        cand = np.nonzero(available)[0]
        items = np.broadcast_to(cand, (len(s.user), len(cand)))
        c, b = self.scores(s, items)
        p = c * b
        top = np.argpartition(-p, k, axis=1)[:, :k]
        order = np.take_along_axis(p, top, 1).argsort(1)[:, ::-1]
        return cand[np.take_along_axis(top, order, 1)]
