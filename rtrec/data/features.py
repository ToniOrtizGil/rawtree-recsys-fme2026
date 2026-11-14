"""Pandas twins of the SQL feature queries in rtrec/sql/ (same names, same output columns), plus the
client-side fits that turn their aggregates into model inputs.

Why twins: the SQL is what runs inside RawTree (the "Use of RawTree" story, and the cheap way to
aggregate 12M events), the pandas version runs on a Parquet dump or on the simulator without a key.
tests/test_data_sql.py checks both give the same numbers.

Time arguments are ISO strings or epoch seconds; tables are the raw-shaped frames (as dumped) or
the prepared ones from rtrec.data.load (both have `ts`; prepared ones also have `t`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .load import epoch, slot_pairs


def _t(x) -> float:
    return float(x) if isinstance(x, (int, float, np.floating)) else float(epoch(pd.Series([x]))[0])


def _tcol(df: pd.DataFrame) -> np.ndarray:
    return df["t"].to_numpy() if "t" in df else epoch(df["ts"])


def _window(df: pd.DataFrame, since, until) -> pd.DataFrame:
    t = _tcol(df)
    return df[(t >= _t(since)) & (t < _t(until))]


# ---------------------------------------------------------------- current_catalog.sql
def current_catalog(catalog: pd.DataFrame, updates: pd.DataFrame | None = None) -> pd.DataFrame:
    cols = ["product_id", "category", "subcategory", "brand", "price_eur", "is_active"]
    base = catalog[cols].assign(change="catalog", v=-np.inf)
    parts = [base]
    if updates is not None and len(updates):
        parts.append(updates[cols + ["change"]].assign(v=_tcol(updates)))
    allr = pd.concat(parts, ignore_index=True).sort_values(["product_id", "v"], kind="stable")
    g = allr.groupby("product_id", sort=True)
    out = g.last()[["category", "subcategory", "brand", "price_eur", "is_active", "change"]]
    out = out.rename(columns={"change": "last_change"})
    out.insert(4, "launch_price_eur", g["price_eur"].first())
    out["available"] = out.is_active.astype(bool) & (out.last_change != "out_of_stock")
    return out.reset_index()


# ---------------------------------------------------------------- popularity_by_context.sql
def popularity_by_context(events: pd.DataFrame, since, until) -> pd.DataFrame:
    e = _window(events, since, until)
    out = (e.assign(views=e.event_type.eq("view"), carts=e.event_type.eq("add_to_cart"),
                    purchases=e.event_type.eq("purchase"))
           .groupby(["product_id", "device", "region"], sort=True)[["views", "carts", "purchases"]].sum())
    return out.astype("int64").reset_index()


# ---------------------------------------------------------------- coviews.sql
def coviews(events: pd.DataFrame, since, until, k: int = 50) -> pd.DataFrame:
    v = _window(events, since, until)
    v = v[v.event_type == "view"][["session_id", "product_id"]].drop_duplicates()
    pairs = v.merge(v, on="session_id", suffixes=("", "_b"))
    pairs = pairs[pairs.product_id != pairs.product_id_b]
    out = pairs.groupby(["product_id", "product_id_b"]).size().rename("sessions").reset_index()
    out = out.rename(columns={"product_id_b": "other_id"})
    out = out.sort_values(["product_id", "sessions", "other_id"], ascending=[True, False, True], kind="stable")
    return out.groupby("product_id", sort=False).head(k).reset_index(drop=True)


# ---------------------------------------------------------------- position_bias.sql / product_slot_stats.sql
def _pairs(sessions: pd.DataFrame, since, until) -> pd.DataFrame:
    s = _window(sessions, since, until)
    if "clicked_slot" not in s:
        from .load import prepare
        s = prepare(pd.DataFrame({"created_at": []}), s, pd.DataFrame({"ts": []})).sessions
    return slot_pairs(s)


def position_bias(sessions: pd.DataFrame, since, until) -> pd.DataFrame:
    p = _pairs(sessions, since, until)
    out = p.groupby("slot").agg(impressions=("clicked", "size"), clicks=("clicked", "sum"),
                                purchases=("purchased", "sum"), mean_propensity=("propensity", "mean"))
    out.index = out.index + 1
    return out.reset_index()


def product_slot_stats(sessions: pd.DataFrame, since, until) -> pd.DataFrame:
    p = _pairs(sessions, since, until).assign(inv=lambda d: 1.0 / d.propensity)
    out = p.groupby(["product_id", "slot"], sort=True).agg(
        impressions=("clicked", "size"), clicks=("clicked", "sum"), purchases=("purchased", "sum"),
        inv_propensity=("inv", "sum")).reset_index()
    out["slot"] += 1
    return out


def fit_position_bias(stats: pd.DataFrame, iters: int = 50, prior: float = 1.0) -> tuple[pd.Series, pd.Series]:
    """Disentangle position from product: clicks[p, k] ~ Poisson(impressions[p, k] * attract[p] * examine[k]).
    Alternating closed-form updates (rank-1 Poisson fit). The old carousel's randomness puts the
    same product in different slots, which is what makes this identifiable.
    Returns (examine by slot, normalised so slot 1 = 1; attractiveness by product, smoothed)."""
    st = stats.copy()
    k = st.slot.to_numpy() - 1
    pid, pidx = np.unique(st.product_id.to_numpy(), return_inverse=True)
    imp, clk = st.impressions.to_numpy(float), st.clicks.to_numpy(float)
    e = np.ones(10)
    a = np.full(len(pid), clk.sum() / max(imp.sum(), 1))
    a0 = a[0]
    for _ in range(iters):
        expo = np.bincount(pidx, imp * e[k], len(pid))
        a = (np.bincount(pidx, clk, len(pid)) + prior * a0) / (expo + prior)
        e = np.bincount(k, clk, 10) / np.maximum(np.bincount(k, imp * a[pidx], 10), 1e-12)
        e = e / e[0]
    return pd.Series(e, index=pd.RangeIndex(1, 11, name="slot"), name="examine"), \
        pd.Series(a, index=pd.Index(pid, name="product_id"), name="attract")


def product_priors(stats: pd.DataFrame, catalog: pd.DataFrame, examine: pd.Series, strength: float = 20.0) -> pd.DataFrame:
    """Position-corrected click and purchase rates per product ("as if shown in slot 1"), shrunk
    towards the subcategory rate with `strength` pseudo-exposures (empirical Bayes). Products never
    shown get their subcategory's rate: this is also the cold-start prior for new products."""
    st = stats.assign(expo=stats.impressions * examine.reindex(stats.slot).to_numpy())
    g = st.groupby("product_id")[["expo", "clicks", "purchases", "impressions"]].sum()
    df = catalog[["product_id", "subcategory"]].merge(g, left_on="product_id", right_index=True, how="left").fillna(
        {"expo": 0.0, "clicks": 0, "purchases": 0, "impressions": 0})
    sub = df.groupby("subcategory")[["expo", "clicks", "purchases"]].sum()
    glob = df[["expo", "clicks", "purchases"]].sum()
    sub_ctr = ((sub.clicks + strength * glob.clicks / max(glob.expo, 1e-9)) / (sub.expo + strength))
    sub_cvr = ((sub.purchases + strength * glob.purchases / max(glob.expo, 1e-9)) / (sub.expo + strength))
    pc, pp = sub_ctr.reindex(df.subcategory).to_numpy(), sub_cvr.reindex(df.subcategory).to_numpy()
    df["ctr_slot1"] = (df.clicks + strength * pc) / (df.expo + strength)
    df["cvr_slot1"] = (df.purchases + strength * pp) / (df.expo + strength)
    return df


