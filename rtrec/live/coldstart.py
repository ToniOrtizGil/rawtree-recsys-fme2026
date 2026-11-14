"""Cold start: new shoppers (no history) and new products (no interactions).

The organisers call a request cold when `is_new_user` or when `recent_views` contains a product
added during the event; that slice is 14 % of the automatic score.

- New product -> content neighbours among established products: same subcategory, same brand,
  close price (log-ratio), overlapping description words. It inherits their live popularity as a
  prior (`new_product_prior`) until its own events arrive (trends pick it up within minutes).
  The reverse map (`similar_new`) lets a session on an old product surface its new look-alikes.
- New shopper without views -> what people with the same device and region are buying right now
  (`ctx_top`), then device-level, then the global trend list.
- New shopper with views -> the session is the only history: viewed items, their live co-views,
  their new look-alikes, then the same context lists.

All functions here are pure reads of State except `refresh_new_product_neighbours` (called by the
learner when catalogue updates arrive).
"""
from __future__ import annotations

import heapq
import math
import re

from rtrec.contract import State
from rtrec.schemas import Request

_WORD = re.compile(r"[a-záéíóúüñ]{3,}")
N_NEIGHBOURS = 10


def _tokens(text: str) -> frozenset:
    return frozenset(_WORD.findall((text or "").lower()))


def _subcat_index(state: State) -> dict:
    idx = state.live.get("_subcat_index")
    if idx is None or idx[0] != len(state.catalog):
        groups: dict = {}
        for pid, row in state.catalog.items():
            groups.setdefault(row.get("subcategory"), []).append(pid)
        idx = state.live["_subcat_index"] = (len(state.catalog), groups)
    return idx[1]


def similarity(a: dict, b: dict, ta: frozenset | None = None, tb: frozenset | None = None) -> float:
    """Content similarity in [0, ~4]: subcategory, brand, price, description words."""
    s = 0.0
    if a.get("subcategory") == b.get("subcategory"):
        s += 1.0
    elif a.get("category") == b.get("category"):
        s += 0.3
    if a.get("brand") and a.get("brand") == b.get("brand"):
        s += 1.0
    pa, pb = float(a.get("price_eur") or 0), float(b.get("price_eur") or 0)
    if pa > 0 and pb > 0:
        s += math.exp(-abs(math.log(pa / pb)) / 0.35)
    ta = _tokens(a.get("description", "")) if ta is None else ta
    tb = _tokens(b.get("description", "")) if tb is None else tb
    if ta and tb:
        s += len(ta & tb) / len(ta | tb)
    return s


def refresh_new_product_neighbours(state: State, only=None) -> None:
    """(Re)compute content neighbours for new products (all of them, or just `only`)."""
    new = state.new_products
    targets = [p for p in (only if only is not None else new) if p in new]
    if not targets:
        state.live.setdefault("new_neighbours", {})
        state.live.setdefault("similar_new", {})
        return
    idx = _subcat_index(state)
    nn = dict(state.live.get("new_neighbours") or {})
    rev = dict(state.live.get("similar_new") or {})
    cat = state.catalog
    for pid in targets:
        row = cat.get(pid)
        if row is None:
            continue
        tp = _tokens(row.get("description", ""))
        pool = [q for q in idx.get(row.get("subcategory"), []) if q != pid and q not in new]
        if len(pool) < N_NEIGHBOURS:   # tiny subcategory: widen to the category
            pool += [q for q, r in cat.items() if r.get("category") == row.get("category")
                     and q != pid and q not in new and q not in pool][:200]
        scored = heapq.nlargest(N_NEIGHBOURS, ((similarity(row, cat[q], tp), q) for q in pool))
        nn[pid] = tuple(q for _, q in scored)
        for _, q in scored[:5]:
            lst = [x for x in rev.get(q, ()) if x != pid]
            rev[q] = tuple([pid] + lst)[:5]
    state.live["new_neighbours"] = nn
    state.live["similar_new"] = rev


def new_product_prior(pid: str, state: State) -> float:
    """Expected live rate (weighted events/h) of a new product: its own if it has some, else its
    neighbours' average, shrunk by how young it is (its own counter takes over in ~minutes)."""
    rate = state.live.get("trend_rate") or {}
    own = rate.get(pid, 0.0)
    nb = (state.live.get("new_neighbours") or {}).get(pid, ())
    prior = sum(rate.get(q, 0.0) for q in nb) / len(nb) if nb else 0.0
    return max(own, 0.5 * prior)


def cold_candidates(req: Request, state: State, k: int = 100) -> list[str]:
    """Ordered candidate list for a (cold) request, built only from live state + catalogue."""
    live = state.live
    avail = state.available
    out: list[str] = []
    seen: set = set()

    def put(pids):
        for p in pids:
            if p not in seen and p in avail:
                seen.add(p)
                out.append(p)

    views = [v["product_id"] for v in sorted(req.get("recent_views") or [],
                                             key=lambda v: v.get("ts", ""), reverse=True)]
    nn = live.get("new_neighbours") or {}
    sim_new = live.get("similar_new") or {}
    cov = live.get("coview") or {}
    by_sub = live.get("trend_top_by_subcat") or {}
    for p in views:
        put([p])
        put(q for q, _ in cov.get(p, ())[:8])
        put(sim_new.get(p, ()))
        put(nn.get(p, ())[:6])
    for p in views[:3]:
        put(by_sub.get((state.catalog.get(p) or {}).get("subcategory"), [])[:10])
    ctx = req.get("context") or {}
    put((live.get("ctx_top") or {}).get((ctx.get("device"), ctx.get("region")), []))
    put((live.get("device_top") or {}).get((ctx.get("device"),), []))
    put(live.get("trend_top") or [])
    put(state.safe_list)
    return out[:k]
