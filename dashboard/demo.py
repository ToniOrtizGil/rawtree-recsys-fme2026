"""DemoStore: a tiny synthetic shop + a fake serving system, so the dashboard runs with no key and no
other agent's code. It implements rtrec.store.Store (plus now() and dump() for tests).

Everything here is SYNTHETIC and labelled as such on the page. It exists to exercise every panel:
two model versions in an A/B split, cold and warm shoppers, a trend that moves every 10 minutes, new
products and stock-outs, realistic latency, and (with chaos=True) a 75 s outage every 20 minutes so the
red light and the alerts can be demonstrated. The purchase model is a toy, not A2's simulator.

Rows are generated lazily up to clock() on every read, so a fake clock gives deterministic tests.
"""
from __future__ import annotations

import bisect
import math
import random
import threading
import time
import zlib

from .metrics import to_iso

CATEGORIES = ["fashion", "home", "sports", "beauty", "electronics", "toys", "books", "garden", "food", "pets"]
REGIONS = ["Catalunya", "Madrid", "Andalucia", "Valencia", "Galicia", "Euskadi"]
DEVICES = ["web", "ios", "android"]
PAGES = ["home", "product", "category"]
FEEDBACK_DELAY_S = 5.0          # feedback lands a few seconds after the 3 s deadline
OUTAGE_EVERY_S, OUTAGE_LEN_S = 1200, 75


class _Table:
    """Append-only rows kept sorted by epoch ts, read with a cursor like the real tables."""

    def __init__(self):
        self.t: list[float] = []
        self.rows: list[dict] = []

    def add(self, t: float, row: dict) -> None:
        i = bisect.bisect_right(self.t, t)
        self.t.insert(i, t)
        self.rows.insert(i, row)

    def since(self, cursor: float, limit: int, overlap: float = 1.0) -> list[dict]:
        i = bisect.bisect_right(self.t, cursor - overlap)
        return [dict(r) for r in self.rows[i:i + limit]]

    def trim(self, before: float) -> None:
        i = bisect.bisect_left(self.t, before)
        del self.t[:i], self.rows[:i]


