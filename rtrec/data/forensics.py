"""Forensics: the questions H1-H11 of the plan (docs/memory/06_plan_ganador.md section 3), as code.

Every function takes the prepared history (rtrec.data.load.History) and/or live tables as
DataFrames and returns `Finding(answer, table)`: a one-line answer and the numbers behind it.
Nothing here knows whether the data is synthetic or real, so the same notebook
(notebooks/forensics.ipynb) and report (docs/FORENSICS.md) rerun unchanged on the real dump.

Click rates are compared "per unit of examination": clicks / sum(examine[slot]) with the position
curve fitted in H2, so a feature is not credited for the slot the old carousel put it in.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import features as F
from .load import History, slot_pairs


@dataclass
class Finding:
    question: str
    answer: str
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    numbers: dict = field(default_factory=dict)


class Forensics:
    """Shared, cached intermediate tables for all questions."""

    def __init__(self, h: History, max_sessions: int | None = 400_000, seed: int = 0):
        self.h = h
        s = h.sessions
        if max_sessions and len(s) > max_sessions:
            s = s.sample(max_sessions, random_state=seed).sort_values("t")
        self.sessions = s.reset_index(drop=True)
        self.cat = h.catalog.set_index("product_id")
        self.pairs = slot_pairs(self.sessions)
        st = F.product_slot_stats(self.sessions, -np.inf, np.inf)
        self.examine, self.attract = F.fit_position_bias(st)
        self.pairs["exam"] = self.examine.to_numpy()[self.pairs.slot.to_numpy()]
        for c in ("subcategory", "category", "brand", "price_eur"):
            self.pairs[c] = self.cat[c].reindex(self.pairs.product_id).to_numpy()
        self._views = None

    # -------------------------------------------------------------- helpers
    def views_long(self) -> pd.DataFrame:
        """(request_id, product_id) of every recent view, with its attributes."""
        if self._views is None:
            s = self.sessions[["request_id", "recent_views"]].explode("recent_views").dropna()
            v = pd.DataFrame({"request_id": s.request_id.to_numpy(),
                              "product_id": [x["product_id"] for x in s.recent_views]})
            for c in ("subcategory", "category", "brand", "price_eur"):
                v[c] = self.cat[c].reindex(v.product_id).to_numpy()
            self._views = v
        return self._views

    def flag(self, keys: list[str], table: pd.DataFrame) -> np.ndarray:
        """For each pair: does (request_id, *keys) appear in `table`?"""
        t = table[["request_id"] + keys].drop_duplicates().assign(_hit=True)
        m = self.pairs[["request_id"] + keys].merge(t, on=["request_id"] + keys, how="left")
        return m["_hit"].fillna(False).to_numpy(bool)

    @staticmethod
    def rate_table(p: pd.DataFrame, by) -> pd.DataFrame:
        g = p.groupby(by, observed=True).agg(pairs=("clicked", "size"), clicks=("clicked", "sum"),
                                             purchases=("purchased", "sum"), exposure=("exam", "sum"))
        g["ctr_per_exam"] = g.clicks / g.exposure
        g["cvr_per_exam"] = g.purchases / g.exposure
        g["p_buy_given_click"] = g.purchases / g.clicks.where(g.clicks > 0)
        return g

    @staticmethod
    def lift(g: pd.DataFrame, yes=True, no=False, col="ctr_per_exam") -> float:
        return float(g.loc[yes, col] / g.loc[no, col]) if yes in g.index and no in g.index and g.loc[no, col] > 0 else float("nan")

    # -------------------------------------------------------------- H1
    def h1_viewed_products(self) -> Finding:
        p = self.pairs.assign(viewed=self.flag(["product_id"], self.views_long()))
        g = self.rate_table(p, "viewed")
        lc, lb = self.lift(g), self.lift(g, col="cvr_per_exam")
        return Finding("H1 Does showing a product already viewed in the session convert?",
                       f"Yes if > 1: a just-viewed product gets x{lc:.2f} clicks and x{lb:.2f} purchases per unit of "
                       f"examination vs other shown products ({int(g.loc[True, 'pairs']) if True in g.index else 0} pairs).",
                       g, {"click_lift": lc, "purchase_lift": lb})

    # -------------------------------------------------------------- H2
    def h2_position_bias(self) -> Finding:
        raw = F.position_bias(self.sessions, -np.inf, np.inf).set_index("slot")
        raw["raw_ctr"] = raw.clicks / raw.impressions
        raw["raw_rel"] = raw.raw_ctr / raw.raw_ctr.iloc[0]
        raw["fitted_examine"] = self.examine.to_numpy()
        return Finding("H2 Position bias curve (clicks per slot, corrected)",
                       f"Slot 10 is examined {self.examine.iloc[-1]:.2f}x as much as slot 1 (fitted, product effects removed); "
                       f"the raw click curve says {raw.raw_rel.iloc[-1]:.2f}x. Top 3 slots carry "
                       f"{self.examine.iloc[:3].sum() / self.examine.sum():.0%} of the attention.",
                       raw, {"examine": self.examine.round(4).tolist()})

    # -------------------------------------------------------------- H3
    def h3_what_drives_clicks(self) -> Finding:
        v = self.views_long()
        has_views = self.pairs.n_views > 0
        p = self.pairs[has_views].copy()
        fl = {}
        for k in ("subcategory", "category", "brand"):
            fl[f"same_{k}"] = self.flag([k], v)[has_views.to_numpy()]
        vp = v.groupby("request_id").price_eur.median().rename("view_price")
        p = p.merge(vp, left_on="request_id", right_index=True, how="left")
        ratio = p.price_eur / p.view_price
        p["price_ratio"] = pd.cut(ratio, [0, 0.5, 0.8, 1.25, 2, np.inf], labels=["<0.5", "0.5-0.8", "0.8-1.25", "1.25-2", ">2"])
        rows = []
        for k, f in fl.items():
            p[k] = f
            g = self.rate_table(p, k)
            rows.append({"feature": k, "share_of_pairs": float(f.mean()), "click_lift": self.lift(g),
                         "purchase_lift": self.lift(g, col="cvr_per_exam")})
        tab = pd.DataFrame(rows).set_index("feature")
        pr = self.rate_table(p, "price_ratio")
        best = tab.click_lift.idxmax()
        return Finding("H3 What explains the click: same subcategory, category, brand, price close to what was viewed?",
                       f"Strongest single signal: {best} (x{tab.click_lift.max():.2f} clicks per examination). "
                       f"By price ratio to the viewed products: " + ", ".join(
                           f"{i}: {r:.4f}" for i, r in pr.ctr_per_exam.items()) + " clicks/exam.",
                       pd.concat([tab, pr[["pairs", "ctr_per_exam", "cvr_per_exam"]].rename_axis("feature")
                                  .rename(index=lambda x: f"price_ratio {x}")]),
                       {"lifts": tab.click_lift.to_dict()})

    # -------------------------------------------------------------- H4
    def h4_price_sensitivity(self) -> Finding:
        e = self.h.events
        sp = e[e.event_type.isin(["add_to_cart", "purchase"])]
        bought = set(map(tuple, sp.loc[sp.event_type == "purchase", ["session_id", "product_id"]].to_numpy()))
        carts = sp[sp.event_type == "add_to_cart"].drop_duplicates(["session_id", "product_id"]).copy()
        carts["bought"] = [(a, b) in bought for a, b in zip(carts.session_id, carts.product_id)]
        carts["log_price"] = np.log(carts.price_eur.astype(float))
        rows = []
        for (dim, grp) in (("device", "device"), ("region", "region")):
            for val, d in carts.groupby(grp):
                if len(d) < 200 or d.bought.nunique() < 2:
                    continue
                slope = _logit_slope(d.log_price.to_numpy(), d.bought.to_numpy())
                rows.append({"dim": dim, "value": val, "carts": len(d), "p_buy_given_cart": d.bought.mean(),
                             "log_price_slope": slope})
        tab = pd.DataFrame(rows).set_index(["dim", "value"])
        # carousel: P(buy | click) by price tercile x device
        c = self.pairs[self.pairs.clicked == 1].copy()
        c["price_bin"] = pd.qcut(c.price_eur.astype(float), 3, labels=["cheap", "mid", "expensive"], duplicates="drop")
        car = c.groupby(["device", "price_bin"], observed=True).purchased.agg(["size", "mean"]).rename(
            columns={"size": "clicks", "mean": "p_buy_given_click"})
        dev = tab.loc["device"].log_price_slope if "device" in tab.index.get_level_values(0) else pd.Series(dtype=float)
        return Finding("H4 Does purchase-given-interest depend on price x device / region?",
                       "Logit slope of P(buy | cart) on log(price), by device: " +
                       ", ".join(f"{i} {v:+.2f}" for i, v in dev.items()) +
                       f"; across regions it ranges {tab.loc['region'].log_price_slope.min():+.2f} .. "
                       f"{tab.loc['region'].log_price_slope.max():+.2f}. More negative = more price sensitive.",
                       tab, {"device_slopes": dev.to_dict(), "carousel": car.reset_index().to_dict("records")})

    # -------------------------------------------------------------- H5
    def h5_user_history(self) -> Finding:
        e = self.h.events
        buys = e[e.event_type == "purchase"][["user_id", "product_id", "t"]]
        hs = self.h.sessions
        cbuys = hs[hs.purchased != ""][["user_id", "purchased", "t"]].rename(columns={"purchased": "product_id"})
        allb = pd.concat([buys, cbuys]).groupby(["user_id", "product_id"]).t.min().rename("first_buy").reset_index()
        p = self.pairs.merge(allb, on=["user_id", "product_id"], how="left")
        p["bought_before"] = p.first_buy < p.t
        g_rep = self.rate_table(p, "bought_before")
        views = e[e.event_type == "view"][["user_id", "product_id", "t"]].copy()
        views["subcategory"] = self.cat.subcategory.reindex(views.product_id).to_numpy()
        first = views.groupby(["user_id", "subcategory"]).t.min().rename("first_sub_view").reset_index()
        p = p.merge(first, on=["user_id", "subcategory"], how="left")
        p["known_affinity"] = p.first_sub_view < p.t - 3600   # viewed that subcategory in an earlier session
        g_aff = self.rate_table(p, "known_affinity")
        ret = float(hs.user_id.isin(e[e.t < hs.t.min() + 7 * 86400].user_id).mean())
        lr, la = self.lift(g_rep), self.lift(g_aff)
        return Finding("H5 Does long-term history predict? How much does having bought it already hurt?",
                       f"Already bought: x{lr:.2f} clicks per examination ({int(g_rep.loc[True, 'pairs']) if True in g_rep.index else 0} pairs). "
                       f"Subcategory seen in an earlier session: x{la:.2f}. "
                       f"{ret:.0%} of impressions are by shoppers already active in the first week of history.",
                       pd.concat({"bought_before": g_rep, "known_affinity": g_aff}, names=["feature"]),
                       {"repeat_lift": lr, "affinity_lift": la})

    # -------------------------------------------------------------- H6
    def h6_trends(self, bin_s: int = 300, recent_bins: int = 3, base_bins: int = 36,
                  lags_min=(0, 15, 30, 60, 120)) -> Finding:
        e = self.h.events
        v = e[e.event_type == "view"]
        sub = self.cat.subcategory.reindex(v.product_id).to_numpy()
        subs, si = np.unique(sub.astype(str), return_inverse=True)
        t0 = min(v.t.min(), self.pairs.t.min())
        b = ((v.t.to_numpy() - t0) // bin_s).astype(int)
        nb = int(max(b.max(), (self.pairs.t.max() - t0) // bin_s)) + 2
        counts = np.zeros((len(subs), nb))
        np.add.at(counts, (si, b), 1)
        cs = np.concatenate([np.zeros((len(subs), 1)), np.cumsum(counts, 1)], 1)
        p = self.pairs
        psi = np.searchsorted(subs, p.subcategory.astype(str).to_numpy())
        psi = np.clip(psi, 0, len(subs) - 1)
        pb = ((p.t.to_numpy() - t0) // bin_s).astype(int)
        rows = []
        for lag in lags_min:
            end = pb - lag * 60 // bin_s
            ok = end - recent_bins - base_bins >= 0
            rec = cs[psi, np.clip(end, 0, nb)] - cs[psi, np.clip(end - recent_bins, 0, nb)]
            base = cs[psi, np.clip(end - recent_bins, 0, nb)] - cs[psi, np.clip(end - recent_bins - base_bins, 0, nb)]
            lift = ((rec + 1) / recent_bins) / ((base + 1) / base_bins)
            q = pd.Series(np.where(ok, lift, np.nan))
            hot = q > q.quantile(0.9)
            g = self.rate_table(p.assign(hot=hot.to_numpy())[ok], "hot")
            rows.append({"lag_min": lag, "hot_threshold_lift": float(q.quantile(0.9)), "click_lift_hot_vs_rest": self.lift(g),
                         "purchase_lift_hot_vs_rest": self.lift(g, col="cvr_per_exam")})
        tab = pd.DataFrame(rows).set_index("lag_min")
        best = tab.click_lift_hot_vs_rest.idxmax()
        return Finding("H6 Do trends in the organic events predict carousel conversion? With what delay?",
                       f"Products whose subcategory is in the top 10% of 15-min view lift get x{tab.click_lift_hot_vs_rest.iloc[0]:.2f} "
                       f"clicks per examination right away; the signal is strongest at lag {best} min and "
                       f"x{tab.click_lift_hot_vs_rest.iloc[-1]:.2f} after {tab.index[-1]} min (decay = half-life of the counters).",
                       tab, tab.click_lift_hot_vs_rest.to_dict())

    # -------------------------------------------------------------- H7
    def h7_price_changes(self, events: pd.DataFrame | None = None, updates: pd.DataFrame | None = None) -> Finding:
        e = self.h.events
        nun = e.groupby("product_id").price_eur.nunique()
        hist_share = float((nun > 1).mean())
        tab = pd.DataFrame({"history_products_with_price_variation": [hist_share]})
        ans = f"{hist_share:.1%} of products change price within the history."
        if events is not None and updates is not None and len(updates):
            pc = updates[updates.change == "price_change"].copy()
            pc["t"] = F._tcol(pc)
            ev = events.copy()
            ev["t"] = F._tcol(ev)
            first = pc.sort_values("t").drop_duplicates("product_id")
            launch = self.cat.price_eur.reindex(first.product_id).to_numpy()
            first = first.assign(rel=np.log(first.price_eur.astype(float).to_numpy() / launch))
            m = ev.merge(first[["product_id", "t", "rel"]].rename(columns={"t": "t_change"}), on="product_id")
            m["after"] = m.t >= m.t_change
            m["kind"] = np.where(m.rel < 0, "drop", "raise")
            g = m.groupby(["kind", "after"]).event_type.value_counts().unstack(fill_value=0)
            g["buy_per_view"] = g.get("purchase", 0) / g.get("view", 1)
            tab = g
            def r(kind):
                try:
                    return float(g.loc[(kind, True), "buy_per_view"] / g.loc[(kind, False), "buy_per_view"])
                except (KeyError, ZeroDivisionError):
                    return float("nan")
            ans += f" Live: purchases per view x{r('drop'):.2f} after a price drop, x{r('raise'):.2f} after a raise."
        else:
            ans += " Live price_change updates are needed to measure the effect (pass events + catalog_updates)."
        return Finding("H7 Effect of a price change", ans, tab)

    # -------------------------------------------------------------- H8
    def h8_target_shape(self) -> Finding:
        s = self.sessions
        clicked, bought = s.clicked != "", s.purchased != ""
        consistent = float((s.purchased[bought] == s.clicked[bought]).mean()) if bought.any() else float("nan")
        tab = pd.DataFrame({"impressions": [len(s)], "click_rate": [clicked.mean()], "purchase_rate": [bought.mean()],
                            "p_buy_given_click": [bought.sum() / max(clicked.sum(), 1)],
                            "purchase_equals_click": [consistent],
                            "purchase_without_click": [int((bought & ~clicked).sum())],
                            "revenue_per_impression": [s.revenue_eur.astype(float).mean()]})
        return Finding("H8 At most one click / purchase per impression? Does purchase imply click?",
                       f"One clicked and one purchased field per impression; purchased == clicked in {consistent:.0%} of purchases; "
                       f"{clicked.mean():.2%} of impressions get a click, {bought.mean():.2%} a purchase "
                       f"({bought.sum() / max(clicked.sum(), 1):.0%} of clicks convert).",
                       tab, tab.iloc[0].to_dict())

    # -------------------------------------------------------------- H9
    def h9_propensity(self) -> Finding:
        p = self.pairs
        day = (p.t // 86400).astype(int)
        p1 = p[p.slot == 0].assign(day=day[p.slot == 0]).groupby(["day", "product_id"]).propensity.median()
        # Plackett-Luce check: p_k == w_k / (W - sum_{j<k} w_j)  <=>  p_k == p1_k / (1 - sum_{j<k} p1_j)
        key = pd.MultiIndex.from_arrays([day, p.product_id])
        p1_each = p1.reindex(key).to_numpy()
        P1 = p1_each.reshape(-1, 10)
        prev = np.concatenate([np.zeros((len(P1), 1)), np.nancumsum(P1, 1)[:, :-1]], 1)
        known_prev = np.concatenate([np.ones((len(P1), 1), bool), np.cumprod(~np.isnan(P1), 1)[:, :-1].astype(bool)], 1)
        pred = P1 / (1 - prev)
        act = p.propensity.to_numpy().reshape(-1, 10)
        ok = known_prev & ~np.isnan(pred)
        rel = np.abs(pred[ok] / act[ok] - 1)
        q = np.quantile(act, [0.0, 0.01, 0.5, 0.99])
        tab = pd.DataFrame({"min": [q[0]], "p01": [q[1]], "median": [q[2]], "p99": [q[3]],
                            "share_below_1e-3": [(act < 1e-3).mean()],
                            "pl_check_pairs": [int(ok.sum())], "pl_median_rel_error": [float(np.median(rel)) if len(rel) else np.nan],
                            "slot1_top_product_propensity": [float(p1.max())]})
        pl = len(rel) and np.median(rel) < 0.02
        return Finding("H9 Structure of `propensity`",
                       ("Consistent with a Plackett-Luce draw over a per-day weight vector "
                        f"(slot-k propensity predicted from slot-1 propensities with median error {np.median(rel):.1%}), "
                        if pl else f"NOT a simple per-day Plackett-Luce (median error {np.median(rel) if len(rel) else float('nan'):.1%}); ")
                       + f"median propensity {q[2]:.4f}, {tab['share_below_1e-3'][0]:.0%} below 1e-3 (IPS weights > 1000: "
                         "clip or use SNIPS / per-slot estimators).", tab, tab.iloc[0].to_dict())


def _logit_slope(x: np.ndarray, y: np.ndarray, iters: int = 25) -> float:
    """Slope of a 1-D logistic regression (Newton), no sklearn needed."""
    X = np.c_[np.ones_like(x), x - x.mean()]
    w = np.zeros(2)
    for _ in range(iters):
        pr = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (pr * (1 - pr))[:, None]) + 1e-6 * np.eye(2)
        w += np.linalg.solve(H, X.T @ (y - pr))
    return float(w[1])


# ------------------------------------------------------------------ live-only questions
def h10_live_requests(requests: pd.DataFrame) -> Finding:
    r = requests.copy()
    r["t"] = F._tcol(r)
    nv = r.recent_views.map(lambda v: len(v) if v is not None else 0)
    per_user = r.groupby("user_id").size()
    span = max(r.t.max() - r.t.min(), 1)
    tab = pd.DataFrame({"requests": [len(r)], "per_second": [len(r) / span], "new_user_share": [r.is_new_user.mean()],
                        "no_view_share": [(nv == 0).mean()], "mean_views": [nv.mean()],
                        "requests_per_user": [per_user.mean()], "users_seen_twice_share": [(per_user > 1).mean()]})
    pages = r.context.map(lambda c: dict(c)["page"]).value_counts(normalize=True)
    return Finding("H10 Live requests: rate, % new shoppers, % without views, repetition",
                   f"{tab.per_second[0]:.2f} requests/s, {tab.new_user_share[0]:.0%} new shoppers, "
                   f"{tab.no_view_share[0]:.0%} without recent views (mean {tab.mean_views[0]:.1f} views); pages "
                   + ", ".join(f"{k} {v:.0%}" for k, v in pages.items())
                   + ". Visibility delay of requests is measured by the runtime (latency_ms in feedback), not here.",
                   tab, tab.iloc[0].to_dict())


def h11_stock_risk(updates: pd.DataFrame, top: list[str], feedback: pd.DataFrame | None = None,
                   horizon_s: float = 3.0) -> Finding:
    """How likely is a list to become invalid between our answer and the organisers' check?"""
    u = updates.copy()
    u["t"] = F._tcol(u)
    span = max(u.t.max() - u.t.min(), 1)
    oos = u[u.change == "out_of_stock"]
    rate_all = len(oos) / span
    top_set = set(top)
    rate_top = oos.product_id.isin(top_set).sum() / span
    # chance that one of 10 random top products goes OOS within the horizon
    p_list = 1 - np.exp(-rate_top * horizon_s * 10 / max(len(top_set), 1))
    tab = pd.DataFrame({"oos_per_hour": [3600 * rate_all], "oos_per_hour_in_top": [3600 * rate_top],
                        "p_list_invalid_within_horizon": [p_list]})
    ans = (f"{3600 * rate_all:.0f} stock-outs/hour ({3600 * rate_top:.0f} among the top {len(top_set)}); a 10-item list of top "
           f"products goes invalid within {horizon_s:.0f}s with p = {p_list:.2%} -> refresh stock <= 0.5 s before sending.")
    if feedback is not None and len(feedback):
        fb = feedback[feedback.request_id != "__init__"]
        inv = float((fb.fallback_reason == "invalid").mean())
        tab["feedback_invalid_share"] = inv
        ans += f" Measured in feedback: {inv:.2%} invalid."
    return Finding("H11 Products going out of stock right after we answer", ans, tab)


