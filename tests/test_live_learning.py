"""Feedback calibration, Thompson exploration, A/B and the bench harness, on a tiny toy simulator.

The toy sim only exists to exercise the plumbing; real measurements use A2's sim/shop.py."""
import math
import random
from datetime import datetime, timezone

import pandas as pd

from rtrec import live
from rtrec.contract import State
from rtrec.live.ab import ABRouter, ABTest, install
from rtrec.live.bandit import ThompsonExplorer
from rtrec.live.bench import run
from rtrec.live.policy import LiveSessionPolicy, LiveWrapper
from rtrec.validate import is_valid
from sim.base import HistoryDump, Tick

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc).timestamp()
POS = [0.30 / (k + 1) ** 0.7 for k in range(10)]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class ToySim:
    start = T0

    def __init__(self, seed=0, n=200):
        self.rng = random.Random(seed)
        self.cat = {f"p{i}": {"product_id": f"p{i}", "category": f"c{i % 3}", "subcategory": f"s{i % 10}",
                              "brand": f"b{i % 7}", "price_eur": 5.0 + i % 40, "description": "nice thing",
                              "created_at": "2026-01-01T00:00:00Z", "is_active": True} for i in range(n)}
        self.trending = set(self.rng.sample(sorted(self.cat), 10))
        self.oos = set()
        self.t = T0
        self.next_req = T0
        self.nreq = 0
        self.users = {}

    def history(self, days=7):
        ev = [{"ts": iso(T0 - 1000 + i), "event_type": "purchase", "user_id": "h", "session_id": "h",
               "product_id": f"p{i % 20}", "price_eur": 1.0, "device": "web", "region": "R"} for i in range(200)]
        return HistoryDump(pd.DataFrame(list(self.cat.values())), pd.DataFrame(), pd.DataFrame(ev))

    def attract(self, req, p):
        row = self.cat[p]
        a = 0.02
        subs = {self.cat[v["product_id"]]["subcategory"] for v in req["recent_views"]}
        if row["subcategory"] in subs:
            a += 0.25
        if p in self.trending:
            a += 0.3
        return a

    def advance(self, now):
        tick = Tick()
        avail = [p for p in self.cat if p not in self.oos]
        while self.t < now:
            self.t += 0.1
            if self.rng.random() < 0.6:
                p = self.rng.choice(sorted(self.trending)) if self.rng.random() < 0.5 else self.rng.choice(avail)
                tick.events.append({"ts": iso(self.t), "event_type": "view" if self.rng.random() < 0.9 else "purchase",
                                    "user_id": f"u{self.rng.randrange(50)}", "session_id": f"x{self.rng.randrange(50)}",
                                    "product_id": p, "price_eur": 1.0, "device": "ios", "region": "R"})
            if self.rng.random() < 0.002:
                p = self.rng.choice(avail)
                self.oos.add(p)
                tick.catalog_updates.append({**self.cat[p], "ts": iso(self.t), "change": "out_of_stock"})
            if self.rng.random() < 0.002:
                pid = f"n{len(self.cat)}"
                self.cat[pid] = {**self.cat["p1"], "product_id": pid, "subcategory": f"s{self.rng.randrange(10)}"}
                tick.catalog_updates.append({**self.cat[pid], "ts": iso(self.t), "change": "new_product"})
                self.trending.add(pid)
            if self.t >= self.next_req:
                self.next_req += 0.33
                self.nreq += 1
                new = self.rng.random() < 0.3
                uid = f"new{self.nreq}" if new else f"u{self.rng.randrange(50)}"
                views = [{"product_id": self.rng.choice(avail), "ts": iso(self.t - 5)}
                         for _ in range(self.rng.randrange(3))]
                tick.requests.append({"request_id": f"r{self.nreq}", "ts": iso(self.t), "user_id": uid,
                                      "is_new_user": new, "context": {"device": "ios", "region": "R", "page": "home"},
                                      "recent_views": views})
                self._last = tick.requests[-1]
                self.users[f"r{self.nreq}"] = tick.requests[-1]
        return tick

    def expected_purchase(self, req, items):
        q = 1.0
        for k, p in enumerate(items[:10]):
            q *= 1 - POS[k] * self.attract(req, p)
        return 1 - q

    def oracle(self, req):
        avail = [p for p in self.cat if p not in self.oos]
        return sorted(avail, key=lambda p: -self.attract(req, p))[:10]

    def judge(self, sub, received_at):
        req = self.users[sub["request_id"]]
        bought = ""
        for k, p in enumerate(sub["items"]):
            if self.rng.random() < POS[k] * self.attract(req, p):
                bought = p
                break
        return {"request_id": sub["request_id"], "ts": iso(received_at + 3), "shown": sub["items"],
                "served_by": "team", "fallback_reason": "", "latency_ms": 800.0, "clicked": bought,
                "purchased": bought, "revenue_eur": 10.0 if bought else 0.0}


