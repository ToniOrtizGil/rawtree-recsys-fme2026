"""ShopSimulator: a synthetic RawTree shop implementing sim.base.Simulator.

    sim = make_simulator(seed=7)                 # or make_simulator(7, SimConfig.small()) in tests
    dump = sim.history(days=60)                  # catalog / history_sessions / history_events DataFrames
    tick = sim.advance(sim.stream_start + 10)    # requests, events, catalog_updates with ts <= now
    fb = sim.judge(submission, received_at)      # organisers' judging: deadline, validity, click/purchase
    sim.expected_purchase(req, items); sim.oracle(req); sim.report()

Design choices (see docs/FORENSICS.md for what the data looks like):
- One hidden purchase model (sim/model.py) labels both the history and the live carousel, exactly
  like the real challenge. The old carousel's propensities are exact.
- The live stream is generated in fixed 5 s blocks from RNG streams keyed by (seed, block), and a
  block is generated only after everything before it has been applied, so the stream does not
  depend on how often you call advance(). It does depend on what you sell: judged purchases enter
  the repeat-purchase ledger, as in the real game.
- Timestamps in the emitted rows are ISO strings like RawTree's; internally everything is epoch seconds.
"""
from __future__ import annotations

import heapq
import zlib
from typing import Any

import numpy as np
import pandas as pd

from rtrec.schemas import CatalogUpdate, Event, Feedback, Request, Submission

from .base import HistoryDump, Tick
from .behaviour import MAX_VIEWS, OldCarousel, organic_actions, pick_intent, pick_page, sample_views, view_times
from .model import PurchaseModel, Sessions
from .params import SimConfig
from .world import DEVICES, PAGES, World, iso

DAY = 86400.0
EVENT_TYPES = np.array(["view", "add_to_cart", "purchase"])


def _last(vt: np.ndarray, default: np.ndarray) -> np.ndarray:
    """Time of the last view of each session (default when it has none)."""
    m = np.where(np.isnan(vt), -np.inf, vt).max(1)
    return np.where(np.isfinite(m), m, default)


