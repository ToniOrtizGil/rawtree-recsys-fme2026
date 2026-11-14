"""Features for one (request, candidate product) pair. The same function serves training and live.

No product or user IDs: every feature is a relation between the shopper's session/profile/context
and the product's attributes, or a rate (conversion, position-debiased carousel lift, trend lift)
that a brand-new product also gets as soon as it collects events (or inherits from its
subcategory until then). That is what lets the model work on Sunday's unseen shoppers and products.
"""
from __future__ import annotations

import math

import numpy as np

from rtrec.contract import State
from rtrec.schemas import Request

from .artefacts import ProdStats, SubStats
from .util import context, to_epoch, view_ids

DEVICES = {"web": 0, "ios": 1, "android": 2}
PAGES = {"home": 0, "product": 1, "category": 2}

FEATURES = [
    # session relation
    "viewed_count", "viewed_recency_rank", "sub_views_w", "cat_views_w", "brand_views_w", "coview",
    "n_views", "price_ratio_session", "sources",
    # product attributes / priors
    "log_price", "price_rel_sub", "conv", "cart_rate", "car_lift", "log_car_imps", "log_views_pd",
    "log_purch_pd", "sub_conv", "sub_car_lift", "is_new_product", "age_days",
    # user profile (history + live)
    "user_sub_aff", "user_cat_aff", "user_bought", "user_bought_sub", "user_carted", "is_new_user",
    # context
    "device", "page", "hour",
    # live stream
    "trend_prod", "trend_sub", "log_live_pop", "log_buy_rate", "price_to_base", "price_last_change",
    # carousel slot (training: the logged slot; serving: 0)
    "slot",
]
CATEGORICAL = ["device", "page"]
F_INDEX = {f: i for i, f in enumerate(FEATURES)}
_EMPTY_P = ProdStats(0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
_EMPTY_S = SubStats(0.0, 0.0, 1.0, 0.0, 0.0)
DECAY = 0.7  # weight of the k-th most recent view = DECAY**k


def featurize(req: Request, state: State, cands: dict[str, int] | list[str], slot: int | list[int] = 0,
              now: float | None = None) -> np.ndarray:
    """Matrix (len(cands), len(FEATURES)), float32, rows in the order of `cands`."""
    pids = list(cands)
    srcs = cands if isinstance(cands, dict) else {}
    cat, off, live = state.catalog, state.offline, state.live
    prod_st, sub_st = off.get("prod", {}), off.get("sub", {})
    gconv = off.get("global_conv", 0.0)
    t = now if now is not None else to_epoch(req.get("ts"))
    device, _, page = context(req)
    dev_i, page_i = DEVICES.get(device, -1), PAGES.get(page, -1)
    hour = (t / 3600.0) % 24.0

    # --- session summary (once per request) ---
    views = view_ids(req)[::-1]
    vcount: dict[str, int] = {}
    vrank: dict[str, int] = {}
    sub_w: dict[str, float] = {}
    cat_w: dict[str, float] = {}
    brand_w: dict[str, float] = {}
    cov: dict[str, float] = {}
    vprices = []
    coview, live_cov = off.get("coview", {}), live.get("coview") or {}
    for k, v in enumerate(views):
        vcount[v] = vcount.get(v, 0) + 1
        vrank.setdefault(v, k)
        w = DECAY ** k
        p = cat.get(v)
        if p:
            sub_w[p["subcategory"]] = sub_w.get(p["subcategory"], 0.0) + w
            cat_w[p["category"]] = cat_w.get(p["category"], 0.0) + w
            brand_w[p["brand"]] = brand_w.get(p["brand"], 0.0) + w
            if p.get("price_eur"):
                vprices.append(float(p["price_eur"]))
        if k < 5:
            for r, (q, _) in enumerate(coview.get(v, ())):
                cov[q] = cov.get(q, 0.0) + w / (r + 1)
            for q, c in live_cov.get(v, ()):
                cov[q] = cov.get(q, 0.0) + w * 0.1 * c
    vmed = float(np.median(vprices)) if vprices else 0.0

    # --- user profile summary ---
    prof = state.users.get(req.get("user_id", ""))
    u_sub: dict[str, float] = {}
    u_cat: dict[str, float] = {}
    bought: set[str] = set()
    bought_sub: set[str] = set()
    carted: set[str] = set()
    if prof is not None:
        for lst, w in ((prof.views[-30:], 1.0), (prof.carts[-20:], 3.0), (prof.purchases[-20:], 3.0)):
            for _, pid in lst:
                p = cat.get(pid)
                if p:
                    u_sub[p["subcategory"]] = u_sub.get(p["subcategory"], 0.0) + w
                    u_cat[p["category"]] = u_cat.get(p["category"], 0.0) + w
        for _, pid in prof.purchases:
            bought.add(pid)
            p = cat.get(pid)
            if p:
                bought_sub.add(p["subcategory"])
        carted = {pid for _, pid in prof.carts}
    u_tot = sum(u_sub.values()) or 1.0
    new_user = 1.0 if req.get("is_new_user") else 0.0

    # live keys written by rtrec.live (docs/LIVE.md); all optional
    tp, tsub = live.get("trend_lift") or {}, live.get("subcat_lift") or {}
    pop, buy, prices = live.get("trend_rate") or {}, live.get("buy_rate") or {}, live.get("price") or {}
    neigh = live.get("new_neighbours") or {}
    new = state.new_products
    slots = slot if isinstance(slot, list) else [slot] * len(pids)

    X = np.zeros((len(pids), len(FEATURES)), dtype=np.float32)
    for i, pid in enumerate(pids):
        p = cat.get(pid) or {}
        sub, ca, br = p.get("subcategory", ""), p.get("category", ""), p.get("brand", "")
        price = float(p.get("price_eur") or 0.0) or 1.0
        ps = prod_st.get(pid, _EMPTY_P)
        ss = sub_st.get(sub, _EMPTY_S)
        seen = pid in prod_st
        if seen:
            conv, cart, lift = ps.conv, ps.cart_rate, ps.car_lift
        else:  # new product: borrow from its content neighbours (A4), else from its subcategory
            nb = [prod_st[q] for q in neigh.get(pid, ()) if q in prod_st]
            if nb:
                conv = sum(x.conv for x in nb) / len(nb)
                cart = sum(x.cart_rate for x in nb) / len(nb)
                lift = sum(x.car_lift for x in nb) / len(nb)
            else:
                conv, cart, lift = (ss.conv or gconv), ss.cart_rate, ss.car_lift
        pinfo = prices.get(pid)
        created = to_epoch(p.get("created_at")) if p.get("created_at") else 0.0
        X[i] = (
            vcount.get(pid, 0), vrank.get(pid, 99), sub_w.get(sub, 0.0), cat_w.get(ca, 0.0), brand_w.get(br, 0.0),
            cov.get(pid, 0.0), len(views), math.log(price / vmed) if vmed else 0.0, srcs.get(pid, 0),
            math.log(price), math.log(price / ss.median_price) if ss.median_price else 0.0,
            conv, cart, lift,
            math.log1p(ps.car_imps), math.log1p(ps.views_pd), math.log1p(ps.purch_pd),
            ss.conv, ss.car_lift, 1.0 if pid in new else 0.0, (t - created) / 86400.0 if created else -1.0,
            u_sub.get(sub, 0.0) / u_tot, u_cat.get(ca, 0.0) / u_tot, 1.0 if pid in bought else 0.0,
            1.0 if sub in bought_sub else 0.0, 1.0 if pid in carted else 0.0, new_user,
            dev_i, page_i, hour,
            tp.get(pid, 1.0), tsub.get(sub, 1.0), math.log1p(pop.get(pid, 0.0)), math.log1p(buy.get(pid, 0.0)),
            pinfo.ratio_to_base if pinfo else 1.0, pinfo.last_change_ratio if pinfo else 1.0,
            slots[i],
        )
    return X
