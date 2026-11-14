"""A deliberately tiny shop simulator implementing sim.base.Simulator.

Why it exists: the runtime must be testable before A2's realistic simulator (sim/shop.py) lands,
and the runtime tests only need the *mechanics* (rates, stock-outs, new products, timing), not a
realistic purchase model. Once sim/shop.py exists, FakeStore uses it unchanged; this stub stays for
fast, dependency-free runtime tests and the load test.

Determinism: traffic is generated per one-second bucket from Random(seed, second), so the stream is
identical whatever the call pattern of advance().
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from rtrec.runtime.clock import to_iso
from rtrec.schemas import N_ITEMS, CatalogUpdate, Event, Feedback, Product, Request, Submission
from sim.base import HistoryDump, Tick

CATEGORIES = [f"cat{i}" for i in range(10)]
POS_WEIGHT = [1.0 / (1 + 0.35 * k) for k in range(N_ITEMS)]  # top slots are looked at much more


@dataclass
class StubParams:
    n_products: int = 400
    request_rate: float = 3.0           # requests / s (the real shop: ~3/s)
    event_rate: float = 20.0            # organic events / s
    update_rate: float = 0.5            # catalog updates / s (stock-outs, prices, new products)
    new_user_share: float = 0.3
    deadline_s: float = 3.0


class StubSimulator:
    def __init__(self, seed: int = 0, start: float = 1_763_110_800.0, params: StubParams | None = None):
        self.p = params or StubParams()
        self.seed = seed
        self.start = float(start)
        rng = random.Random(seed)
        self.products: dict[str, Product] = {}
        for i in range(self.p.n_products):
            pid = f"p_{i:05d}"
            cat = CATEGORIES[i % len(CATEGORIES)]
            self.products[pid] = Product(
                product_id=pid, category=cat, subcategory=f"{cat}_s{i % 9}", brand=f"b{i % 37}",
                price_eur=round(rng.uniform(5, 200), 2), description=f"stub product {i}",
                created_at=to_iso(self.start - 86400 * 90), is_active=True)
        self._initial = {k: dict(v) for k, v in self.products.items()}
        self.in_stock: dict[str, bool] = {pid: True for pid in self.products}
        self.popularity = {pid: rng.paretovariate(1.2) for pid in self.products}
        self._buckets: dict[int, Tick] = {}
        self._emitted_until = self.start
        self._next_new = self.p.n_products
        self.requests: dict[str, Request] = {}
        self._fb_rng = random.Random(seed + 1)

    # ------------------------------------------------------------------ helpers
    def available(self) -> set[str]:
        return {p for p, prod in self.products.items() if prod["is_active"] and self.in_stock[p]}

    def catalog_at_start(self) -> list[Product]:
        return [dict(v) for v in self._initial.values()]

    def best_sellers(self) -> list[str]:
        return sorted(self._initial, key=lambda p: -self.popularity[p])

    def _bucket(self, sec: int) -> Tick:
        if sec in self._buckets:
            return self._buckets[sec]
        rng = random.Random(self.seed * 1_000_003 + sec)
        tick = Tick()
        pids = sorted(self._initial)
        weights = [self.popularity[p] for p in pids]
        n_req = _poisson(rng, self.p.request_rate)
        for j in range(n_req):
            ts = sec + rng.random()
            new_user = rng.random() < self.p.new_user_share
            n_views = 0 if new_user and rng.random() < 0.5 else rng.randint(1, 5)
            views = rng.choices(pids, weights=weights, k=n_views)
            tick.requests.append(Request(
                request_id=f"r_{sec}_{j}_{self.seed}", ts=to_iso(ts), user_id=f"u_{rng.randint(0, 5000)}",
                is_new_user=new_user,
                context={"device": rng.choice(["web", "ios", "android"]), "region": rng.choice(["CAT", "MAD", "AND"]),
                         "page": rng.choice(["home", "product", "category"])},
                recent_views=[{"product_id": v, "ts": to_iso(ts - 30 * (n_views - k))} for k, v in enumerate(views)]))
        for _ in range(_poisson(rng, self.p.event_rate)):
            ts = sec + rng.random()
            pid = rng.choices(pids, weights=weights)[0]
            tick.events.append(Event(
                ts=to_iso(ts), event_type=rng.choices(["view", "add_to_cart", "purchase"], [90, 7, 3])[0],
                user_id=f"u_{rng.randint(0, 5000)}", session_id=f"s_{rng.randint(0, 9999)}", product_id=pid,
                price_eur=self._initial[pid]["price_eur"], device="web", region="CAT"))
        for _ in range(_poisson(rng, self.p.update_rate)):
            ts = sec + rng.random()
            tick.catalog_updates.append(self._make_update(rng, ts))
        for k in ("requests", "events", "catalog_updates"):
            getattr(tick, k).sort(key=lambda r: r["ts"])
        self._buckets[sec] = tick
        return tick

    def _make_update(self, rng: random.Random, ts: float) -> CatalogUpdate:
        kind = rng.choices(["out_of_stock", "back_in_stock", "price_change", "new_product"], [4, 3, 2, 1])[0]
        if kind == "new_product":
            pid = f"p_{self._next_new:05d}"
            self._next_new += 1
            cat = rng.choice(CATEGORIES)
            prod = Product(product_id=pid, category=cat, subcategory=f"{cat}_s{rng.randint(0, 8)}", brand="bnew",
                           price_eur=round(rng.uniform(5, 200), 2), description="new stub product",
                           created_at=to_iso(ts), is_active=True)
        else:
            pid = f"p_{rng.randrange(self.p.n_products):05d}"
            prod = dict(self._initial[pid])
            if kind == "price_change":
                prod["price_eur"] = round(prod["price_eur"] * rng.uniform(0.7, 1.3), 2)
        return CatalogUpdate(**prod, ts=to_iso(ts), change=kind)

    # ------------------------------------------------------------------ Simulator protocol
    def history(self, days: int = 60) -> HistoryDump:
        import pandas as pd
        return HistoryDump(catalog=pd.DataFrame(self.catalog_at_start()),
                           history_sessions=pd.DataFrame([]), history_events=pd.DataFrame([]))

    def advance(self, now: float) -> Tick:
        """Everything with emitted_until < ts <= now, in ts order. Applies catalog updates to the shop."""
        from rtrec.runtime.clock import parse_ts
        out = Tick()
        lo, hi = self._emitted_until, now
        if hi <= lo:
            return out
        for sec in range(int(lo), int(hi) + 1):
            b = self._bucket(sec)
            for k in ("requests", "events", "catalog_updates"):
                getattr(out, k).extend(r for r in getattr(b, k) if lo < parse_ts(r["ts"]) <= hi)
        self._emitted_until = hi
        for r in out.requests:
            self.requests[r["request_id"]] = r
        for u in out.catalog_updates:
            pid = u["product_id"]
            self.products[pid] = Product(**{k: u[k] for k in Product.__annotations__})
            if u["change"] == "out_of_stock":
                self.in_stock[pid] = False
            elif u["change"] in ("back_in_stock", "new_product"):
                self.in_stock[pid] = True
            if pid not in self.popularity:
                self.popularity[pid] = 1.0
        return out

    def _affinity(self, req: Request, pid: str) -> float:
        subs = {self.products[v["product_id"]]["subcategory"] for v in req.get("recent_views") or []
                if v["product_id"] in self.products}
        prod = self.products.get(pid)
        a = 0.01 * min(self.popularity.get(pid, 1.0), 20)
        if prod and prod["subcategory"] in subs:
            a += 0.05
        return min(a, 0.5)

    def expected_purchase(self, req: Request, items: list[str]) -> float:
        miss = 1.0
        for k, pid in enumerate(items[:N_ITEMS]):
            miss *= 1 - POS_WEIGHT[k] * self._affinity(req, pid) * 0.3
        return 1 - miss

    def oracle(self, req: Request) -> list[str]:
        avail = self.available()
        return sorted(avail, key=lambda p: -self._affinity(req, p))[:N_ITEMS]

    def judge(self, sub: Submission, received_at: float) -> Feedback:
        from rtrec.runtime.clock import parse_ts
        req = self.requests[sub["request_id"]]
        items = list(sub.get("items") or [])
        latency = received_at - parse_ts(req["ts"])
        avail = self.available()
        reason = ""
        if latency > self.p.deadline_s:
            reason = "late"
        elif len(items) != N_ITEMS or len(set(items)) != N_ITEMS or any(p not in avail for p in items):
            reason = "invalid"
        shown = items if not reason else [p for p in self.best_sellers() if p in avail][:N_ITEMS]
        p_buy = self.expected_purchase(req, shown)
        bought = shown[0] if self._fb_rng.random() < p_buy else ""
        return Feedback(request_id=sub["request_id"], ts=to_iso(received_at), shown=shown,
                        served_by="fallback" if reason else "team", fallback_reason=reason,
                        latency_ms=round(latency * 1000, 1), clicked=bought, purchased=bought,
                        revenue_eur=self.products[bought]["price_eur"] if bought else 0.0)


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth; lam is small (<= ~300) in every use here
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, lam ** 0.5))))
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1