class DemoStore:
    team = "team_demo"
    kind = "demo"

    def __init__(self, seed: int = 7, start: float | None = None, clock=time.time, rate: float = 3.0,
                 events_per_s: float = 15.0, chaos: bool = False, keep_s: float = 3 * 3600):
        self.rng = random.Random(seed)
        self.clock = clock
        self.rate, self.events_per_s, self.chaos, self.keep_s = rate, events_per_s, chaos, keep_s
        self.t = float(math.floor(start if start is not None else clock() - 900))   # 15 min backfill
        self.t0 = self.t
        self._lock = threading.Lock()
        self.requests, self.events, self.updates = _Table(), _Table(), _Table()
        self.feedback, self.scratch = _Table(), _Table()
        self.submissions: dict[str, dict] = {}
        self.sub_log = _Table()                   # submissions by insert time (they carry no ts)
        self._pending: list[tuple[float, dict]] = []
        self._make_catalog()
        self.feedback.add(self.t, {"request_id": "__init__", "ts": to_iso(self.t), "shown": [],
                                   "served_by": "team", "fallback_reason": "", "latency_ms": 0,
                                   "clicked": "", "purchased": "", "revenue_eur": 0})

    # ---------------------------------------------------------------- world
    def _make_catalog(self) -> None:
        r = self.rng
        self.products: dict[str, dict] = {}
        self.by_sub: dict[str, list[str]] = {}
        for c in CATEGORIES:
            for j in range(6):
                sub = f"{c}/{c[:3]}{j}"
                for _ in range(10):
                    self._new_product(sub, created_at=self.t - 86400 * r.randint(30, 400))
        self.available = set(self.products)
        self.new_products: set[str] = set()
        self.bestsellers = r.sample(sorted(self.products), 30)    # the organisers' fixed fallback list
        self.users = [f"u_{i:05d}" for i in range(2000)]
        self.taste = {u: r.choice(sorted(self.by_sub)) for u in self.users}

    def _new_product(self, sub: str, created_at: float) -> str:
        pid = f"p_{len(self.products):05x}"
        cat = sub.split("/")[0]
        self.products[pid] = {"product_id": pid, "category": cat, "subcategory": sub,
                              "brand": f"brand_{self.rng.randint(1, 40)}",
                              "price_eur": round(self.rng.lognormvariate(3.3, 0.6), 2),
                              "description": f"A {sub.split('/')[1]} item from the {cat} range.",
                              "created_at": to_iso(created_at), "is_active": True}
        self.by_sub.setdefault(sub, []).append(pid)
        return pid

    def _trending(self, t: float) -> str:
        subs = sorted(self.by_sub)
        return subs[zlib.crc32(str(int(t // 600)).encode()) % len(subs)]

    def _outage(self, t: float) -> bool:
        return self.chaos and (t - self.t0) % OUTAGE_EVERY_S > OUTAGE_EVERY_S - OUTAGE_LEN_S

    def _poisson(self, lam: float) -> int:
        n, p, l = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p < l:
                return n
            n += 1

    # ---------------------------------------------------------------- one simulated second
    def _second(self, t: float) -> None:
        r = self.rng
        trend = self._trending(t)
        for _ in range(self._poisson(self.events_per_s)):
            sub = trend if r.random() < 0.25 else r.choice(sorted(self.by_sub))
            pid = r.choice(self.by_sub[sub])
            et = "view" if r.random() < 0.9 else ("add_to_cart" if r.random() < 0.7 else "purchase")
            ts = t + r.random()
            self.events.add(ts, {"ts": to_iso(ts), "event_type": et, "user_id": r.choice(self.users),
                                 "session_id": f"s_{r.randint(0, 99999)}", "product_id": pid,
                                 "price_eur": self.products[pid]["price_eur"], "device": r.choice(DEVICES),
                                 "region": r.choice(REGIONS)})
        u = r.random()
        if u < 0.02:
            self._update(t, self._new_product(r.choice(sorted(self.by_sub)), t), "new_product")
        elif u < 0.05 and len(self.available) > 100:
            self._update(t, r.choice(sorted(self.available)), "out_of_stock")
        elif u < 0.08:
            gone = sorted(set(self.products) - self.available)
            if gone:
                self._update(t, r.choice(gone), "back_in_stock")
        if int(t) % 2 == 0 and not self._outage(t):
            self.scratch.add(t, {"kind": "heartbeat", "ts": to_iso(t), "role": "primary", "host": "demo",
                                 "model_version": "demo-v1-session"})
        for _ in range(self._poisson(self.rate)):
            self._request(t + r.random())

    def _update(self, t: float, pid: str, change: str) -> None:
        p = self.products[pid]
        if change == "new_product":
            self.new_products.add(pid)
        p["is_active"] = change != "out_of_stock"
        (self.available.add if p["is_active"] else self.available.discard)(pid)
        self.updates.add(t, {**p, "ts": to_iso(t), "change": change})

    def _request(self, ts: float) -> None:
        r = self.rng
        rid = f"r_{int(ts * 1000):x}{r.randint(0, 0xfff):03x}"
        new = r.random() < 0.25
        user = f"u_new{r.randint(0, 10**6)}" if new else r.choice(self.users)
        sub = self.taste.get(user) or r.choice(sorted(self.by_sub))
        views = []
        for k in range(r.choice([0, 0, 1, 2, 3, 4])):
            pool = sorted(self.new_products) if (self.new_products and r.random() < 0.1) else self.by_sub[sub]
            views.append({"product_id": r.choice(pool), "ts": to_iso(ts - 60 * (k + 1))})
        req = {"request_id": rid, "ts": to_iso(ts), "user_id": user, "is_new_user": new,
               "context": {"device": r.choice(DEVICES), "region": r.choice(REGIONS), "page": r.choice(PAGES)},
               "recent_views": views}
        self.requests.add(ts, req)
        self._serve(req, ts)

    # ---------------------------------------------------------------- the fake serving system + judge
    def _serve(self, req: dict, ts: float) -> None:
        r = self.rng
        rid = req["request_id"]
        version = "demo-v1-session" if zlib.crc32(rid.encode()) % 2 else "demo-v0-bestsellers"
        viewed = [v["product_id"] for v in req["recent_views"]]
        if version == "demo-v1-session" and viewed:
            subs = {self.products[p]["subcategory"] for p in viewed}
            cand = viewed + [p for s in subs for p in self.by_sub[s] if p not in viewed]
        else:
            cand = []
        items = [p for p in dict.fromkeys(cand + self.bestsellers) if p in self.available][:10]
        latency = r.lognormvariate(math.log(900), 0.3)
        reason = ""
        if self._outage(ts):
            reason = "missing"
        elif r.random() < 0.004:
            reason, latency = "late", 3000 + r.random() * 1500
        elif r.random() < 0.003:
            reason = "invalid"
        if reason != "missing":
            self.submissions[rid] = {"request_id": rid, "items": items, "model_version": version}
            self.sub_log.add(ts + latency / 1000, self.submissions[rid])
        shown = items if not reason else [p for p in self.bestsellers if p in self.available][:10]
        match = sum(1 / (1 + i) for i, p in enumerate(shown)
                    if self.products[p]["subcategory"] in {self.products[v]["subcategory"] for v in viewed})
        p_buy = 0.02 + 0.05 * min(match, 2.0)
        clicked = purchased = ""
        if r.random() < min(0.9, 3 * p_buy):
            clicked = shown[min(int(r.expovariate(0.5)), len(shown) - 1)]
            if r.random() < 1 / 3:
                purchased = clicked
        due = ts + FEEDBACK_DELAY_S + r.random()
        self._pending.append((due, {
            "request_id": rid, "ts": to_iso(due), "shown": shown,
            "served_by": "fallback" if reason else "team", "fallback_reason": reason,
            "latency_ms": 0 if reason == "missing" else round(latency, 1),
            "clicked": clicked, "purchased": purchased,
            "revenue_eur": self.products[purchased]["price_eur"] if purchased else 0}))

    def _advance(self) -> None:
        now = self.clock()
        with self._lock:
            while self.t + 1 <= now:
                self._second(self.t)
                self.t += 1
            still = []
            for due, row in self._pending:
                (self.feedback.add(due, row) if due <= now else still.append((due, row)))
            self._pending = still
            if self.t - self.t0 > self.keep_s:
                for tab in (self.requests, self.events, self.updates, self.feedback, self.scratch, self.sub_log):
                    tab.trim(now - self.keep_s)

    # ---------------------------------------------------------------- rtrec.store.Store
    def now(self) -> float:
        return self.clock()

    def catalog(self) -> list[dict]:
        self._advance()
        return [dict(p) for p in self.products.values()]

    def requests_since(self, cursor: float, limit: int = 500) -> list[dict]:
        self._advance()
        return self.requests.since(cursor, limit)

    def events_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        self._advance()
        return self.events.since(cursor, limit)

    def catalog_updates_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        self._advance()
        return self.updates.since(cursor, limit)

    def feedback_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        self._advance()
        return self.feedback.since(cursor, limit)

    def insert_submissions(self, rows: list[dict]) -> None:
        raise PermissionError("DemoStore answers by itself; the dashboard is read-only")

    def insert_scratch(self, rows: list[dict]) -> None:
        for row in rows:
            self.scratch.add(self.clock(), dict(row))

    def query(self, sql: str) -> list[dict]:
        raise NotImplementedError("DemoStore has no SQL engine (tests use chdb for that)")

    def submissions_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        self._advance()
        return [r for r in self.sub_log.since(cursor, limit) if r]

    def scratch_since(self, cursor: float, limit: int = 10_000, kind: str | None = None) -> list[dict]:
        self._advance()
        return [r for r in self.scratch.since(cursor, limit) if kind is None or r.get("kind") == kind]

    def dump(self) -> dict[str, list[dict]]:
        """All tables as row lists (for loading into chdb in the SQL tests)."""
        self._advance()
        return {"shop.requests": self.requests.rows, "shop.events": self.events.rows,
                "shop.catalog_updates": self.updates.rows,
                "shop.catalog": [p for p in self.products.values() if p["product_id"] not in self.new_products],
                f"{self.team}.feedback": self.feedback.rows, f"{self.team}.scratch": self.scratch.rows,
                f"{self.team}.submissions": list(self.submissions.values())}
