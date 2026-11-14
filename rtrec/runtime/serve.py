"""The serving loop: every request answered, on time, with exactly 10 valid products.

    python -m rtrec.runtime.serve --store fake --duration 60          # local, simulated shop
    RTREC_STORE=rawtree TEAM_ID=team_NN RAWTREE_API_KEY=... python -m rtrec.runtime.serve

Threads (real-time mode, `run()`):

    requests poller ──▶ req queue ──┐
    catalog poller  ──▶ upd queue ──┤      serving thread (the only one touching State):
    events poller   ──▶ upd queue ──┼──▶   apply catalogue -> policy (budget, 3 levels) -> finalize
    feedback poller ──▶ upd queue ──┘      -> writer queue; between batches: hooks, tick, status
    writer          ◀── writer queue       single writer: batched inserts, safe retries
    heartbeat       ── team.scratch        primary beats; standby decides when to write

Every I/O call is on its own thread, so a slow query never delays the answer to an unrelated
request. `step()` runs the same pipeline once, synchronously, for deterministic tests on a SimClock.

Latency budget (target p99 < 1.8 s from ts, hard limit 3 s): visible ~0.3-0.6 s after ts, poll
<= 0.2 s, compute < 50 ms per batch, insert ~0.2 s, organisers' poll <= 0.5 s.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import pickle
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rtrec.contract import Policy, State
from rtrec.schemas import Submission
from rtrec.validate import finalize

from .catalog import CatalogTracker
from .clock import RealClock, parse_ts, to_iso
from .cursor import Cursor, event_key, request_key, update_key
from .heartbeat import Heartbeat, default_instance
from .hooks import HOOK_NAMES, HookSet
from .journal import Journal
from .metrics import Metrics
from .policies import RecentViewsPolicy
from .runner import PolicyRunner
from .writer import Writer

log = logging.getLogger("rtrec.serve")


@dataclass
class ServeConfig:
    poll_interval: float = 0.2          # shop.requests polling (organisers' baseline: 0.3 s)
    request_pollers: int = 2            # staggered pollers: one stalled HTTP call never delays a request
    catalog_interval: float = 0.25      # stock must be exact: one stock-out invalidates the whole answer
    events_interval: float = 1.0
    feedback_interval: float = 2.0
    heartbeat_interval: float = 0.5
    status_interval: float = 10.0
    snapshot_interval: float = 60.0
    tick_interval: float = 1.0
    batch_budget_s: float = 0.15        # level-1 policy time per batch
    stale_s: float = 6.0                # skip requests already this old when first seen (our clock)
    give_up_s: float = 4.0              # writer drops rows this old (cannot be on time any more)
    insert_hedge_s: float = 0.7         # send an identical copy if an insert is slower (live RTT ~0.3 s)
    request_lookback_s: float = 10.0    # on a cold start, look this far back for requests
    events_lookback_s: float = 600.0    # on a cold start, replay this much of shop.events to the hooks
    hook_chunk: int = 2000
    role: str = "primary"
    instance: str = field(default_factory=default_instance)
    takeover_after_s: float = 2.5
    state_dir: str | None = "artifacts/runtime"
    page_limit_requests: int = 500
    page_limit_rows: int = 50_000


class Server:
    def __init__(self, store, policy: Policy | None = None, *, policy_factory=None, fallback_policy=None,
                 hooks: list | None = None, clock=None, config: ServeConfig | None = None,
                 state: State | None = None):
        self.store = store
        self.cfg = config or ServeConfig()
        # FakeStore carries its own (simulated or offset) clock: the loop must live on the same one
        self.clock = clock or getattr(store, "clock", None) or RealClock()
        self.state = state or State()
        # hooks=None -> A4's live learning (LiveHook) by default; hooks=[] -> none
        self.hooks = HookSet(default_hooks() if hooks is None else hooks)
        self.metrics = Metrics()
        self.runner = PolicyRunner(policy, budget_s=self.cfg.batch_budget_s)
        self.policy_factory = policy_factory
        self.fallback = fallback_policy or RecentViewsPolicy()
        self.tracker = CatalogTracker()
        sd = Path(self.cfg.state_dir) if self.cfg.state_dir else None
        self.state_dir = sd
        self.journal = Journal(sd)
        self.writer = Writer(store, self.clock, self.journal, self.metrics, give_up_s=self.cfg.give_up_s,
                             hedge_after_s=self.cfg.insert_hedge_s)
        self.heartbeat = Heartbeat(store, self.clock, role=self.cfg.role, instance=self.cfg.instance,
                                   takeover_after_s=self.cfg.takeover_after_s,
                                   model_version=lambda: self.runner.name)
        now = self.clock.now()
        self.cur_requests = Cursor(request_key, start=now - self.cfg.request_lookback_s, window=60)
        self.cur_updates = Cursor(update_key, start=0.0, window=120)
        self.cur_events = Cursor(event_key, start=now - self.cfg.events_lookback_s, window=60)
        self.cur_feedback = Cursor(request_key, start=now - 60, window=600)
        self._policy_ready: queue.Queue = queue.Queue()     # (policy, scratch State) from the loader thread
        self._shadow: list[tuple[Submission, float]] = []   # standby: answers computed but not written
        self._req_q: queue.Queue = queue.Queue()
        self._upd_q: queue.Queue = queue.Queue()            # ("catalog"|"events"|"feedback", rows)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._due: dict[str, float] = {}
        self.started = False

    # ================================================================== bootstrap
    def start(self) -> None:
        """Load everything needed to answer validly. Cheap: a few store calls."""
        t0 = time.perf_counter()
        restored = self._restore_snapshot()
        # without a snapshot we cannot answer validly until the catalogue loads: retry until it does;
        # with one, a few tries then carry on (the catalogue poller catches up in the background)
        tries = 3 if restored else None
        if self._retry(lambda: self.tracker.load(self.store.catalog()), "catalog", tries) is None and not restored:
            raise RuntimeError("catalog never loaded")
        self._retry(self._backfill_catalog_updates, "catalog_updates", tries)
        self.tracker.publish(self.state)
        if not self.state.safe_list:
            self.state.safe_list = self._load_safe_list()
        # dedupe across restarts: requests answered by a previous run are skipped
        newest = self.journal.newest_ts()
        for rid, t in self.journal.answered.items():
            self.cur_requests.mark(rid, t)
        if newest:
            self.cur_requests.position = max(self.cur_requests.position, newest - 1.0)
        if self.journal.unacked:
            resend = list(self.journal.unacked.values())
            log.warning("resending %d answers decided but not confirmed before the restart", len(resend))
            self.writer.enqueue(resend)
        self.hooks.call("on_start", self.state)
        if self.runner.policy is None and self.policy_factory is not None:
            threading.Thread(target=self._load_policy, name="policy-loader", daemon=True).start()
        self.started = True
        log.info("ready in %.2f s: %d products, %d available, %d safe, role=%s, policy=%s",
                 time.perf_counter() - t0, len(self.state.catalog), len(self.state.available),
                 len(self.state.safe_list), self.cfg.role, self.runner.name)

    def _retry(self, fn, what: str, tries: int | None):
        """fn() until it succeeds (tries=None: forever). Returns True on success, None if given up."""
        n, delay = 0, 0.2
        while True:
            try:
                fn()
                return True
            except Exception as e:  # noqa: BLE001
                n += 1
                self.metrics.inc(f"start_errors_{what}")
                log.warning("start: %s failed (%d): %s", what, n, e)
                if tries is not None and n >= tries:
                    return None
                self.clock.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _load_policy(self) -> None:
        """Background thread. A3's factories take (state, dump) and fill state.offline (artefacts from the
        history) and warm profiles: that can take a while, so it runs on a scratch State and the serving
        thread merges the result (_adopt_policy) -- the shared State keeps a single writer."""
        t0 = time.perf_counter()
        try:
            st = self.state
            scratch = State(catalog=dict(st.catalog), available=st.available, new_products=st.new_products,
                            safe_list=list(st.safe_list))
            if _wants_args(self.policy_factory):
                p = self.policy_factory(scratch, self._history_dump())
            else:
                p = self.policy_factory()
            self._policy_ready.put((p, scratch))
            log.info("policy %s built in %.1f s", getattr(p, "name", "?"), time.perf_counter() - t0)
        except Exception:
            log.exception("policy factory failed; serving with the fallback policy")

    def _adopt_policy(self) -> None:
        """Serving thread: install a policy built in the background, with what it computed."""
        try:
            p, scratch = self._policy_ready.get_nowait()
        except queue.Empty:
            return
        self.state.offline.update(scratch.offline)
        if scratch.users and not self.state.users:
            self.state.users = scratch.users
        if scratch.safe_list and scratch.safe_list != self.state.safe_list:
            self.state.safe_list = scratch.safe_list
        self.runner.set_policy(p)
        log.info("policy %s live", getattr(p, "name", "?"))

    def _history_dump(self):
        """catalog / history_sessions / history_events for policy factories: the simulator's own history
        on a FakeStore, else the Parquet dump in $RTREC_DATA (scripts/download_history.py); None if absent."""
        sim = getattr(self.store, "sim", None)
        if sim is not None:
            days = getattr(getattr(sim, "cfg", None), "history_days", 60)
            return sim.history(days=days)
        d = Path(os.environ.get("RTREC_DATA", "data"))
        if (d / "catalog.parquet").exists():
            from types import SimpleNamespace

            from rtrec.data.load import load_history
            h = load_history(d)
            return SimpleNamespace(catalog=h.catalog, history_sessions=h.sessions, history_events=h.events)
        log.warning("no history dump found (%s): policy built without offline artefacts", d)
        return None

    def _backfill_catalog_updates(self) -> None:
        while True:
            rows = self.store.catalog_updates_since(self.cur_updates.position, limit=self.cfg.page_limit_rows)
            before = self.cur_updates.position
            fresh = self.cur_updates.filter(rows)
            self.tracker.apply(fresh)
            if len(rows) < self.cfg.page_limit_rows or self.cur_updates.position == before:
                break

    def _load_safe_list(self) -> list[str]:
        cache = self.state_dir / "safe_list.json" if self.state_dir else None
        try:
            if hasattr(self.store, "best_sellers"):
                lst = list(self.store.best_sellers())
                if lst:
                    if cache:
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        cache.write_text(json.dumps(lst))
                    return lst
        except Exception:
            log.exception("best_sellers() failed")
        if cache and cache.exists():
            return json.loads(cache.read_text())
        return sorted(self.state.catalog)   # last resort: any deterministic order

    # ================================================================== fetchers (I/O, any thread)
    def fetch_requests(self) -> list[dict]:
        rows = self.store.requests_since(self.cur_requests.position, limit=self.cfg.page_limit_requests)
        return self.cur_requests.filter(rows)

    def fetch_catalog(self) -> list[dict]:
        return self.cur_updates.filter(self.store.catalog_updates_since(self.cur_updates.position,
                                                                        limit=self.cfg.page_limit_rows))

    def fetch_events(self) -> list[dict]:
        return self.cur_events.filter(self.store.events_since(self.cur_events.position,
                                                              limit=self.cfg.page_limit_rows))

    def fetch_feedback(self) -> list[dict]:
        rows = self.store.feedback_since(self.cur_feedback.position, limit=self.cfg.page_limit_rows)
        return [r for r in self.cur_feedback.filter(rows) if r.get("request_id") != "__init__"]

    # ================================================================== serving thread
    def serve(self, reqs: list[dict]) -> list[Submission]:
        """Answer a batch. Never raises; every fresh request gets exactly one answer."""
        now = self.clock.now()
        fresh = []
        for r in reqs:
            rid = r.get("request_id")
            if not rid or rid in self.journal:
                self.metrics.inc("skipped_already_answered")
                continue
            age = now - parse_ts(r.get("ts"))
            self.metrics.visible_lag_s.add(now, age)
            if age > self.cfg.stale_s:
                self.metrics.inc("skipped_stale")
                continue
            fresh.append(r)
        if not fresh:
            return []
        fresh.sort(key=lambda r: parse_ts(r.get("ts")))
        t0 = time.perf_counter()
        level1 = self.runner.run(fresh, self.state)
        out: list[tuple[Submission, float]] = []
        for r in fresh:
            items, mv = level1.get(r["request_id"]), self.runner.name
            if items is not None and hasattr(self.runner.policy, "version_for"):
                try:   # A4's A/B router names the arm that answered
                    mv = str(self.runner.policy.version_for(r))
                except Exception:  # noqa: BLE001
                    pass
            if items is None:
                mv = self.fallback.name
                try:
                    items = self.fallback.recommend(r, self.state)
                    self.metrics.inc("served_level2")
                except Exception:  # noqa: BLE001
                    items, mv = [], "safe"
                    self.metrics.inc("served_level3")
            else:
                self.metrics.inc("served_level1")
            final = finalize(items, self.state)
            if len(final) < 10:
                self.metrics.inc("served_short")   # fewer than 10 products available: nothing better exists
            out.append((Submission(request_id=r["request_id"], items=final, model_version=mv),
                        parse_ts(r.get("ts"))))
        self.metrics.compute_ms.add(now, (time.perf_counter() - t0) * 1000)
        self._emit(out)
        self.hooks.call("on_served", [(r, s["items"], s["model_version"]) for r, (s, _) in zip(fresh, out)],
                        self.state)
        return [s for s, _ in out]

    def _emit(self, rows: list[tuple[Submission, float]]) -> None:
        if self.heartbeat.active:
            self.writer.enqueue(rows)
        else:
            self._shadow.extend(rows)
            cut = self.clock.now() - self.cfg.give_up_s
            self._shadow = [x for x in self._shadow if x[1] >= cut]
            self.metrics.inc("shadow_answers", len(rows))

    def _became_active(self) -> None:
        """Standby taking over: write the shadow answers that can still be on time."""
        cut = self.clock.now() - self.cfg.give_up_s
        rows = [x for x in self._shadow if x[1] >= cut and x[0]["request_id"] not in self.journal]
        self._shadow = []
        self.writer.enqueue(rows)
        self.metrics.inc("takeover_resent", len(rows))

    def apply_updates(self, kind: str, rows: list[dict]) -> None:
        if not rows:
            return
        if kind == "catalog":
            if self.tracker.apply(rows):
                self.tracker.publish(self.state)
            self.metrics.inc("catalog_updates", len(rows))
            name = "on_catalog_updates"
        elif kind == "events":
            self.metrics.inc("events", len(rows))
            name = "on_events"
        else:
            for f in rows:
                try:
                    self.metrics.fb_s.add(self.clock.now(), float(f.get("latency_ms") or 0) / 1000)
                except (TypeError, ValueError):
                    pass
                self.metrics.fb_reasons[f.get("fallback_reason") or "ok"] += 1
            self.metrics.inc("feedback", len(rows))
            name = "on_feedback"
        rows = sorted(rows, key=lambda r: parse_ts(r.get("ts")))
        for i in range(0, len(rows), self.cfg.hook_chunk):
            self.hooks.call(name, rows[i:i + self.cfg.hook_chunk], self.state)

    def _drain_updates(self, only_catalog: bool = False, budget_s: float = 0.05) -> None:
        """Apply queued update batches. Catalogue first: it decides validity."""
        deadline = time.perf_counter() + budget_s
        held = []
        while time.perf_counter() < deadline:
            try:
                kind, rows = self._upd_q.get_nowait()
            except queue.Empty:
                break
            if only_catalog and kind != "catalog":
                held.append((kind, rows))
                continue
            self.apply_updates(kind, rows)
        for x in held:
            self._upd_q.put(x)

    def _periodic(self, now: float) -> None:
        if self._is_due("tick", now, self.cfg.tick_interval):
            self.hooks.call("on_tick", self.state, now)
        if self._is_due("status", now, self.cfg.status_interval):
            self._report_status(now)
        if self.state_dir and self._is_due("snapshot", now, self.cfg.snapshot_interval, first=False):
            threading.Thread(target=self.save_snapshot, name="snapshot", daemon=True).start()

    def _is_due(self, key: str, now: float, every: float, first: bool = True) -> bool:
        nxt = self._due.get(key)
        if nxt is None:
            self._due[key] = now + every
            return first
        if now >= nxt:
            self._due[key] = now + every
            return True
        return False

    # ================================================================== heartbeat
    def do_heartbeat(self) -> None:
        was = self.heartbeat.active
        try:
            self.heartbeat.check()
        except Exception as e:  # noqa: BLE001 - keep the previous decision
            self.metrics.inc("heartbeat_read_errors")
            log.debug("heartbeat read failed: %s", e)
        if self.heartbeat.active and not was:
            self._became_active()
        try:
            self.heartbeat.beat()
        except Exception as e:  # noqa: BLE001
            self.metrics.inc("heartbeat_write_errors")
            log.debug("heartbeat write failed: %s", e)

    # ================================================================== status / snapshot
    def status(self) -> dict:
        now = self.clock.now()
        s = self.metrics.snapshot(now)
        s.update({"instance": self.cfg.instance, "role": self.cfg.role, "active": self.heartbeat.active,
                  "policy": self.runner.name, "policy_timeouts": self.runner.timeouts,
                  "policy_errors": self.runner.errors, "writer_backlog": self.writer.backlog(),
                  "available": len(self.state.available), "hook_errors": dict(self.hooks.errors),
                  "hook_slow": dict(self.hooks.slow), "now": now})
        return s

    def _report_status(self, now: float) -> None:
        s = self.status()
        c, a = s["counters"], s["ack_latency_s"]
        log.info("served=%d l1=%d l2=%d l3=%d stale=%d late_drop=%d ins_err=%d ack p50/p99=%s/%s fb=%s",
                 c.get("submitted", 0), c.get("served_level1", 0), c.get("served_level2", 0),
                 c.get("served_level3", 0), c.get("skipped_stale", 0), c.get("dropped_too_late", 0),
                 c.get("insert_errors", 0), a.get("p50"), a.get("p99"), s["feedback_reasons"])
        if self.state_dir:
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.state_dir / "status.json.tmp"
                tmp.write_text(json.dumps(s, default=str))
                os.replace(tmp, self.state_dir / "status.json")
            except OSError:
                log.exception("status file write failed")
        threading.Thread(target=self._scratch_status, args=(s,), daemon=True).start()

    def _scratch_status(self, s: dict) -> None:
        try:
            self.store.insert_scratch([{"kind": "runtime_status", "ts": to_iso(s["now"]), "ts_epoch": s["now"],
                                        "instance": self.cfg.instance, "status": json.dumps(s, default=str)}])
        except Exception:  # noqa: BLE001
            self.metrics.inc("status_write_errors")

    def save_snapshot(self) -> None:
        """Persist learnt state (not offline artefacts: those reload from disk) for a fast warm restart."""
        if not self.state_dir:
            return
        st = self.state
        try:
            blob = pickle.dumps({
                "saved_at": self.clock.now(), "users": st.users, "live": st.live,
                "tracker": self.tracker, "updates_pos": self.cur_updates.position,
                "events_pos": self.cur_events.position, "feedback_pos": self.cur_feedback.position,
                "safe_list": st.safe_list}, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:  # noqa: BLE001 - e.g. a dict mutated while pickling: try next time
            log.warning("snapshot skipped: %s", e)
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_dir / "snapshot.pkl.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, self.state_dir / "snapshot.pkl")

    def _restore_snapshot(self) -> bool:
        p = self.state_dir / "snapshot.pkl" if self.state_dir else None
        if not p or not p.exists():
            return False
        try:
            snap = pickle.loads(p.read_bytes())
        except Exception:  # noqa: BLE001
            log.exception("snapshot unreadable; cold start")
            return False
        self.state.users = snap.get("users") or {}
        self.state.live = snap.get("live") or {}
        self.state.safe_list = snap.get("safe_list") or []
        self.tracker = snap.get("tracker") or CatalogTracker()
        self.cur_updates.position = snap.get("updates_pos", 0.0)
        self.cur_events.position = max(self.cur_events.position, snap.get("events_pos", 0.0))
        self.cur_feedback.position = max(self.cur_feedback.position, snap.get("feedback_pos", 0.0))
        log.info("snapshot restored (%.0f s old)", self.clock.now() - snap.get("saved_at", 0))
        return True

    # ================================================================== synchronous mode (tests)
    def step(self) -> list[Submission]:
        """One full iteration on the calling thread: fetch, apply, serve, write."""
        if not self.started:
            self.start()
        now = self.clock.now()
        if self._is_due("catalog", now, self.cfg.catalog_interval):
            self._guarded(lambda: self.apply_updates("catalog", self.fetch_catalog()), "catalog")
        if self._is_due("events", now, self.cfg.events_interval):
            self._guarded(lambda: self.apply_updates("events", self.fetch_events()), "events")
        if self._is_due("feedback", now, self.cfg.feedback_interval):
            self._guarded(lambda: self.apply_updates("feedback", self.fetch_feedback()), "feedback")
        if self._is_due("heartbeat", now, self.cfg.heartbeat_interval):
            self.do_heartbeat()
        self._adopt_policy()
        out: list[Submission] = []
        reqs = self._guarded(self.fetch_requests, "requests") or []
        if reqs:
            out = self.serve(reqs)
        self.writer.flush_once()
        self._periodic(now)
        return out

    def _guarded(self, fn, what: str):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            self.metrics.inc(f"fetch_errors_{what}")
            log.debug("%s fetch failed: %s", what, e)
            return None

    # ================================================================== threaded mode (production)
    def _poller(self, what: str, fetch, interval: float, sink, offset: float = 0.0) -> None:
        errors = 0
        self._stop.wait(offset)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                rows = fetch()
                errors = 0
                if rows:
                    sink(rows)
            except Exception as e:  # noqa: BLE001 - keep polling: shoppers keep arriving
                errors += 1
                self.metrics.inc(f"fetch_errors_{what}")
                if errors <= 3 or errors % 50 == 0:
                    log.warning("%s fetch failed (%d in a row): %s", what, errors, e)
            # poll again quickly after an error; never sleep the 2 s the baseline does
            self._stop.wait(max(0.0, interval - (time.monotonic() - t0)))

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.do_heartbeat()
            self._stop.wait(max(0.0, self.cfg.heartbeat_interval - (time.monotonic() - t0)))

    def run(self, duration: float | None = None) -> None:
        if not self.started:
            self.start()
        c = self.cfg
        n_req = max(1, c.request_pollers)
        specs = [("requests", self.fetch_requests, c.poll_interval, self._req_q.put, i * c.poll_interval / n_req)
                 for i in range(n_req)]
        specs += [
            ("catalog", self.fetch_catalog, c.catalog_interval, lambda rows: self._upd_q.put(("catalog", rows))),
            ("events", self.fetch_events, c.events_interval, lambda rows: self._upd_q.put(("events", rows))),
            ("feedback", self.fetch_feedback, c.feedback_interval, lambda rows: self._upd_q.put(("feedback", rows))),
        ]
        for i, (name, fetch, every, sink, *offset) in enumerate(specs):
            self._threads.append(threading.Thread(target=self._poller, args=(name, fetch, every, sink, *offset),
                                                  name=f"poll-{name}-{i}", daemon=True))
        self._threads.append(threading.Thread(target=self.writer.run, args=(self._stop,), name="writer",
                                              daemon=True))
        self._threads.append(threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True))
        for t in self._threads:
            t.start()
        end = None if duration is None else time.monotonic() + duration
        try:
            while not self._stop.is_set() and (end is None or time.monotonic() < end):
                try:
                    reqs = self._req_q.get(timeout=0.02)
                except queue.Empty:
                    reqs = None
                self._adopt_policy()
                if reqs:
                    while True:   # merge everything already waiting into one batch
                        try:
                            reqs = reqs + self._req_q.get_nowait()
                        except queue.Empty:
                            break
                    self._drain_updates(only_catalog=True, budget_s=0.01)
                    try:
                        self.serve(reqs)
                    except Exception:  # pragma: no cover - serve() guards itself; belt and braces
                        log.exception("serve failed")
                        self.metrics.inc("serve_crash")
                else:
                    self._drain_updates()
                self._periodic(self.clock.now())
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        # writer thread gone: push what is still queued (one writer at a time, always)
        deadline = time.monotonic() + 1.0
        while self.writer.backlog() and time.monotonic() < deadline:
            self.writer.flush_once()
            time.sleep(0.01)
        self.runner.shutdown()
        if self.writer._pool:
            self.writer._pool.shutdown(wait=False)
        self.save_snapshot()
        self.journal.close()


# ====================================================================== helpers / CLI
def default_hooks() -> list:
    """A4's live learning, plugged in unless the caller passes its own hooks (RTREC_HOOKS=none disables)."""
    if os.environ.get("RTREC_HOOKS", "") == "none":
        return []
    try:
        from rtrec.live.hook import LiveHook
    except ImportError:
        log.warning("rtrec.live not available: serving without live learning")
        return []
    return [LiveHook()]