def run_all(h: History, live: dict[str, pd.DataFrame] | None = None, max_sessions: int | None = 400_000) -> list[Finding]:
    fz = Forensics(h, max_sessions=max_sessions)
    live = live or {}
    out = [fz.h1_viewed_products(), fz.h2_position_bias(), fz.h3_what_drives_clicks(), fz.h4_price_sensitivity(),
           fz.h5_user_history(), fz.h6_trends(), fz.h7_price_changes(live.get("events"), live.get("catalog_updates")),
           fz.h8_target_shape(), fz.h9_propensity()]
    if "requests" in live:
        out.append(h10_live_requests(live["requests"]))
    if "catalog_updates" in live:
        ev = h.events
        top = ev[ev.event_type == "purchase"].product_id.value_counts().index[:100].tolist()
        out.append(h11_stock_risk(live["catalog_updates"], top, live.get("feedback")))
    return out


def _fmt(x) -> str:
    if isinstance(x, (float, np.floating)):
        return "nan" if np.isnan(x) else f"{x:.4g}"
    return str(x)


def md_table(df: pd.DataFrame) -> str:
    """Markdown table without the optional `tabulate` dependency."""
    named = any(n is not None for n in df.index.names)
    d = df.reset_index() if named or not isinstance(df.index, pd.RangeIndex) else df
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_fmt(v) for v in row) + " |" for row in d.itertuples(index=False)]
    return "\n".join(lines)


def to_markdown(findings: list[Finding], max_rows: int = 15) -> str:
    parts = []
    for f in findings:
        parts.append(f"### {f.question}\n\n**{f.answer}**\n")
        if len(f.table):
            t = f.table.head(max_rows)
            parts.append(md_table(t))
        parts.append("")
    return "\n".join(parts)


if __name__ == "__main__":   # python -m rtrec.data.forensics --data data > findings.md
    import argparse

    from .load import load_history, load_live
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--max-sessions", type=int, default=400_000)
    a = ap.parse_args()
    hist = load_history(a.data)
    print(f"<!-- source: {hist.source} -->\n")
    print(to_markdown(run_all(hist, load_live(a.data), a.max_sessions)))
