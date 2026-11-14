"""A tiny synthetic shop implementing `sim.base.Simulator`, used by A3's tests and smoke runs.

Why it exists: A3 must test its harness and policies before A2's full simulator (`sim/shop.py`) is
available, and unit tests should stay fast and independent of A2's parameters. It is deliberately
small (hundreds of products, a few thousand impressions) but has the mechanisms that matter:

- shoppers with a favourite category and a price sensitivity that depends on the device;
- sessions whose `recent_views` come mostly from one subcategory (session affinity);
- a choice model over the 10 shown items with position bias (items compete for one click),
  then P(purchase | click) that falls with price and with "already bought";
- a logging policy "best sellers + randomness" sampled slot by slot (Plackett-Luce), whose
  per-slot propensities are recorded exactly like `shop.history_sessions.propensity`;
- new products and stock-outs during the live stream.

Never use it to claim a score: real numbers come from A2's simulator and, later, from RawTree.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from rtrec.schemas import Feedback, Request, Submission
from sim.base import HistoryDump, Tick

POS_BIAS = np.array([1.0, 0.8, 0.65, 0.55, 0.47, 0.4, 0.35, 0.3, 0.26, 0.23])
DEVICES = ("web", "ios", "android")
REGIONS = ("Catalunya", "Madrid", "Andalucia")
PAGES = ("home", "product", "category")
T0 = 1_760_000_000.0  # history starts here (epoch seconds)


def iso(t: float) -> str:
    return pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class _User:
    uid: str
    fav_cat: int
    price_sens: float
    device: str
    region: str


class ToySim:
    def __init__(self, seed: int = 0, n_products: int = 400, n_cats: int = 5, n_sub: int = 4,
                 n_users: int = 300, req_per_s: float = 3.0):
        self.rng = np.random.default_rng(seed)
        r = self.rng
        self.n_cats, self.n_sub = n_cats, n_sub
        self.req_per_s = req_per_s
        self.products: dict[str, dict] = {}
        self.quality: dict[str, float] = {}
        for i in range(n_products):
            self._add_product(f"p_{i:05d}", T0 - 86400 * 100)
        self.users = [self._new_user(f"u_{i:05d}") for i in range(n_users)]
        self._by_id = {u.uid: u for u in self.users}
        self.bought: dict[str, set[str]] = {}
        self.stock_out: set[str] = set()
        self.now = T0 + 60 * 86400  # live stream starts after 60 days of history
        self._n_req = 0
        self._n_new = 0
        self._served: dict[str, Request] = {}
        # base popularity drives the logging policy (best sellers + randomness)
        self.pop = {p: math.exp(self.quality[p]) for p in self.products}

    # ---------- world ----------
    def _add_product(self, pid: str, created: float) -> dict:
        r = self.rng
        c = int(r.integers(self.n_cats))
        s = int(r.integers(self.n_sub))
        price = float(np.round(np.exp(r.normal(3.2 + 0.3 * c, 0.6)), 2))
        prod = {"product_id": pid, "category": f"cat{c}", "subcategory": f"cat{c}/sub{s}",
                "brand": f"brand{int(r.integers(30))}", "price_eur": price,
                "description": f"A cat{c} sub{s} item", "created_at": iso(created), "is_active": True}
        self.products[pid] = prod
        self.quality[pid] = float(r.normal(0, 0.7))
        return prod

    def _new_user(self, uid: str) -> _User:
        r = self.rng
        dev = DEVICES[int(r.integers(3))]
        return _User(uid, int(r.integers(self.n_cats)), {"web": 0.3, "ios": 0.15, "android": 0.5}[dev],
                     dev, REGIONS[int(r.integers(3))])

    def _user(self, uid: str) -> _User | None:
        return self._by_id.get(uid)

    def _available(self) -> list[str]:
        return [p for p, v in self.products.items() if v["is_active"] and p not in self.stock_out]

    def _make_request(self, t: float, new_user: bool | None = None) -> Request:
        r = self.rng
        self._n_req += 1
        if new_user is None:
            new_user = r.random() < 0.2
        if new_user:
            u = self._new_user(f"u_new{self._n_req:06d}")
            self.users.append(u)
            self._by_id[u.uid] = u
        else:
            u = self.users[int(r.integers(len(self.users)))]
        avail = self._available()
        n_views = 0 if (new_user and r.random() < 0.5) else int(r.integers(0, 6))
        views = []
        if n_views:
            cat = u.fav_cat if r.random() < 0.7 else int(r.integers(self.n_cats))
            sub = f"cat{cat}/sub{int(r.integers(self.n_sub))}"
            pool = [p for p in avail if self.products[p]["subcategory"] == sub] or avail
            for k in range(n_views):
                if r.random() < 0.25:
                    pool2 = [p for p in avail if self.products[p]["category"] == f"cat{cat}"] or avail
                    pid = pool2[int(r.integers(len(pool2)))]
                else:
                    pid = pool[int(r.integers(len(pool)))]
                views.append({"product_id": pid, "ts": iso(t - 60 * (n_views - k))})
        req: Request = {"request_id": f"r_{self._n_req:08d}", "ts": iso(t), "user_id": u.uid,
                        "is_new_user": new_user,
                        "context": {"device": u.device, "region": u.region, "page": PAGES[int(r.integers(3))]},
                        "recent_views": views}
        return req

    # ---------- hidden purchase model ----------
    def _utility(self, req: Request, pid: str) -> tuple[float, float]:
        """(attractiveness, P(purchase | click)) of one product for one request."""
        p = self.products[pid]
        u = self._user(req["user_id"])
        views = [v["product_id"] for v in req.get("recent_views") or []]
        vsub = {self.products[v]["subcategory"] for v in views if v in self.products}
        vcat = {self.products[v]["category"] for v in views if v in self.products}
        a = self.quality[pid] - 1.5
        a += 1.6 * (p["subcategory"] in vsub) + 0.6 * (p["category"] in vcat) + 0.8 * (pid in views)
        if u is not None:
            a += 0.7 * (p["category"] == f"cat{u.fav_cat}")
        sens = u.price_sens if u else 0.3
        conv = 1 / (1 + math.exp(-(0.2 - sens * (math.log(p["price_eur"]) - 3.2))))
        if u is not None and pid in self.bought.get(u.uid, ()):
            conv *= 0.1
        return math.exp(a), conv * 0.5

    def _probs(self, req: Request, items: list[str]) -> tuple[np.ndarray, np.ndarray]:
        att = np.zeros(len(items))
        conv = np.zeros(len(items))
        for k, pid in enumerate(items[:10]):
            if pid in self.products:
                a, c = self._utility(req, pid)
                att[k], conv[k] = a * POS_BIAS[k], c
        pclick = att / (1.0 + att.sum())
        return pclick, conv

    def expected_purchase(self, req: Request, items: list[str]) -> float:
        pclick, conv = self._probs(req, items)
        return float((pclick * conv).sum())

    def oracle(self, req: Request) -> list[str]:
        # greedy: best 10 by attractiveness*conv placed in order (exact for this model up to ties)
        scored = []
        for pid in self._available():
            a, c = self._utility(req, pid)
            scored.append((a * c, a, pid))
        scored.sort(reverse=True)
        best = scored[:40]
        out: list[tuple[float, float, str]] = []
        for k in range(10):  # greedy slot by slot: exact enough for this choice model
            def gain(c):
                lst = out + [c]
                att = np.array([a * POS_BIAS[i] for i, (_, a, _) in enumerate(lst)])
                cv = np.array([ac / a for ac, a, _ in lst])
                return float((att * cv).sum() / (1 + att.sum()))
            out.append(max((c for c in best if c not in out), key=gain))
        return [pid for _, _, pid in out]

    def _outcome(self, req: Request, items: list[str]) -> tuple[str | None, str | None]:
        pclick, conv = self._probs(req, items)
        r = self.rng.random()
        cum = np.cumsum(pclick)
        k = int(np.searchsorted(cum, r))
        if k >= len(items):
            return None, None
        clicked = items[k]
        if self.rng.random() < conv[k]:
            self.bought.setdefault(req["user_id"], set()).add(clicked)
            return clicked, clicked
        return clicked, None

    # ---------- logging policy ----------
    def _logging_policy(self, avail: list[str]) -> tuple[list[str], list[float]]:
        w = np.array([self.pop[p] for p in avail]) ** 1.5
        w = w / w.sum()
        w = 0.8 * w + 0.2 / len(w)
        shown, props = [], []
        mask = np.ones(len(avail), bool)
        for _ in range(10):
            ww = w * mask
            ww = ww / ww.sum()
            j = int(self.rng.choice(len(avail), p=ww))
            shown.append(avail[j])
            props.append(float(ww[j]))
            mask[j] = False
        return shown, props

    def history(self, days: int = 60, sessions_per_day: int = 200, events_per_session: int = 4) -> HistoryDump:
        sess, evs = [], []
        start = self.now - days * 86400
        avail = self._available()
        n = days * sessions_per_day
        for t in np.sort(self.rng.uniform(start, self.now, n)):
            req = self._make_request(float(t), new_user=False)
            shown, props = self._logging_policy(avail)
            clicked, purchased = self._outcome(req, shown)
            sess.append({"request_id": req["request_id"], "ts": req["ts"], "user_id": req["user_id"],
                         "context": req["context"], "recent_views": req["recent_views"],
                         "shown": shown, "propensity": props, "clicked": clicked, "purchased": purchased,
                         "revenue_eur": self.products[purchased]["price_eur"] if purchased else 0.0})
            u = self._user(req["user_id"])
            for k in range(events_per_session):
                pid = shown[k] if self.rng.random() < 0.3 else (
                    req["recent_views"][k % len(req["recent_views"])]["product_id"]
                    if req["recent_views"] else avail[int(self.rng.integers(len(avail)))])
                a, c = self._utility(req, pid)
                et = "purchase" if self.rng.random() < 0.05 * a * c else ("add_to_cart" if self.rng.random() < 0.1 else "view")
                evs.append({"ts": iso(float(t) + k), "event_type": et, "user_id": req["user_id"],
                            "session_id": "s_" + req["request_id"], "product_id": pid,
                            "price_eur": self.products[pid]["price_eur"], "device": u.device, "region": u.region})
        self.bought.clear()
        catalog = pd.DataFrame(list(self.products.values()))
        return HistoryDump(catalog=catalog, history_sessions=pd.DataFrame(sess), history_events=pd.DataFrame(evs))

    # ---------- live stream ----------
    def advance(self, now: float) -> Tick:
        tick = Tick()
        r = self.rng
        while self.now < now:
            self.now += float(r.exponential(1 / self.req_per_s))
            u = r.random()
            if u < 0.01:
                self._n_new += 1
                prod = self._add_product(f"p_new{self._n_new:05d}", self.now)
                self.pop[prod["product_id"]] = math.exp(self.quality[prod["product_id"]])
                tick.catalog_updates.append({**prod, "ts": iso(self.now), "change": "new_product"})
            elif u < 0.02:
                pid = list(self.products)[int(r.integers(len(self.products)))]
                change = "back_in_stock" if pid in self.stock_out else "out_of_stock"
                (self.stock_out.discard if change == "back_in_stock" else self.stock_out.add)(pid)
                tick.catalog_updates.append({**self.products[pid], "ts": iso(self.now), "change": change})
            req = self._make_request(self.now)
            self._served[req["request_id"]] = req
            tick.requests.append(req)
            for v in req["recent_views"][-1:]:
                u_ = self._user(req["user_id"])
                tick.events.append({"ts": req["ts"], "event_type": "view", "user_id": req["user_id"],
                                    "session_id": "s_" + req["request_id"], "product_id": v["product_id"],
                                    "price_eur": self.products[v["product_id"]]["price_eur"],
                                    "device": u_.device, "region": u_.region})
        return tick

    def judge(self, sub: Submission, received_at: float) -> Feedback:
        req = self._served[sub["request_id"]]
        clicked, purchased = self._outcome(req, sub["items"])
        return {"request_id": sub["request_id"], "ts": iso(received_at + 3), "shown": sub["items"],
                "served_by": "team", "fallback_reason": "", "latency_ms": 0.0,
                "clicked": clicked or "", "purchased": purchased or "",
                "revenue_eur": self.products[purchased]["price_eur"] if purchased else 0.0}


def make_simulator(seed: int = 0, **kw) -> ToySim:
    return ToySim(seed=seed, **kw)