def _wants_args(factory) -> bool:
    """A3's policies are built as Policy(state, dump); older factories take nothing."""
    import inspect
    try:
        params = [p for p in inspect.signature(factory).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return False
    return len(params) >= 2

def _load_obj(spec: str):
    """'pkg.module:attr' -> the attribute (a class, a factory function or an instance)."""
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr or "make")


def as_factory(obj):
    """Class or factory function -> call it later; ready-made instance -> return it.
    (A class also has the instance's methods as attributes, so test isinstance(type) first.)"""
    if isinstance(obj, type):
        return obj                                   # a class: instantiate it
    if hasattr(obj, "recommend") or any(hasattr(obj, h) for h in HOOK_NAMES):
        return lambda: obj                           # already an instance
    if callable(obj):
        return obj                                   # a factory function
    raise TypeError(f"{obj!r} is neither a class, a factory nor a policy/hook instance")


def build_store(kind: str, args):
    from rtrec.stores import make_store
    return make_store(kind, seed=args.seed, rate_multiplier=args.rate_multiplier)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default=os.environ.get("RTREC_STORE", "fake"), choices=["fake", "rawtree"])
    ap.add_argument("--policy", default=os.environ.get("RTREC_POLICY", ""),
                    help="module:factory returning a Policy (loaded in the background); default: built-in fallback")
    ap.add_argument("--hooks", default=os.environ.get("RTREC_HOOKS", ""),
                    help="comma-separated module:Class|factory for hook objects; default A4's LiveHook; 'none'")
    ap.add_argument("--dashboard", type=int, default=int(os.environ.get("RTREC_DASHBOARD", "0")),
                    help="port: also run A5's dashboard in this process (needed with the in-memory fake store)")
    ap.add_argument("--role", default=os.environ.get("RTREC_ROLE", "primary"), choices=["primary", "standby"])
    ap.add_argument("--state-dir", default=os.environ.get("RTREC_STATE_DIR", "artifacts/runtime"))
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: forever)")
    ap.add_argument("--seed", type=int, default=0, help="fake store only")
    ap.add_argument("--rate-multiplier", type=float, default=1.0, help="fake store only")
    ap.add_argument("--log-level", default=os.environ.get("RTREC_LOG", "INFO"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = build_store(args.store, args)
    factory = as_factory(_load_obj(args.policy)) if args.policy else None
    if args.hooks in ("", None):
        hooks = None                       # Server default: LiveHook
    elif args.hooks == "none":
        hooks = []
    else:
        hooks = [as_factory(_load_obj(spec.strip()))() for spec in args.hooks.split(",") if spec.strip()]
    cfg = ServeConfig(role=args.role, state_dir=args.state_dir)
    server = Server(store, policy_factory=factory, hooks=hooks, config=cfg)
    if args.dashboard:
        from dashboard.server import start_in_background
        start_in_background(store, port=args.dashboard)
        log.info("dashboard on http://127.0.0.1:%d", args.dashboard)
    signal.signal(signal.SIGTERM, lambda *_: server._stop.set())
    server.run(duration=args.duration)
    if hasattr(store, "report"):
        print(json.dumps(store.report(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