# ---------------------------------------------------------------- trend_lift.sql
def trend_lift(events: pd.DataFrame, catalog: pd.DataFrame, updates: pd.DataFrame | None, now,
               recent_min: float = 15, base_min: float = 180) -> pd.DataFrame:
    now = _t(now)
    cat = catalog[["product_id", "subcategory"]]
    if updates is not None and len(updates):
        cat = pd.concat([cat, updates.loc[updates.change == "new_product", ["product_id", "subcategory"]]])
    cat = cat.drop_duplicates("product_id")
    e = _window(events[events.event_type == "view"], now - 60 * (recent_min + base_min), now)
    recent = _tcol(e) >= now - 60 * recent_min
    e = e.assign(recent=recent, base=~recent).merge(cat, on="product_id")
    g = e.groupby("subcategory")[["recent", "base"]].sum().astype("int64")
    out = g.rename(columns={"recent": "views_recent", "base": "views_base"})
    out["lift"] = (out.views_recent + 1) / recent_min / ((out.views_base + 1) / base_min)
    return out.reset_index().sort_values(["lift", "subcategory"], ascending=[False, True], kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------- user_affinity.sql
def user_affinity(events: pd.DataFrame, catalog: pd.DataFrame, since, until) -> pd.DataFrame:
    e = _window(events, since, until).merge(catalog[["product_id", "subcategory"]], on="product_id")
    out = (e.assign(views=e.event_type.eq("view"), carts=e.event_type.eq("add_to_cart"),
                    purchases=e.event_type.eq("purchase"))
           .groupby(["user_id", "subcategory"], sort=True)[["views", "carts", "purchases"]].sum())
    return out.astype("int64").reset_index()
