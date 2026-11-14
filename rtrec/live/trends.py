"""Decayed trend counters over shop.events, per product, subcategory and context.

Why: the final round has new trends that are "visible early in the live events". A short half-life
counter (default 8 min) tracks what is hot *now*; a long one (3 h) is the background rate; their
ratio is the lift. Purchases and carts weigh more than views because the score is purchases.

Smoothing: lift = (r_short + a) / (r_long + a), with `a` = pseudo-rate of an average product, so a
product with 2 views does not get lift 50. Lift is also divided by the shop-wide lift, so a global
traffic surge does not make everything look trendy.

Everything is O(1) per event; the snapshot published in state.live (sorted lists, dicts) is rebuilt
at most every `snapshot_every_s` of event time and always replaced as a whole object, so policies on
other threads never see a half-built structure.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from rtrec.contract import State

from ._util import DecayedCounter

EVENT_WEIGHT = {"view": 1.0, "add_to_cart": 3.0, "purchase": 6.0}


@dataclass
class TrendTracker:
    short_half_life_s: float = 8 * 60
    long_half_life_s: float = 3 * 3600
    ctx_half_life_s: float = 60 * 60
    snapshot_every_s: float = 2.0
    top_n: int = 300
    top_per_group: int = 30
    smoothing: float = 1.0          # pseudo-count in units of "average product rate"
    group_half_life_s: float = 5 * 60   # subcategory / brand: more support, so react faster
    group_prior: float = 0.3        # pseudo-share (x a uniform share) shrinking group lifts towards 1
    baseline: dict = field(default_factory=dict)   # {"sub": {g: share}, "brand": {g: share}} from history
    now: float = 0.0
    _last_snap: float = float("-inf")
    p_short: DecayedCounter = field(init=False)
    p_long: DecayedCounter = field(init=False)
    p_buy: DecayedCounter = field(init=False)     # purchases only, short half-life
    s_short: DecayedCounter = field(init=False)
    s_long: DecayedCounter = field(init=False)
    b_short: DecayedCounter = field(init=False)   # brand level: trends can hit a whole brand
    b_long: DecayedCounter = field(init=False)
    g_short: DecayedCounter = field(init=False)
    g_long: DecayedCounter = field(init=False)
    ctx: DecayedCounter = field(init=False)       # (device, region, pid) weighted
    dev: DecayedCounter = field(init=False)       # (device, pid)

    def __post_init__(self):
        self.p_short = DecayedCounter(self.short_half_life_s)
        self.p_long = DecayedCounter(self.long_half_life_s)
        self.p_buy = DecayedCounter(self.short_half_life_s * 2)
        self.s_short = DecayedCounter(self.group_half_life_s)
        self.s_long = DecayedCounter(self.long_half_life_s)
        self.b_short = DecayedCounter(self.group_half_life_s)
        self.b_long = DecayedCounter(self.long_half_life_s)
        self.g_short = DecayedCounter(self.short_half_life_s)
        self.g_long = DecayedCounter(self.long_half_life_s)
        self.ctx = DecayedCounter(self.ctx_half_life_s)
        self.dev = DecayedCounter(self.ctx_half_life_s)

    def add(self, ev: dict, t: float, state: State) -> None:
        w = EVENT_WEIGHT.get(ev.get("event_type"), 1.0)
        pid = str(ev["product_id"])
        row = state.catalog.get(pid) or {}
        sub, brand = row.get("subcategory"), row.get("brand")
        self.p_short.add(pid, w, t)
        self.p_long.add(pid, w, t)
        if ev.get("event_type") == "purchase":
            self.p_buy.add(pid, 1.0, t)
        if sub is not None:
            self.s_short.add(sub, w, t)
            self.s_long.add(sub, w, t)
        if brand is not None:
            self.b_short.add(brand, w, t)
            self.b_long.add(brand, w, t)
        self.g_short.add(0, w, t)
        self.g_long.add(0, w, t)
        if w > 1.0:   # context popularity: carts and purchases only (intent, not browsing)
            self.ctx.add((ev.get("device"), ev.get("region"), pid), w, t)
            self.dev.add((ev.get("device"), pid), w, t)
        if t > self.now:
            self.now = t

    # ------------------------------------------------------------------ reads (pure)
    def global_lift(self, now: float | None = None) -> float:
        now = self.now if now is None else now
        rl = self.g_long.rate(0, now)
        return self.g_short.rate(0, now) / rl if rl > 0 else 1.0

    def lift(self, pid: str, now: float | None = None) -> float:
        now = self.now if now is None else now
        a = self._pseudo_rate(now)
        rs, rl = self.p_short.rate(pid, now), self.p_long.rate(pid, now)
        return (rs + a) / (rl + a) / max(self.global_lift(now), 1e-6)

    def rate(self, pid: str, now: float | None = None) -> float:
        """Weighted events per hour for this product, recent (short half-life)."""
        now = self.now if now is None else now
        return 3600.0 * self.p_short.rate(pid, now)

    def _pseudo_rate(self, now: float) -> float:
        n = max(len(self.p_long.d), 1)
        return self.smoothing * self.g_long.rate(0, now) / n

    # ------------------------------------------------------------------ snapshot
    def maybe_publish(self, state: State, force: bool = False) -> bool:
        if not force and self.now - self._last_snap < self.snapshot_every_s:
            return False
        self._last_snap = self.now
        self.publish(state)
        return True

    def publish(self, state: State) -> None:
        now = self.now
        avail = state.available
        cat = state.catalog
        gl = max(self.global_lift(now), 1e-6)
        a = self._pseudo_rate(now)
        ws, wl = self.p_short.warmup(now) * self.p_short.tau, self.p_long.warmup(now) * self.p_long.tau
        rate, lift = {}, {}
        for pid, vs in self.p_short.items(now):
            rs = vs / ws
            rl = self.p_long.value(pid, now) / wl
            rate[pid] = 3600.0 * rs
            lift[pid] = (rs + a) / (rl + a) / gl
        sub_lift = self._group_lift(self.s_short, self.s_long, now, gl, "sub")
        brand_lift = self._group_lift(self.b_short, self.b_long, now, gl, "brand")

        av_rate = sorted(((r, p) for p, r in rate.items() if p in avail), reverse=True)
        top = [p for _, p in av_rate[:self.top_n]]
        # rising: lift with some support (>= 2 weighted events in the short window)
        min_rate = 3600.0 * 2.0 / ws
        rising = [p for _, p in heapq.nlargest(
            self.top_n, ((lift[p], p) for r, p in av_rate if r >= min_rate))]
        by_sub: dict[str, list[str]] = {}
        for _, p in av_rate:
            sub = (cat.get(p) or {}).get("subcategory")
            lst = by_sub.setdefault(sub, [])
            if len(lst) < self.top_per_group:
                lst.append(p)

        ctx_top = self._group_top(self.ctx, now, avail, key_len=2)
        dev_top = self._group_top(self.dev, now, avail, key_len=1)
        buy_rate = {p: 3600.0 * self.p_buy.rate(p, now) for p in self.p_buy.d}

        live = state.live
        live["now"] = now
        live["trend_rate"] = rate
        live["trend_lift"] = lift
        live["trend_global_lift"] = gl
        live["trend_top"] = top
        live["trend_rising"] = rising
        live["trend_top_by_subcat"] = by_sub
        live["subcat_lift"] = sub_lift
        live["brand_lift"] = brand_lift
        live["buy_rate"] = buy_rate
        live["ctx_top"] = ctx_top
        live["device_top"] = dev_top

    def seed_baseline(self, history_events, catalog: dict) -> None:
        """Long-run activity share of every subcategory and brand, from shop.history_events.

        Group lift then compares the recent share with this share. Without it, the comparison is
        with a 3 h live counter, which absorbs a 20-70 min trend and understates it ~3x."""
        if hasattr(history_events, "to_dict"):
            ev = history_events
            w = ev["event_type"].map(EVENT_WEIGHT).fillna(1.0)
            pid = ev["product_id"].astype(str)
            tot = w.groupby(pid).sum()
            rows = tot.items()
        else:
            acc: dict = {}
            for e in history_events:
                p = str(e["product_id"])
                acc[p] = acc.get(p, 0.0) + EVENT_WEIGHT.get(e.get("event_type"), 1.0)
            rows = acc.items()
        sub, brand = {}, {}
        for p, v in rows:
            row = catalog.get(p) or {}
            if row.get("subcategory") is not None:
                sub[row["subcategory"]] = sub.get(row["subcategory"], 0.0) + float(v)
            if row.get("brand") is not None:
                brand[row["brand"]] = brand.get(row["brand"], 0.0) + float(v)
        self.baseline = {k: {g: v / s for g, v in d.items()} for k, d in (("sub", sub), ("brand", brand))
                         if (s := sum(d.values())) > 0}

    def _group_lift(self, short: DecayedCounter, long: DecayedCounter, now: float, gl: float,
                    kind: str = "sub") -> dict:
        """Lift per group = recent share of activity / long-run share (shrunk towards 1).
        Shares, not rates, so a shop-wide surge does not lift every group."""
        cur = dict(short.items(now))
        tot = sum(cur.values())
        if tot <= 0:
            return {}
        base = self.baseline.get(kind)
        if not base:                       # no history: long live counter as the reference
            lv = dict(long.items(now))
            lt = sum(lv.values()) or 1.0
            base = {g: v / lt for g, v in lv.items()}
        n = max(len(base), len(cur), 1)
        a = self.group_prior / n
        return {g: (v / tot + a) / (base.get(g, 1.0 / n) + a) for g, v in cur.items()}

    def _group_top(self, counter: DecayedCounter, now: float, avail, key_len: int):
        groups: dict[tuple, list[tuple[float, str]]] = {}
        for key, v in counter.items(now):
            pid = key[-1]
            if pid in avail:
                groups.setdefault(key[:key_len], []).append((v, pid))
        return {g: [p for _, p in heapq.nlargest(self.top_per_group, lst)] for g, lst in groups.items()}

    def prune(self) -> None:
        now = self.now
        for c in (self.p_short, self.p_long, self.p_buy, self.ctx, self.dev):
            c.prune(now, 1e-3)
