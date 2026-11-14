"""Candidate generation: a few hundred plausible products per request, cheaply, from State only.

The ranker can only pick what is offered here, so the sources cover every reason a shopper might
buy: what they just looked at, what people look at together, the best of the subcategories and
brands in play, what they liked before, what is hot right now, what sells on this device, and
brand-new products that resemble the session (cold start: new products have no co-views yet).
Each candidate carries a bitmask of the sources that proposed it; the ranker uses it as a feature.
"""
from __future__ import annotations

from rtrec.contract import State
from rtrec.schemas import Request

from .util import context, view_ids

SRC_VIEWED, SRC_COVIEW, SRC_SUB, SRC_BRAND, SRC_USER, SRC_TREND, SRC_CTX, SRC_NEW, SRC_BEST = (1 << i for i in range(9))
N_CANDIDATES = 300


def new_in_subcategories(state: State, subs: list[str]) -> dict[str, list[str]]:
    """New products (added during the event) grouped by subcategory, only for `subs`.
    A linear scan: there are at most a few hundred new products in a 2-hour round."""
    want, out = set(subs), {}
    for p in state.new_products:
        sub = (state.catalog.get(p) or {}).get("subcategory")
        if sub in want:
            out.setdefault(sub, []).append(p)
    return out


def generate(req: Request, state: State, n: int = N_CANDIDATES) -> dict[str, int]:
    """{product_id: source bitmask}, only available products, at most ~n entries."""
    avail, cat, off, live = state.available, state.catalog, state.offline, state.live
    out: dict[str, int] = {}

    def add(pids, flag: int, limit: int) -> None:
        k = 0
        for p in pids:
            if k >= limit or len(out) >= n:
                return
            if p in avail:
                out[p] = out.get(p, 0) | flag
                k += 1

    views = view_ids(req)[::-1]  # most recent first
    add(views, SRC_VIEWED, 30)

    cov, live_cov = off.get("coview", {}), live.get("coview") or {}
    similar_new, neighbours = live.get("similar_new") or {}, live.get("new_neighbours") or {}
    for v in views[:5]:
        add((q for q, _ in cov.get(v, ())), SRC_COVIEW, 20)
        add((q for q, _ in live_cov.get(v, ())), SRC_COVIEW, 10)
        add(similar_new.get(v, ()), SRC_NEW, 5)      # new look-alikes of what they viewed (A4)
        add(neighbours.get(v, ()), SRC_SUB, 6)       # viewed a new product: its content neighbours

    subs, brands = [], []
    for v in views[:5]:
        p = cat.get(v)
        if p:
            if p["subcategory"] not in subs:
                subs.append(p["subcategory"])
            if p["brand"] not in brands:
                brands.append(p["brand"])

    prof = state.users.get(req.get("user_id", ""))
    if prof is not None:
        recent = [pid for _, pid in (prof.carts[-10:] + prof.views[-10:])][::-1]
        add(recent, SRC_USER, 15)
        for pid in recent[:10]:
            p = cat.get(pid)
            if p and p["subcategory"] not in subs:
                subs.append(p["subcategory"])

    sub_top, newsub = off.get("sub_top", {}), new_in_subcategories(state, subs[:6])
    live_sub = live.get("trend_top_by_subcat") or {}
    for i, s in enumerate(subs[:6]):
        add(sub_top.get(s, ()), SRC_SUB, 25 if i < 2 else 10)
        add(live_sub.get(s, ()), SRC_TREND, 10 if i < 2 else 5)
        add(newsub.get(s, ()), SRC_NEW, 10)
    if brands:
        bset = set(brands[:3])
        add((p for s in subs[:3] for p in sub_top.get(s, ()) if cat.get(p, {}).get("brand") in bset), SRC_BRAND, 10)

    add(live.get("trend_top") or (), SRC_TREND, 30)
    device, region, _ = context(req)
    add((live.get("ctx_top") or {}).get((device, region), ()), SRC_CTX, 20)
    add(off.get("ctx_top", {}).get(device, ()), SRC_CTX, 30)
    add(state.safe_list, SRC_BEST, 40)
    if len(out) < 20:
        add(sorted(avail), SRC_BEST, 50)  # nearly empty State: anything valid
    return out