class ShopSimulator:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.world = World(cfg, np.random.default_rng([cfg.seed, 0]))
        self.model = PurchaseModel(self.world, cfg.hidden)
        self.carousel = OldCarousel(cfg.logging_rank_power, cfg.logging_floor)
        self.stream_start = cfg.stream_start
        self.now = cfg.stream_start
        self._history: HistoryDump | None = None
        self._hist_sessions: Sessions | None = None
        self._hist_index: dict[str, int] = {}
        self._hist_purchases = np.zeros(0)
        # live
        self._heap: list[tuple[float, int, str, Any]] = []
        self._seq = 0
        self._next_block = -int(600 / cfg.block_s)        # pre-warm 10 min so requests flow from t=0
        self._live: dict[str, tuple[Request, Sessions, float]] = {}
        self._live_users: list[int] = []
        self.new_products: set[int] = set()
        self._feedback: dict[str, Feedback] = {}
        self._stats: dict[str, dict] = {}
        self._fallback_order: list[int] | None = None

    # ================================================================== history
    def _hist_user_sampler(self):
        a = self.world.users.activity[: self.world.n_history_users]
        return a / a.sum()

    def _diurnal_times(self, n: int, t0: float, rng: np.random.Generator) -> np.ndarray:
        """n timestamps in [t0, t0 + 1 day) with a day/night cycle (peak ~20:00 local)."""
        x = rng.uniform(0, 1, size=4 * n + 16)
        hour = (x * 24 + 1) % 24  # UTC -> CET
        keep = rng.random(len(x)) < 0.55 + 0.45 * np.sin(2 * np.pi * (hour - 14) / 24)
        x = x[keep][:n]
        return np.sort(t0 + x * DAY)

    def _sessions(self, user: np.ndarray, start: np.ndarray, n_views: np.ndarray, rng: np.random.Generator,
                  fixed_device: bool = False) -> tuple[Sessions, np.ndarray]:
        w = self.world
        region = w.users.region[user]
        device = np.where(rng.random(len(user)) < 0.85, w.users.device[user], rng.integers(0, 3, len(user)))
        intent = pick_intent(w, self.model, user, region, start, rng)
        views = sample_views(w, self.model, user, region, device, intent, start, n_views, rng)
        vt = view_times(start, views, rng)
        s = Sessions(user, region, device, np.zeros(len(user), dtype=np.int64), intent, start.copy(), views)
        return s, vt

    def _events_frame(self, s: Sessions, vt: np.ndarray, cart: np.ndarray, buy: np.ndarray,
                      sid: list[str], rng: np.random.Generator) -> dict[str, np.ndarray]:
        """Flatten (B, V) views/carts/purchases into event columns (numeric, ids resolved later)."""
        cart_t = vt + rng.exponential(30, vt.shape)
        buy_t = cart_t + rng.exponential(60, vt.shape)
        cols = {k: [] for k in ("t", "type", "user", "sess", "item", "device", "region")}
        for typ, mask, tt in ((0, s.views >= 0, vt), (1, cart, cart_t), (2, buy, buy_t)):
            bi, vi = np.nonzero(mask)
            cols["t"].append(tt[bi, vi])
            cols["type"].append(np.full(len(bi), typ))
            cols["user"].append(s.user[bi])
            cols["sess"].append(np.asarray(sid, dtype=object)[bi] if len(sid) else np.zeros(0, object))
            cols["item"].append(s.views[bi, vi])
            cols["device"].append(s.device[bi])
            cols["region"].append(s.region[bi])
        return {k: np.concatenate(v) for k, v in cols.items()}

    def _ids(self, prefix: str, n: int, rng: np.random.Generator, nhex: int = 10) -> list[str]:
        return [f"{prefix}_{x:0{nhex}x}" for x in rng.integers(0, 16 ** nhex, size=n, dtype=np.int64)]

    def _ensure_history(self) -> None:
        if self._history is not None:
            return
        cfg, w, m = self.cfg, self.world, self.model
        rng = np.random.default_rng([cfg.seed, 2])
        h0 = cfg.stream_start - cfg.history_days * DAY
        pu = self._hist_user_sampler()
        N = w.products.n
        purchases = np.zeros(N)
        prior = 3.0 * np.exp(w.products.q[:N]) / np.exp(w.products.q[:N]).mean()
        ev_parts, sess_rows, sess_parts = [], [], []
        for d in range(cfg.history_days):
            t0 = h0 + d * DAY
            # ---- organic browsing (history_events)
            n = rng.poisson(cfg.history_organic_sessions_per_day)
            start = self._diurnal_times(n, t0, rng)
            user = rng.choice(len(pu), size=len(start), p=pu)
            nv = np.minimum(1 + rng.poisson(3.5, len(start)), MAX_VIEWS)
            s, vt = self._sessions(user, start, nv, rng)
            s.page[:] = 1
            s.t = _last(vt, start)
            cart, buy = organic_actions(m, s, rng)
            ev = self._events_frame(s, vt, cart, buy, self._ids("s", len(start), rng, 8), rng)
            ev_parts.append(ev)
            # ---- old carousel impressions (history_sessions)
            n = rng.poisson(cfg.history_requests_per_day)
            start = self._diurnal_times(n, t0, rng)
            user = rng.choice(len(pu), size=len(start), p=pu)
            nv = np.where(rng.random(len(start)) < cfg.no_view_share, 0,
                          np.minimum(1 + rng.poisson(2.0, len(start)), MAX_VIEWS))
            s, vt = self._sessions(user, start, nv, rng)
            s.page = pick_page(nv, rng)
            s.t = np.where(nv > 0, _last(vt, start) + rng.exponential(10, len(start)), start)
            # recent views are organic views too: they also appear in history_events
            ev_parts.append(self._events_frame(s, vt, np.zeros_like(s.views, bool), np.zeros_like(s.views, bool),
                                               self._ids("s", len(start), rng, 8), rng))
            avail = w.available(t0)
            wts = self.carousel.weights(purchases + prior, avail)
            shown, prop = self.carousel.sample(wts, len(start), rng)
            c, b = m.scores(s, shown)
            pclick = c * m.examine[None, :]
            r = rng.random(len(start))
            cum = np.cumsum(pclick, 1)
            clicked_slot = np.where(r < cum[:, -1], (r[:, None] >= cum).sum(1), -1)
            slot = np.maximum(clicked_slot, 0)
            pb = np.take_along_axis(b, slot[:, None], 1)[:, 0]
            bought = (clicked_slot >= 0) & (rng.random(len(start)) < pb)
            sess_rows.append(dict(rid=self._ids("r", len(start), rng, 12), s=s, vt=vt, shown=shown, prop=prop,
                                  slot=clicked_slot, bought=bought))
            sess_parts.append(s)
            # ---- end of day: update best sellers and the repeat-purchase ledger
            pm = ev["type"] == 2
            np.add.at(purchases, ev["item"][pm], 1)
            for u, i, t in zip(ev["user"][pm], ev["item"][pm], ev["t"][pm]):
                m.record_purchase(u, i, t)
            bi = np.nonzero(bought)[0]
            items = shown[bi, slot[bi]]
            np.add.at(purchases, items, 1)
            for u, i, t in zip(s.user[bi], items, s.t[bi]):
                m.record_purchase(u, i, t + 30)
        self._hist_purchases = purchases
        self._history = self._history_frames(ev_parts, sess_rows)
        self._hist_sessions = Sessions.concat(sess_parts)

    def _history_frames(self, ev_parts: list[dict], sess_rows: list[dict]) -> HistoryDump:
        w, cfg = self.world, self.cfg
        ev = {k: np.concatenate([e[k] for e in ev_parts]) for k in ev_parts[0]}
        ev = {k: v[(ev["t"] < cfg.stream_start)] for k, v in ev.items()}
        order = np.argsort(ev["t"], kind="stable")
        ev = {k: v[order] for k, v in ev.items()}
        pid, uid = np.asarray(w.pid, dtype=object), np.asarray(w.uid, dtype=object)
        P = w.products
        events = pd.DataFrame({
            "ts": iso(ev["t"]), "event_type": EVENT_TYPES[ev["type"]], "user_id": uid[ev["user"]],
            "session_id": ev["sess"], "product_id": pid[ev["item"]], "price_eur": P.price0[ev["item"]],
            "device": np.asarray(DEVICES)[ev["device"]], "region": np.asarray(w.region_names)[ev["region"]]})
        rows = []
        k = 0
        for part in sess_rows:
            s = part["s"]
            for j in range(len(s.user)):
                vmask = s.views[j] >= 0
                vts = iso(part["vt"][j][vmask]) if vmask.any() else []
                shown = [w.pid[x] for x in part["shown"][j]]
                slot = int(part["slot"][j])
                clicked = shown[slot] if slot >= 0 else None
                purchased = clicked if part["bought"][j] else None
                rows.append({
                    "request_id": part["rid"][j], "t": s.t[j], "user_id": w.uid[s.user[j]],
                    "context": {"device": DEVICES[s.device[j]], "region": w.region_names[s.region[j]],
                                "page": PAGES[s.page[j]]},
                    "recent_views": [{"product_id": w.pid[v], "ts": str(t)} for v, t in zip(s.views[j][vmask], vts)],
                    "shown": shown, "propensity": [float(x) for x in part["prop"][j]],
                    "clicked": clicked, "purchased": purchased,
                    "revenue_eur": float(P.price0[part["shown"][j][slot]]) if purchased else 0.0})
                self._hist_index[part["rid"][j]] = k
                k += 1
        sessions = pd.DataFrame(rows)
        sessions = sessions[sessions.t < cfg.stream_start]
        sessions.insert(1, "ts", iso(sessions.pop("t").to_numpy()))
        sessions = sessions.sort_values("ts", kind="stable").reset_index(drop=True)
        return HistoryDump(catalog=self.catalog_frame(), history_sessions=sessions, history_events=events)

    def catalog_frame(self) -> pd.DataFrame:
        """shop.catalog as published at the start of the stream (initial price and is_active)."""
        w = self.world
        n = len(w.hist_active)
        rows = []
        for i in range(n):
            r = w.product_row(i)
            r["price_eur"] = float(w.products.price0[i])
            r["is_active"] = bool(w.hist_active[i])
            rows.append(r)
        return pd.DataFrame(rows)

    def catalog(self) -> list[dict]:
        return self.catalog_frame().to_dict("records")

    def history(self, days: int = 60) -> HistoryDump:
        """The last `days` days (<= cfg.history_days) of the synthetic history."""
        self._ensure_history()
        if days > self.cfg.history_days:
            raise ValueError(f"history has {self.cfg.history_days} days; pass SimConfig(history_days=...)")
        h = self._history
        if days == self.cfg.history_days:
            return h
        cut = iso(self.cfg.stream_start - days * DAY)
        return HistoryDump(h.catalog, h.history_sessions[h.history_sessions.ts >= cut].reset_index(drop=True),
                           h.history_events[h.history_events.ts >= cut].reset_index(drop=True))

    # ================================================================== live stream
    def _push(self, ts: float, kind: str, payload: Any) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (ts, self._seq, kind, payload))

    def _pick_users(self, n: int, rng: np.random.Generator, new_share: float, live_share: float):
        """User indices and is_new flags for n live sessions (may create new shoppers)."""
        w = self.world
        pu = self._pu
        r = rng.random(n)
        out, new = np.empty(n, dtype=np.int64), np.zeros(n, bool)
        for j in range(n):
            if r[j] < new_share:
                out[j] = w.add_user(w.sample_user(rng))
                self._live_users.append(int(out[j]))
                new[j] = True
            elif r[j] < new_share + live_share and self._live_users:
                out[j] = self._live_users[rng.integers(0, len(self._live_users))]
                new[j] = True        # not in history: still a "new shopper" for the organisers' cold segment
            else:
                out[j] = rng.choice(len(pu), p=pu)
        return out, new

    def _gen_block(self, k: int) -> None:
        cfg, w, m = self.cfg, self.world, self.model
        rng = np.random.default_rng([cfg.seed, 1, k + 10_000_000])
        t0 = cfg.stream_start + k * cfg.block_s
        t1 = t0 + cfg.block_s
        # ---- carousel requests (born now, arrive after a few views)
        n = rng.poisson(cfg.requests_per_s * cfg.block_s)
        if n:
            start = np.sort(rng.uniform(t0, t1, n))
            user, is_new = self._pick_users(n, rng, cfg.new_user_share, cfg.returning_live_share)
            nv = np.where(rng.random(n) < cfg.no_view_share, 0, np.minimum(1 + rng.poisson(2.0, n), MAX_VIEWS))
            s, vt = self._sessions(user, start, nv, rng)
            s.page = pick_page(nv, rng)
            s.t = np.where(nv > 0, _last(vt, start) + 0.5 + rng.exponential(8, n), start)
            sid = self._ids("s", n, rng, 8)
            rid = self._ids("r", n, rng, 12)
            for j in range(n):
                views = []
                for v, t in zip(s.views[j], vt[j]):
                    if v < 0:
                        break
                    self._push(t, "event", (t, 0, int(user[j]), sid[j], int(v), int(s.device[j]), int(s.region[j])))
                    views.append((int(v), float(t)))
                self._push(s.t[j], "request", (rid[j], bool(is_new[j]), s.row(j), views))
        # ---- organic browsing
        n = rng.poisson(cfg.organic_sessions_per_s * cfg.block_s)
        if n:
            start = np.sort(rng.uniform(t0, t1, n))
            user, _ = self._pick_users(n, rng, 0.10, 0.10)
            nv = np.minimum(1 + rng.poisson(3.5, n), MAX_VIEWS)
            s, vt = self._sessions(user, start, nv, rng)
            s.page[:] = 1
            cart, buy = organic_actions(m, s, rng)
            ev = self._events_frame(s, vt, cart, buy, self._ids("s", n, rng, 8), rng)
            for j in range(len(ev["t"])):
                self._push(float(ev["t"][j]), "event", (float(ev["t"][j]), int(ev["type"][j]), int(ev["user"][j]),
                                                        ev["sess"][j], int(ev["item"][j]), int(ev["device"][j]),
                                                        int(ev["region"][j])))
        if k < 0:
            return
        # ---- catalogue changes
        P = w.products
        for _ in range(rng.poisson(cfg.new_products_per_hour * cfg.block_s / 3600)):
            ts = float(rng.uniform(t0, t1))
            i = w.add_product(w.sample_product(rng, created=ts))
            self._push(ts, "new_product", i)
        active = np.nonzero(P.active[: P.n])[0]
        for _ in range(rng.poisson(cfg.price_changes_per_hour * cfg.block_s / 3600)):
            i = int(rng.choice(active))
            f = np.exp(rng.normal(-0.2, 0.1) if rng.random() < 0.65 else rng.normal(0.12, 0.05))
            self._push(float(rng.uniform(t0, t1)), "price_change", (i, f))
        pq = np.exp(P.q[active])
        for _ in range(rng.poisson(cfg.out_of_stock_per_hour * cfg.block_s / 3600)):
            i = int(rng.choice(active, p=pq / pq.sum()))
            self._push(float(rng.uniform(t0, t1)), "out_of_stock", (i, float(rng.exponential(cfg.back_in_stock_mean_s))))

    def _apply(self, ts: float, kind: str, payload: Any, tick: Tick) -> None:
        w, m, P = self.world, self.model, self.world.products
        if ts < self.cfg.stream_start:
            return
        if kind == "event":
            t, typ, u, sid, i, dev, reg = payload
            if typ == 2:
                m.record_purchase(u, i, t)
            tick.events.append(Event(ts=iso(t), event_type=str(EVENT_TYPES[typ]), user_id=w.uid[u], session_id=sid,
                                     product_id=w.pid[i], price_eur=float(P.price[i]), device=DEVICES[dev],
                                     region=w.region_names[reg]))
        elif kind == "request":
            rid, is_new, s, views = payload
            req = Request(request_id=rid, ts=iso(ts), user_id=w.uid[int(s.user[0])], is_new_user=is_new,
                          context={"device": DEVICES[int(s.device[0])], "region": w.region_names[int(s.region[0])],
                                   "page": PAGES[int(s.page[0])]},
                          recent_views=[{"product_id": w.pid[v], "ts": iso(t)} for v, t in views])
            self._live[rid] = (req, s, ts)
            tick.requests.append(req)
        else:
            if kind == "new_product":
                i = payload
                P.active[i] = True
                self.new_products.add(i)
            elif kind == "price_change":
                i, f = payload
                P.price[i] = round(float(np.clip(P.price[i] * f, 0.4 * P.price0[i], 1.6 * P.price0[i])), 2)
            elif kind == "out_of_stock":
                i, delay = payload
                if not P.active[i]:
                    return
                P.active[i] = False
                self._push(ts + delay, "back_in_stock", i)
            elif kind == "back_in_stock":
                i = payload
                if P.active[i]:
                    return
                P.active[i] = True
            row = w.product_row(i)
            tick.catalog_updates.append(CatalogUpdate(**row, ts=iso(ts), change=kind))

    def advance(self, now: float) -> Tick:
        """Everything that happened in (previous now, now]."""
        tick = Tick()
        if now <= self.now and self._next_block >= 0:
            return tick
        if self._fallback_order is None:
            self._start_stream()
        bs = self.cfg.block_s
        while self.cfg.stream_start + self._next_block * bs < now:
            bstart = self.cfg.stream_start + self._next_block * bs
            self._drain(bstart, tick, inclusive=False)
            self._gen_block(self._next_block)
            self._next_block += 1
        self._drain(now, tick, inclusive=True)
        self.now = max(self.now, now)
        return tick

    def _drain(self, until: float, tick: Tick, inclusive: bool) -> None:
        h = self._heap
        while h and (h[0][0] <= until if inclusive else h[0][0] < until):
            ts, _, kind, payload = heapq.heappop(h)
            self._apply(ts, kind, payload, tick)

    def _start_stream(self) -> None:
        """Freeze the organisers' baseline: history best sellers (organic purchases), ranked once."""
        self._ensure_history()
        self._pu = self._hist_user_sampler()
        ev = self._history.history_events
        counts = ev[ev.event_type == "purchase"].groupby("product_id").size()
        n = len(self.world.hist_active)
        pids = [self.world.pid[i] for i in range(n)]
        cnt = np.array([counts.get(p, 0) for p in pids])
        order = sorted(range(n), key=lambda i: (cnt[i], pids[i]), reverse=True)
        self._fallback_order = order

    # ================================================================== judging
    def _session_of(self, req: Request) -> tuple[Sessions, bool]:
        rid = req["request_id"]
        if rid in self._live:
            return self._live[rid][1], True
        self._ensure_history()
        if rid in self._hist_index:
            return self._hist_sessions.row(self._hist_index[rid]), False
        raise KeyError(f"unknown request_id {rid}")

    def _idx(self, items: list[str]) -> np.ndarray:
        return np.array([self.world.pid_index.get(p, -1) for p in items][:10] or [-1], dtype=np.int64)

    def available_now(self) -> np.ndarray:
        return self.world.available(self.now)

    def baseline_items(self) -> list[int]:
        if self._fallback_order is None:
            self._start_stream()
        av = self.available_now()
        out = []
        for i in self._fallback_order:
            if av[i]:
                out.append(i)
                if len(out) == 10:
                    break
        return out

    def is_valid(self, items: list[str]) -> bool:
        idx = [self.world.pid_index.get(p, -1) for p in items]
        if len(idx) != 10 or len(set(idx)) != 10 or min(idx) < 0:
            return False
        return bool(self.available_now()[idx].all())

    def expected_purchase(self, req: Request, items: list[str]) -> float:
        """Hidden P(purchase | these items shown in this order), with the shop as it is now."""
        s, _ = self._session_of(req)
        return float(self.model.expected(s, self._idx(items)[None, :])[0])

    def oracle(self, req: Request) -> list[str]:
        s, live = self._session_of(req)
        avail = self.available_now() if live else self.world.available(float(s.t[0]))
        return [self.world.pid[i] for i in self.model.best(s, avail)[0]]

    def is_cold(self, req: Request) -> bool:
        return bool(req.get("is_new_user")) or any(
            self.world.pid_index.get(v["product_id"], -1) in self.new_products for v in req.get("recent_views") or [])

    def judge(self, sub: Submission, received_at: float) -> Feedback:
        """Score one submission like the organisers: late / invalid -> baseline is shown instead.
        Only the first submission per request counts (later ones return the same feedback)."""
        rid = sub["request_id"]
        if rid in self._feedback:
            return self._feedback[rid]
        req, s, req_ts = self._live[rid]
        latency = received_at - req_ts
        items = list(sub.get("items") or [])
        reason = "late" if latency > self.cfg.deadline_s else ("" if self.is_valid(items) else "invalid")
        return self._serve(rid, req, s, req_ts, items, reason, latency)

    def judge_missing(self, request_id: str) -> Feedback:
        """Feedback for a request nobody answered (FakeStore calls this after the deadline)."""
        if request_id in self._feedback:
            return self._feedback[request_id]
        req, s, req_ts = self._live[request_id]
        return self._serve(request_id, req, s, req_ts, [], "missing", None)

    def _serve(self, rid, req, s, req_ts, items, reason, latency) -> Feedback:
        w, m = self.world, self.model
        base = self.baseline_items()
        shown = self._idx(items) if not reason else np.array(base, dtype=np.int64)
        c, b = m.scores(s, shown[None, :])
        pk = (c * b * m.examine[: shown.size])[0]
        exp_team = float(pk.sum())
        exp_base = exp_team if reason else float(m.expected(s, np.array([base]))[0])
        exp_oracle = float(m.expected(s, m.best(s, self.available_now()))[0])
        rng = np.random.default_rng([self.cfg.seed, 3, zlib.crc32(rid.encode())])
        pclick = c[0] * m.examine[: shown.size]
        r = rng.random()
        cum = np.cumsum(pclick)
        clicked = purchased = ""
        revenue = 0.0
        if r < cum[-1]:
            k = int((r >= cum).sum())
            clicked = w.pid[shown[k]]
            if rng.random() < b[0, k]:
                purchased = clicked
                revenue = float(w.products.price[shown[k]])
                m.record_purchase(int(s.user[0]), int(shown[k]), req_ts + self.cfg.deadline_s)
        fb = Feedback(request_id=rid, ts=iso(req_ts + self.cfg.deadline_s + rng.uniform(*self.cfg.feedback_delay_s)),
                      shown=[w.pid[i] for i in shown], served_by="fallback" if reason else "team",
                      fallback_reason=reason, latency_ms=0.0 if latency is None else round(latency * 1000, 1),
                      clicked=clicked, purchased=purchased, revenue_eur=revenue)
        self._feedback[rid] = fb
        self._stats[rid] = dict(team=exp_team, base=exp_base, oracle=exp_oracle, cold=self.is_cold(req),
                                ok=not reason, revenue=float((pk * w.products.price[shown]).sum()))
        return fb

    def report(self) -> dict:
        """The organisers' automatic score over all judged requests (call judge_missing first for the
        unanswered ones). Purchases are expected purchases per 100 requests."""
        if not self._stats:
            return {"n": 0}
        df = pd.DataFrame(self._stats.values())

        def score(d):
            t, b, o = (float(100 * d[k].mean()) for k in ("team", "base", "oracle"))
            return dict(n=len(d), team=t, baseline=b, oracle=o,
                        score=float(np.clip(100 * (t - b) / max(o - b, 1e-9), 0, 100)) if len(d) else float("nan"))

        cold = df[df.cold]
        return {**score(df), "cold": score(cold) if len(cold) else {"n": 0},
                "reliability": float(df.ok.mean()), "revenue_per_request": float(df.revenue.mean())}


def make_simulator(seed: int = 7, config: SimConfig | None = None, **overrides) -> ShopSimulator:
    """Entry point used by FakeStore, tests and notebooks."""
    cfg = (config or SimConfig()).with_(seed=seed, **overrides)
    return ShopSimulator(cfg)
