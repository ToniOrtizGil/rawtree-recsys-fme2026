"""Collector: keeps a bounded, deduplicated in-memory picture of the live tables, read through any Store.

Why a separate reader instead of asking the serving process: if the serving machine dies, the dashboard
must still show it (feedback keeps arriving, all `missing`). So it reads the organisers' tables directly.

Reads are incremental (cursor = newest ts seen; stores return rows from cursor - overlap; we dedupe by key)
and cheap: requests/feedback are ~3 rows/s; organic events (~50/s) are folded into per-minute counters
instead of being kept. Our own tables, read back:
- model_version of each answer: store.submissions_since (protocol), else store.submissions_for(ids), else SQL;
- heartbeat rows: store.scratch_since(cursor, kind="heartbeat") (protocol), else SQL.
Optional readers are always called by keyword (implementations disagree on argument order).
"""
from __future__ import annotations

import threading
import time
from collections import Counter, OrderedDict, deque

from . import explain, metrics, queries
from .metrics import FB, to_epoch

MAX_PAGES = 20


class Collector:
    def __init__(self, store, lookback_s: float = 3 * 3600, events_lookback_s: float = 20 * 60,
                 sql_every_s: float = 20.0):
        self.store = store
        self.team = getattr(store, "team", "team_NN")
        self.kind = getattr(store, "kind", type(store).__name__)
        self.lookback_s, self.sql_every_s = lookback_s, sql_every_s
        now = self.now()
        self.cur = {"requests": now - lookback_s, "feedback": now - lookback_s,
                    "updates": now - lookback_s, "events": now - events_lookback_s, "scratch": now - 120,
                    "submissions": now - lookback_s}
        self.versions: OrderedDict[str, str] = OrderedDict()     # request_id -> model_version
        self.started = now
        self.lock = threading.RLock()
        self.requests: OrderedDict[str, tuple] = OrderedDict()   # rid -> (ts, is_new, views, context, user)
        self.feedback: OrderedDict[str, FB] = OrderedDict()
        self._unresolved: deque[str] = deque()
        self.catalog: dict[str, dict] = {}
        self.new_products: set[str] = set()
        self._seen_updates: set[tuple] = set()
        self._seen_events: dict[tuple, float] = {}
        self.views_min: dict[int, Counter] = {}       # minute -> subcategory view counts (organic events)
        self.buys_min: dict[int, Counter] = {}        # minute -> product purchase counts (organic events)
        self.last = {"request": 0.0, "feedback": 0.0, "event": 0.0, "heartbeat": 0.0}
        self.heartbeat: dict | None = None
        self.errors: dict[str, str] = {}
        self._sql_ok: bool | None = None
        self.sql_results: dict[str, dict] = {}
        self._sql_ran = 0.0
        self.polls = 0                       # 0 = first read still running (10 s on live RawTree: 3 h lookback)

    # ------------------------------------------------------------------ helpers
    def now(self) -> float:
        """The store's clock (simulated in tests and in the FakeStore), else wall time."""
        f = getattr(self.store, "now", None) or getattr(getattr(self.store, "clock", None), "now", None)
        return float(f()) if callable(f) else time.time()

    def _read(self, name: str, fn, key: str, limit: int) -> list[dict]:
        """Page through one incremental reader. Errors are recorded, never raised (the page shows them)."""
        out: list[dict] = []
        try:
            for _ in range(MAX_PAGES):
                rows = fn(self.cur[key], limit)
                out.extend(rows)
                if rows:
                    self.cur[key] = max(self.cur[key], max(to_epoch(r.get("ts")) for r in rows))
                if len(rows) < limit:
                    break
            self.errors.pop(name, None)
        except Exception as e:  # noqa: BLE001 - a dashboard must survive a flaky store
            self.errors[name] = f"{type(e).__name__}: {e}"[:300]
        return out

    def sql_capable(self) -> bool:
        if self._sql_ok is None:
            try:
                self.store.query("SELECT 1 AS ok")
                self._sql_ok = True
            except (NotImplementedError, AttributeError):
                self._sql_ok = False
            except Exception as e:  # noqa: BLE001
                self.errors["sql"] = f"{type(e).__name__}: {e}"[:300]
                return False                       # try again next poll
        return self._sql_ok

    # ------------------------------------------------------------------ polling
    def poll(self) -> None:
        with self.lock:
            if not self.catalog:
                try:
                    self.catalog = {str(p["product_id"]): p for p in self.store.catalog()}
                    self.errors.pop("catalog", None)
                except Exception as e:  # noqa: BLE001
                    self.errors["catalog"] = f"{type(e).__name__}: {e}"[:300]
            self._poll_updates()
            self._poll_requests()
            self._poll_feedback()
            self._poll_events()
            self._poll_submissions()
            self._resolve_versions()
            self._poll_heartbeat()
            self._prune()
            self.polls += 1
        if self.sql_every_s and self.now() - self._sql_ran >= self.sql_every_s and self.sql_capable():
            self.run_sql()

    def _poll_updates(self) -> None:
        for u in self._read("catalog_updates", self.store.catalog_updates_since, "updates", 50_000):
            k = (u.get("product_id"), u.get("ts"), u.get("change"))
            if k in self._seen_updates:
                continue
            self._seen_updates.add(k)
            pid = str(u.get("product_id"))
            self.catalog[pid] = {**self.catalog.get(pid, {}), **u}
            if u.get("change") == "new_product":
                self.new_products.add(pid)

    def _poll_requests(self) -> None:
        for r in self._read("requests", self.store.requests_since, "requests", 500):
            rid = str(r.get("request_id"))
            if rid in self.requests:
                continue
            ts = to_epoch(r.get("ts"))
            views = tuple(str(v.get("product_id")) for v in (r.get("recent_views") or []))
            self.requests[rid] = (ts, bool(r.get("is_new_user")), views, r.get("context") or {},
                                  str(r.get("user_id", "")))
            self.last["request"] = max(self.last["request"], ts)

    def _cold(self, rid: str) -> bool | None:
        req = self.requests.get(rid)
        if req is None:
            return None
        return req[1] or any(v in self.new_products for v in req[2])

    def _poll_feedback(self) -> None:
        for row in self._read("feedback", self.store.feedback_since, "feedback", 50_000):
            rid = str(row.get("request_id"))
            if rid == "__init__" or rid in self.feedback:
                continue
            fb = FB.from_row(row)
            fb.cold = self._cold(rid)
            self.feedback[rid] = fb
            if fb.ok:
                self._unresolved.append(rid)
            self.last["feedback"] = max(self.last["feedback"], fb.ts)
        for fb in self.feedback.values():          # requests may arrive after their feedback was read
            if fb.cold is None and fb.request_id in self.requests:
                fb.cold = self._cold(fb.request_id)

    def _poll_events(self) -> None:
        horizon = self.cur["events"] - 10
        for e in self._read("events", self.store.events_since, "events", 50_000):
            ts = to_epoch(e.get("ts"))
            k = (e.get("ts"), e.get("user_id"), e.get("product_id"), e.get("event_type"), e.get("session_id"))
            if k in self._seen_events:
                continue
            self._seen_events[k] = ts
            self.last["event"] = max(self.last["event"], ts)
            m = int(ts // 60)
            pid = str(e.get("product_id"))
            if e.get("event_type") == "view":
                sub = (self.catalog.get(pid) or {}).get("subcategory", "?")
                self.views_min.setdefault(m, Counter())[sub] += 1
            elif e.get("event_type") == "purchase":
                self.buys_min.setdefault(m, Counter())[pid] += 1
        self._seen_events = {k: t for k, t in self._seen_events.items() if t >= horizon}

    def _poll_submissions(self) -> None:
        """Submissions carry no ts: the store filters on insert time; we re-read the last minute and dedupe."""
        if not hasattr(self.store, "submissions_since"):
            return
        try:
            rows = self.store.submissions_since(self.cur["submissions"], limit=50_000)
            self.errors.pop("submissions", None)
        except Exception as e:  # noqa: BLE001
            self.errors["submissions"] = f"{type(e).__name__}: {e}"[:300]
            return
        for r in rows:
            self.versions[str(r.get("request_id"))] = str(r.get("model_version") or "")
        self.cur["submissions"] = self.now() - 60
        while len(self.versions) > 100_000:
            self.versions.popitem(last=False)

    def _resolve_versions(self, max_batches: int = 10) -> None:
        """Attach model_version to answers we served (needed for the A/B table). Batches of 400 ids."""
        retry: list[str] = []
        now = self.now()
        for _ in range(max_batches):
            if not self._unresolved:
                break
            batch = [self._unresolved.popleft() for _ in range(min(400, len(self._unresolved)))]
            try:
                if hasattr(self.store, "submissions_since"):
                    found = {rid: self.versions[rid] for rid in batch if rid in self.versions}
                elif hasattr(self.store, "submissions_for"):
                    found = self.store.submissions_for(batch)
                elif self.sql_capable():
                    rows = self.store.query(queries.versions_for(self.team, batch))
                    found = {str(r["request_id"]): str(r["model_version"]) for r in rows}
                else:                               # this store cannot tell us: do not keep retrying
                    for rid in batch:
                        if rid in self.feedback:
                            self.feedback[rid].version = "(version not readable)"
                    continue
                self.errors.pop("versions", None)
            except Exception as e:  # noqa: BLE001
                self.errors["versions"] = f"{type(e).__name__}: {e}"[:300]
                retry.extend(batch)
                break
            for rid in batch:
                fb = self.feedback.get(rid)
                if fb is None:
                    continue
                if rid in found:
                    fb.version = found[rid]
                elif now - fb.ts < 120:             # submission row may not be visible yet: retry
                    retry.append(rid)
                else:
                    fb.version = "(no submission row)"
        self._unresolved.extend(retry)

    def _poll_heartbeat(self) -> None:
        rows: list[dict] = []
        if hasattr(self.store, "scratch_since"):
            rows = self._read("scratch", lambda c, n: self.store.scratch_since(c, kind="heartbeat", limit=n),
                              "scratch", 10_000)
        elif self.sql_capable():
            try:
                rows = self.store.query(queries.last_heartbeat(self.team))
                self.errors.pop("scratch", None)
            except Exception as e:  # noqa: BLE001
                self.errors["scratch"] = f"{type(e).__name__}: {e}"[:300]
        for r in rows:
            if (r.get("kind") or r.get("type")) == "heartbeat":
                ts = to_epoch(r.get("ts_epoch") or r.get("ts"))
                if ts >= self.last["heartbeat"]:
                    self.last["heartbeat"], self.heartbeat = ts, r

    def _prune(self) -> None:
        lo = self.now() - self.lookback_s
        for d in (self.requests,):
            while d and next(iter(d.values()))[0] < lo:
                d.popitem(last=False)
        while self.feedback and next(iter(self.feedback.values())).ts < lo:
            self.feedback.popitem(last=False)
        for d in (self.views_min, self.buys_min):
            for m in [m for m in d if m * 60 < lo]:
                del d[m]
        if len(self._seen_updates) > 200_000:
            self._seen_updates.clear()

    def run_sql(self) -> None:
        """Run the SQL twin of each panel in RawTree (only when the store can run SQL)."""
        self._sql_ran = self.now()
        for name, (title, sql) in queries.panels(self.team).items():
            t0 = time.perf_counter()
            try:
                rows = self.store.query(sql)
                self.sql_results[name] = {"rows": rows[:20], "ms": round(1000 * (time.perf_counter() - t0)),
                                          "ran_at": self._sql_ran, "error": None}
            except Exception as e:  # noqa: BLE001
                self.sql_results[name] = {"rows": [], "ms": None, "ran_at": self._sql_ran,
                                          "error": f"{type(e).__name__}: {e}"[:300]}

    # ------------------------------------------------------------------ derived views
    def trends(self, now: float, recent_min: int = 5, base_min: int = 15, top: int = 8) -> list[dict]:
        """Organic views per subcategory: last `recent_min` minutes vs the `base_min` before (per 5 min)."""
        m_now = int(now // 60)
        recent, base = Counter(), Counter()
        for m, c in self.views_min.items():
            age = m_now - m
            if 0 <= age < recent_min:
                recent.update(c)
            elif recent_min <= age < recent_min + base_min:
                base.update(c)
        scale = recent_min / base_min
        out = [{"subcategory": s, "views_recent": v, "views_base": round(base[s] * scale, 1),
                "lift": round(v / max(base[s] * scale, 1.0), 2)} for s, v in recent.items() if v >= 5]
        out.sort(key=lambda d: -d["lift"])
        return out[:top]

    def popular(self, now: float, minutes: int = 15, top: int = 50) -> set[str]:
        c = Counter()
        for m, cnt in self.buys_min.items():
            if now // 60 - m < minutes:
                c.update(cnt)
        return {p for p, _ in c.most_common(top)}

    def feed(self, n: int, now: float, trending: set[str]) -> list[dict]:
        pop = self.popular(now)
        out = []
        for fb in list(self.feedback.values())[-n:][::-1]:
            req = self.requests.get(fb.request_id)
            views = req[2] if req else ()
            out.append({
                "request_id": fb.request_id, "ts": fb.ts, "user_id": req[4] if req else None,
                "is_new_user": req[1] if req else None, "cold": fb.cold, "context": req[3] if req else {},
                "views": [{"product_id": v, "subcategory": (self.catalog.get(v) or {}).get("subcategory")}
                          for v in views],
                "shown": [{"product_id": p, "subcategory": (self.catalog.get(p) or {}).get("subcategory"),
                           "price_eur": (self.catalog.get(p) or {}).get("price_eur"),
                           "tags": explain.tags(p, views, self.catalog, self.new_products, trending, pop)}
                          for p in fb.shown],
                "clicked": fb.clicked, "purchased": fb.purchased, "served_by": fb.served_by,
                "reason": fb.reason, "latency_ms": fb.latency_ms, "model_version": fb.version})
        return out

    def snapshot(self, window_s: float = 3600, feed_n: int = 12) -> dict:
        """Everything the page shows, as one JSON-able dict (alerts are added by dashboard.alerts)."""
        with self.lock:
            now = self.now()
            rows = list(self.feedback.values())
            w5 = metrics.window(rows, now, metrics.LIGHT_WINDOW_S)
            ww = metrics.window(rows, now, window_s)
            trends = self.trends(now)
            hot = {t["subcategory"] for t in trends[:3] if t["lift"] >= 1.3}
            recent_req = sum(1 for v in reversed(self.requests.values()) if v[0] > now - 60)
            return {
                "now": now, "store": self.kind, "team": self.team, "window_s": window_s, "polls": self.polls,
                "uptime_s": now - self.started,
                "light": {"status": metrics.light(w5), **metrics.reliability(w5)},
                "latency_5m": metrics.latency(w5),
                "reliability_window": metrics.reliability(ww),
                "conversion": metrics.conversion([r for r in ww if r.ok]),
                "conversion_all": metrics.conversion(ww),
                "versions": metrics.by_version(ww),
                "segments": metrics.segments([r for r in ww if r.ok]),
                "series": metrics.per_minute(rows, now, 60),
                "trends": trends,
                "requests_per_s": recent_req / 60,
                "ages": {k: (now - v if v else None) for k, v in self.last.items()},
                "heartbeat": self.heartbeat,
                "new_products": len(self.new_products),
                "feed": self.feed(feed_n, now, hot),
                "errors": dict(self.errors),
                "sql_live": bool(self._sql_ok),
                "sql": [{"name": k, "title": t, "sql": s, **self.sql_results.get(k, {})}
                        for k, (t, s) in queries.panels(self.team).items()],
            }