class SafeList:
    name = "best-sellers"

    def recommend(self, req, state):
        return state.safe_list


def test_bench_live_policies_beat_baseline_and_stay_valid():
    sim = ToySim()
    checked = []

    def check(now, state):
        assert not (state.available & sim.oos)        # tracker never shows an OOS product
        checked.append(1)

    res = run(sim, {"live-session": LiveSessionPolicy(), "best-sellers": SafeList(),
                    "wrapped": LiveWrapper(LiveSessionPolicy(), explore=True)}, minutes=6, on_tick=check)
    s = res.scores
    assert res.n_requests > 900 and checked
    assert abs(s["best-sellers"]["all"].normalised) < 1e-6
    assert s["live-session"]["all"].normalised > 20
    assert s["wrapped"]["all"].normalised > 20
    assert s["live-session"]["cold"].n > 100
    assert max(res.latency_ms.values()) < 5.0
    st = live.learner  # noqa: F841  (smoke: table renders)
    assert "live-session" in res.table()


def test_calibration_learns_good_and_bad_products():
    s = State()
    live.init(s, [{"product_id": f"p{i}", "subcategory": "s", "category": "c", "brand": "b", "price_eur": 1,
                   "description": "", "created_at": "", "is_active": True} for i in range(10)])
    rng = random.Random(3)
    rows = []
    for i in range(4000):
        shown = [f"p{(i + k) % 10}" for k in range(10)]
        bought = ""
        for k, p in enumerate(shown):
            rate = 0.08 if p == "p0" else (0.0 if p == "p9" else 0.02)
            if rng.random() < rate:
                bought = p
                break
        rows.append({"request_id": f"r{i}", "ts": iso(T0 + i), "shown": shown, "served_by": "team",
                     "fallback_reason": "", "latency_ms": 900, "clicked": bought, "purchased": bought,
                     "revenue_eur": 0})
    live.on_feedback(rows, s)
    live.on_feedback(rows[:100], s)                    # duplicates ignored
    live.learner(s).flush(s)
    m = s.live["calib_product"]
    assert m["p0"] > 2.0 and m["p9"] < 0.3 and 0.5 < m["p5"] < 1.5
    assert s.live["impressions"]["p0"] == 4000
    assert s.live["reliability"]["p99_ms"] == 900


def test_thompson_explores_last_slots_only_and_logs_propensity():
    s = State()
    live.init(s, [{"product_id": f"p{i}", "subcategory": "s", "category": "c", "brand": "b", "price_eur": 1,
                   "description": "", "created_at": "", "is_active": True} for i in range(40)])
    ex = ThompsonExplorer()
    ranked = [f"p{i}" for i in range(30)]
    outs = set()
    for i in range(200):
        req = {"request_id": f"r{i}", "recent_views": []}
        out = ex.apply(req, ranked, s)
        assert out[:8] == ranked[:8] and len(set(out)) == len(out) >= 10
        assert out == ex.apply(req, ranked, s)[:len(out)]       # deterministic per request_id
        outs.add(tuple(out[8:10]))
    assert len(outs) > 10
    assert all(0 < pr <= 1 for *_, pr in ex.log)


