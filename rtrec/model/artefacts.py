"""Offline artefacts: statistics computed once from history and put into `state.offline`.

Everything here is a *relation* or a *rate*, never a learned per-ID weight inside the model:
the ranker sees "this product converts 1.4x better than its slot predicts" or "co-viewed with
what you just looked at", which also exists for tomorrow's products once they collect events.
Counts are normalised per day so the same features work whether the history covers 7 or 60 days.

Keys written into the returned dict (see docs/MODEL.md for the full contract):
  best_sellers  list[pid]                        all-time purchases desc (= organisers' baseline order)
  prod          dict[pid, ProdStats]             product rates (events + carousel)
  sub           dict[subcategory, SubStats]      same, pooled per subcategory (prior for new products)
  sub_top       dict[subcategory, list[pid]]     best products of each subcategory, best first
  ctx_top       dict[device, list[pid]]          best products per device
  coview        dict[pid, list[(pid, weight)]]   top-20 products seen in the same sessions
  slot_rate     list[float]                      purchase rate per carousel slot (position bias)
  ctx_cat       dict[(region|device, category), float]  log lift of buying intent for a category in a context
  ctx_price     dict[region|device, float]       mean log price bought minus mean log price viewed (price appetite)
  idf           dict[token, float]               inverse document frequency of description words
  tokens        dict[pid, frozenset[str]]        description words of every catalogue product
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .util import clean_id, to_epoch

_STOP = frozenset("a an the and or of for with by in on to is it this that from at as its your our".split())


def tokenize(text: str) -> frozenset[str]:
    """Lower-case alphabetic words of a description, minus stop words (content signal, no IDs)."""
    out, w = [], []
    for ch in (text or "").lower():
        if ch.isalpha():
            w.append(ch)
        elif w:
            out.append("".join(w))
            w = []
    if w:
        out.append("".join(w))
    return frozenset(t for t in out if len(t) > 2 and t not in _STOP)


def content_index(catalog: pd.DataFrame) -> tuple[dict[str, frozenset[str]], dict[str, float]]:
    tokens = {str(p): tokenize(d) for p, d in zip(catalog.product_id, catalog.description.fillna(""))}
    df: dict[str, int] = {}
    for ts in tokens.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    n = max(len(tokens), 1)
    return tokens, {t: float(np.log((n + 1) / (c + 1))) for t, c in df.items()}


def context_stats(events: pd.DataFrame, catalog: pd.DataFrame) -> tuple[dict, dict]:
    """How much more a region / device engages with each category than the shop overall (log lift,
    shrunk), and its price appetite. Both are relations of the context, not of any shopper or product."""
    if events.empty:
        return {}, {}
    cat = catalog.assign(product_id=catalog.product_id.astype(str)).set_index("product_id")
    e = events.assign(product_id=events.product_id.astype(str))
    e = e.join(cat[["category"]], on="product_id")
    w = e.event_type.map({"view": 1.0, "add_to_cart": 3.0, "purchase": 6.0}).fillna(1.0)
    e = e.assign(w=w, lp=np.log(e.price_eur.astype(float).clip(lower=0.5)))
    glob = e.groupby("category").w.sum()
    glob = glob / glob.sum()
    ctx_cat, ctx_price = {}, {}
    gb = e[e.event_type == "purchase"].lp.mean() - e[e.event_type == "view"].lp.mean()
    for col in ("region", "device"):
        tab = e.groupby([col, "category"]).w.sum().unstack(fill_value=0.0)
        for key, row in tab.iterrows():
            share = (row + 20 * glob.reindex(row.index).fillna(0)) / (row.sum() + 20)
            for c, v in share.items():
                g = glob.get(c, 0.0)
                if g > 0:
                    ctx_cat[(str(key), str(c))] = float(np.log(v / g))
        bought = e[e.event_type == "purchase"].groupby(col).lp.agg(["sum", "count"])
        viewed = e[e.event_type == "view"].groupby(col).lp.mean()
        for key in viewed.index:
            if key in bought.index:
                s_, n_ = bought.loc[key, "sum"], bought.loc[key, "count"]
                ctx_price[str(key)] = float((s_ + 10 * (viewed[key] + gb)) / (n_ + 10) - viewed[key])
    return ctx_cat, ctx_price

COVIEW_TOP = 20
SUB_TOP = 30
CTX_TOP = 100


@dataclass(frozen=True, slots=True)
class ProdStats:
    views_pd: float        # organic views per day
    purch_pd: float        # organic purchases per day
    conv: float            # purchases / views, shrunk towards the subcategory rate
    cart_rate: float       # add_to_cart / views, shrunk
    car_lift: float        # carousel purchases / purchases expected from the slots it was shown in (COEC), shrunk to 1
    car_imps: float        # carousel impressions (how sure we are about car_lift)


@dataclass(frozen=True, slots=True)
class SubStats:
    conv: float
    cart_rate: float
    car_lift: float
    median_price: float
    purch_pd: float


def _days(ts: pd.Series) -> float:
    if ts.empty:
        return 1.0
    e = ts.map(to_epoch)
    return max((e.max() - e.min()) / 86400.0, 1.0)


def coviews(events: pd.DataFrame, top: int = COVIEW_TOP, max_per_session: int = 30) -> dict[str, list[tuple[str, float]]]:
    """Products that appear in the same organic sessions, weighted by 1/log2(1+session size)."""
    if events.empty:
        return {}
    e = events[["session_id", "product_id"]].astype(str).drop_duplicates()
    e = e.groupby("session_id", sort=False).head(max_per_session)
    size = e.groupby("session_id")["product_id"].transform("size")
    e = e.assign(w=1.0 / np.log2(1.0 + size.to_numpy()))
    pairs = e.merge(e[["session_id", "product_id"]], on="session_id", suffixes=("", "_b"))
    pairs = pairs[pairs.product_id != pairs.product_id_b]
    agg = pairs.groupby(["product_id", "product_id_b"], sort=False)["w"].sum().reset_index()
    agg = agg.sort_values(["product_id", "w"], ascending=[True, False]).groupby("product_id").head(top)
    out: dict[str, list[tuple[str, float]]] = {}
    for a, b, w in zip(agg.product_id.to_numpy(), agg.product_id_b.to_numpy(), agg.w.to_numpy()):
        out.setdefault(a, []).append((b, float(w)))
    return out


def carousel_stats(sessions: pd.DataFrame, n_slots: int = 10) -> tuple[list[float], pd.DataFrame]:
    """Position bias and per-product COEC from history_sessions.

    slot_rate[k] = P(purchase of the item in slot k). For each product, expected = sum of slot_rate
    over its impressions; lift = purchases / expected (1 = as good as an average item in that slot)."""
    if sessions.empty:
        return [0.0] * n_slots, pd.DataFrame(columns=["product_id", "imps", "purch", "expected"])
    shown = sessions["shown"].map(lambda s: list(s)[:n_slots])
    purchased = [clean_id(x) or "" for x in sessions["purchased"]]
    rows_p, rows_k, rows_y = [], [], []
    for lst, p in zip(shown, purchased):
        for k, pid in enumerate(lst):
            rows_p.append(pid)
            rows_k.append(k)
            rows_y.append(1.0 if pid == p and p else 0.0)
    long = pd.DataFrame({"product_id": rows_p, "slot": rows_k, "y": rows_y})
    slot_rate = long.groupby("slot")["y"].mean().reindex(range(n_slots), fill_value=0.0)
    long["exp"] = slot_rate.to_numpy()[long.slot.to_numpy()]
    per = long.groupby("product_id").agg(imps=("y", "size"), purch=("y", "sum"), expected=("exp", "sum")).reset_index()
    return [float(x) for x in slot_rate.to_numpy()], per


def build_offline(catalog: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame) -> dict:
    """All offline artefacts from the three history tables (DataFrames in rtrec.schemas shapes)."""
    cat = catalog.copy()
    cat["product_id"] = cat.product_id.astype(str)
    days = _days(events["ts"]) if not events.empty else 1.0

    ev = events.assign(product_id=events.product_id.astype(str), event_type=events.event_type.astype(str))
    cnt = ev.groupby(["product_id", "event_type"]).size().unstack(fill_value=0)
    for col in ("view", "add_to_cart", "purchase"):
        if col not in cnt:
            cnt[col] = 0
    df = cat[["product_id", "subcategory", "price_eur"]].merge(cnt, left_on="product_id", right_index=True, how="left").fillna(
        {"view": 0, "add_to_cart": 0, "purchase": 0})

    slot_rate, car = carousel_stats(sessions)
    df = df.merge(car, on="product_id", how="left").fillna({"imps": 0, "purch": 0, "expected": 0})

    sub = df.groupby("subcategory").agg(v=("view", "sum"), c=("add_to_cart", "sum"), p=("purchase", "sum"),
                                        cp=("purch", "sum"), ce=("expected", "sum"),
                                        price=("price_eur", "median"))
    g_conv = (sub.p.sum() + 1) / (sub.v.sum() + 50)
    g_cart = (sub.c.sum() + 1) / (sub.v.sum() + 20)
    sub_conv = (sub.p + 20 * g_conv) / (sub.v + 20)
    sub_cart = (sub.c + 20 * g_cart) / (sub.v + 20)
    sub_lift = (sub.cp + 5) / (sub.ce + 5)
    subs = {s: SubStats(float(sub_conv[s]), float(sub_cart[s]), float(sub_lift[s]), float(sub.price[s]),
                        float(sub.p[s] / days)) for s in sub.index}

    K = 30.0  # prior strength in views
    sc = df.subcategory.map(sub_conv).fillna(g_conv)
    scart = df.subcategory.map(sub_cart).fillna(g_cart)
    slift = df.subcategory.map(sub_lift).fillna(1.0)
    conv = (df.purchase + K * sc) / (df.view + K)
    cart = (df.add_to_cart + K * scart) / (df.view + K)
    lift = (df.purch + 2 * slift) / (df.expected + 2)  # shrink to the subcategory lift, strength = 2 expected purchases
    prod = {p: ProdStats(float(v / days), float(pu / days), float(c), float(ca), float(li), float(im))
            for p, v, pu, c, ca, li, im in zip(df.product_id, df.view, df.purchase, conv, cart, lift, df.imps)}

    # baseline order exactly as the organisers: purchases desc, ties -> higher product_id first
    bs = cnt[cnt["purchase"] > 0]["purchase"].reset_index()
    bs.columns = ["product_id", "n"]
    best_sellers = bs.sort_values(["n", "product_id"], ascending=[False, False]).product_id.tolist()

    score = df.assign(s=df.purchase / days + 0.1 * df.view / days * conv)
    sub_top = {s: g.sort_values("s", ascending=False).product_id.head(SUB_TOP).tolist()
               for s, g in score.groupby("subcategory")}
    ctx_top: dict[str, list[str]] = {}
    if not ev.empty:
        dp = ev[ev.event_type == "purchase"].groupby(["device", "product_id"]).size().rename("n").reset_index()
        for d, g in dp.groupby("device"):
            ctx_top[str(d)] = g.sort_values("n", ascending=False).product_id.head(CTX_TOP).tolist()

    ctx_cat, ctx_price = context_stats(ev, cat)
    tokens, idf = content_index(cat)
    return {"ctx_cat": ctx_cat, "ctx_price": ctx_price, "tokens": tokens, "idf": idf,
            "best_sellers": best_sellers, "prod": prod, "sub": subs, "sub_top": sub_top, "ctx_top": ctx_top,
            "coview": coviews(ev), "slot_rate": slot_rate, "global_conv": float(g_conv), "history_days": days}