def test_ab_split_report_and_promotion():
    t = ABTest([("champ", 0.5), ("chall", 0.5)], min_n=500)
    arms = [t.assign(f"r{i}") for i in range(10_000)]
    assert 0.47 < arms.count("champ") / len(arms) < 0.53
    assert all(t.assign(f"r{i}") == arms[i] for i in range(100))   # deterministic
    s = State()
    install(s, t)
    rng = random.Random(1)
    rows = []
    for i in range(6000):
        rate = 0.10 if t.assign(f"r{i}") == "chall" else 0.05
        b = "p1" if rng.random() < rate else ""
        rows.append({"request_id": f"r{i}", "ts": iso(T0 + i), "shown": ["p1"], "served_by": "team",
                     "fallback_reason": "", "latency_ms": 1, "clicked": b, "purchased": b, "revenue_eur": 0})
    live.on_feedback(rows, s)
    live.learner(s).flush(s)
    rep = s.live["ab_report"]
    assert rep["promote"] == "chall"
    lo, hi = rep["arms"]["chall"]["ci95"]
    assert lo < 10 < hi and math.isclose(sum(a["n"] for a in rep["arms"].values()), 6000)
    router = ABRouter(t, {"champ": SafeList(), "chall": LiveSessionPolicy()})
    s.safe_list = ["p1"]
    s.available = frozenset(["p1"])
    assert router.version_for({"request_id": "r5"}) == arms[5]
    assert is_valid(router.recommend({"request_id": "r5", "recent_views": []}, s) or ["p1"], s, n=1)


def test_runtime_hook_adapter_and_chunk_latency():
    import time
    from rtrec.live.hook import LiveHook
    from rtrec.runtime.catalog import CatalogTracker as A1Tracker
    s = State()
    a1 = A1Tracker()                              # the runtime owns the contract fields
    a1.load([{"product_id": f"p{i}", "category": "c", "subcategory": f"s{i % 90}", "brand": "b",
              "price_eur": 3.0, "description": "", "created_at": "", "is_active": True} for i in range(5000)])
    a1.publish(s)
    h = LiveHook()
    h.on_start(s)
    assert len(s.available) == 5000
    upd = {**s.catalog["p7"], "price_eur": 2.0, "ts": iso(T0 + 1), "change": "price_change"}
    a1.apply([upd])
    a1.publish(s)
    before = s.available
    h.on_catalog_updates([upd], s)
    assert s.available is before                  # LiveHook never writes contract fields
    assert s.live["price"]["p7"].last_change_ratio == 2.0 / 3.0
    rng = random.Random(0)
    evs = [{"ts": iso(T0 + i * 0.02), "event_type": "view", "user_id": f"u{rng.randrange(9000)}",
            "session_id": f"x{rng.randrange(9000)}", "product_id": f"p{rng.randrange(5000)}", "price_eur": 3.0,
            "device": "web", "region": "R"} for i in range(2000)]
    t0 = time.perf_counter()
    h.on_events(evs, s)
    h.on_tick(s, T0 + 40)
    assert (time.perf_counter() - t0) < 0.2       # A1 budget is ~50 ms per call on a normal chunk
    req = {"request_id": "r1", "ts": iso(T0 + 41), "user_id": "u1", "is_new_user": True,
           "context": {"device": "web", "region": "R", "page": "home"}, "recent_views": []}
    install(s, ABTest([("a", 1), ("b", 1)]))
    h.on_served([(req, ["p1"] * 10, "b")], s)
    h.on_feedback([{"request_id": "r1", "ts": iso(T0 + 45), "shown": ["p1"], "served_by": "team",
                    "fallback_reason": "", "latency_ms": 700, "clicked": "p1", "purchased": "p1",
                    "revenue_eur": 3}], s)
    live.learner(s).flush(s)
    assert s.live["ab_report"]["arms"]["b"]["n"] == 1 and s.live["ab_report"]["arms"]["b"]["cold_n"] == 1
    assert s.users["u1"].purchases[-1][1] == "p1"
