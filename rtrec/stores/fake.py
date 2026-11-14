"""FakeStore: the rtrec.store.Store protocol over a local simulator, with the organisers' timing.

Why: we have no API key, so the whole serving loop is built and measured against this. It copies the
parts of the real platform that decide our reliability score:

  * a request becomes visible to us `visibility_delay` seconds after its `ts` (ingestion lag);
  * our insert lands `insert_latency` seconds after the call (HTTP round trip);
  * the organisers poll team.submissions every `judge_poll` (0.5 s): an answer counts as received
    at the first poll after it landed, and must be received within 3 s of `ts`;
  * only the first answer per request counts; later ones are counted as duplicates;
  * an answer missing at the deadline gets fallback_reason "missing";
  * feedback rows appear `feedback_delay` seconds after the deadline;
  * insert_deduplication_token is honoured like RawTree's (a retried identical batch is ignored).

Works with a SimClock (deterministic tests) or a RealClock (load test). Fault injection
(`faults`) makes calls fail, hang or fail *after* writing, to test retries and dedupe.
Everything here is simulated; nothing in it says anything about the live platform's numbers.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from collections import Counter, deque
from dataclasses import dataclass, field

from rtrec.runtime.clock import RealClock, parse_ts, to_iso
from rtrec.schemas import DEADLINE_S, CatalogUpdate, Event, Feedback, Product, Request, Submission


class StoreError(RuntimeError):
    """Transient store failure (network, 5xx). Callers retry."""


class RateLimited(StoreError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass
class Faults:
    """Probabilities per call. `ambiguous_insert`: the rows ARE written but the call raises."""
    read_error: float = 0.0
    insert_error: float = 0.0
    ambiguous_insert: float = 0.0
    rate_limit: float = 0.0
    latency: float = 0.0          # extra seconds per call (real-time mode only: really sleeps)
    latency_jitter: float = 0.0
    spike_prob: float = 0.0       # probability of a slow call
    spike_s: float = 1.0
    seed: int = 0


@dataclass
class _ReqRecord:
    req: Request
    ts: float
    visible_at: float
    first_landed: float | None = None
    received_at: float | None = None
    items: list[str] = field(default_factory=list)
    model_version: str = ""
    n_answers: int = 0
    distinct_answers: set = field(default_factory=set)
    feedback: Feedback | None = None
    expected: float | None = None
    fb_emitted: bool = False


def _canon(rows: list[dict]) -> str:
    return hashlib.sha1(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FakeStore:
    def __init__(self, sim, clock=None, team: str = "team_00", *, visibility_delay: float = 0.4,
                 insert_latency: float = 0.15, judge_poll: float = 0.5, feedback_delay: float = 2.5,
                 overlap: float = 1.0, faults: Faults | None = None, track_expected: bool = False,
                 catalog_rows: list[Product] | None = None):
        self.sim = sim
        self.clock = clock or RealClock()
        self.team = team
        self.visibility_delay = visibility_delay
        self.insert_latency = insert_latency
        self.judge_poll = judge_poll
        self.feedback_delay = feedback_delay
        self.overlap = overlap
        self.faults = faults or Faults()
        self.track_expected = track_expected
        self._rng = random.Random(self.faults.seed)
        self._lock = threading.RLock()
        self._t0 = self.clock.now()
        self._pumped_until = self._t0
        self._next_poll = self._t0 + judge_poll
        self._catalog = catalog_rows if catalog_rows is not None else self._initial_catalog()
        self.requests: dict[str, _ReqRecord] = {}
        self._req_list: deque[_ReqRecord] = deque()   # in ts order, feedback not yet emitted
        self._done: list[_ReqRecord] = []
        self.events: list[Event] = []
        self.catalog_updates: list[CatalogUpdate] = []
        self.feedback: list[Feedback] = []
        self.submissions: list[tuple[float, Submission]] = []   # (landed_at, row), every row ever written
        self.scratch: list[dict] = []
        self._pending_landing: list[tuple[float, Submission]] = []
        self._tokens: set[str] = set()
        self.calls = Counter()

    def _initial_catalog(self) -> list[Product]:
        for name in ("catalog_at_start", "catalog"):        # stub / A2's ShopSimulator
            if callable(getattr(self.sim, name, None)):
                return getattr(self.sim, name)()
        return self.sim.history(days=1).catalog.to_dict("records")

    # ------------------------------------------------------------------ fault injection
    def _call(self, kind: str, write: bool = False):
        self.calls[kind] += 1
        f = self.faults
        if f.latency or f.latency_jitter or f.spike_prob:
            dt = f.latency + self._rng.random() * f.latency_jitter
            if self._rng.random() < f.spike_prob:
                dt += f.spike_s
            if isinstance(self.clock, RealClock):
                self.clock.sleep(dt)
        if f.rate_limit and self._rng.random() < f.rate_limit:
            raise RateLimited(0.2)
        if (f.insert_error if write else f.read_error) and self._rng.random() < (f.insert_error if write else f.read_error):
            raise StoreError(f"injected {kind} failure")

    # ------------------------------------------------------------------ simulation pump
    def pump(self) -> None:
        """Advance the simulator to now, judge at every organiser poll, emit due feedback."""
        with self._lock:
            now = self.clock.now()
            while self._next_poll <= now:
                self._advance_to(self._next_poll)
                self._judge_poll(self._next_poll)
                self._next_poll += self.judge_poll
            self._advance_to(now)

    def _advance_to(self, t: float) -> None:
        if t <= self._pumped_until:
            return
        tick = self.sim.advance(t)
        for r in tick.requests:
            ts = parse_ts(r["ts"])
            if ts < self._t0:
                continue   # the simulator pre-warms before our store opened: nobody could answer those
            rec = _ReqRecord(req=r, ts=ts, visible_at=ts + self.visibility_delay)
            self.requests[r["request_id"]] = rec
            self._req_list.append(rec)
        self.events.extend(tick.events)
        self.catalog_updates.extend(tick.catalog_updates)
        self._pumped_until = t

    def _judge_poll(self, t: float) -> None:
        landed = [x for x in self._pending_landing if x[0] <= t]
        self._pending_landing = [x for x in self._pending_landing if x[0] > t]
        for at, row in sorted(landed, key=lambda x: x[0]):
            rec = self.requests.get(row["request_id"])
            if rec is None:
                continue
            rec.n_answers += 1
            rec.distinct_answers.add(tuple(row.get("items") or ()))
            if rec.first_landed is None:
                rec.first_landed, rec.received_at = at, t
                rec.items, rec.model_version = list(row.get("items") or []), row.get("model_version", "")
                rec.feedback = self.sim.judge(row, t)
                if self.track_expected and rec.feedback["served_by"] == "team":
                    rec.expected = self.sim.expected_purchase(rec.req, rec.items)
        # missing answers and feedback emission
        for rec in self._req_list:
            if rec.ts + DEADLINE_S + self.feedback_delay > t:
                break   # _req_list is in ts order: nothing later is due yet
            if rec.feedback is None:
                if callable(getattr(self.sim, "judge_missing", None)):
                    rec.feedback = self.sim.judge_missing(rec.req["request_id"])
                else:
                    rec.feedback = Feedback(request_id=rec.req["request_id"], ts=to_iso(t), shown=[],
                                            served_by="fallback", fallback_reason="missing", latency_ms=0.0,
                                            clicked="", purchased="", revenue_eur=0.0)
            if not rec.fb_emitted:
                # the simulator may stamp its own (later) feedback time; never publish before it
                due = max(rec.ts + DEADLINE_S + self.feedback_delay, parse_ts(rec.feedback.get("ts")))
                if due > t:
                    continue
                fb = dict(rec.feedback)
                fb["ts"] = to_iso(due)
                self.feedback.append(fb)
                rec.fb_emitted = True
        while self._req_list and self._req_list[0].fb_emitted:
            self._done.append(self._req_list.popleft())

    # ------------------------------------------------------------------ Store protocol: reads
    def catalog(self) -> list[Product]:
        self._call("catalog")
        return [dict(p) for p in self._catalog]

    def requests_since(self, cursor: float, limit: int = 500) -> list[Request]:
        self._call("requests")
        self.pump()
        now = self.clock.now()
        lo = cursor - self.overlap
        with self._lock:
            out = [rec.req for rec in self._iter_recent_requests(lo) if rec.ts > lo and rec.visible_at <= now]
        return out[:limit]

    def _iter_recent_requests(self, lo: float):
        # requests still in flight are in _req_list; only scan the tail of _done when the cursor is old
        done = self._done
        i = len(done)
        while i > 0 and done[i - 1].ts > lo:
            i -= 1
        yield from done[i:]
        yield from self._req_list

    def _since(self, rows: list[dict], cursor: float, limit: int) -> list[dict]:
        """Rows with cursor - overlap < ts, already visible (ts + visibility_delay <= now)."""
        lo = cursor - self.overlap
        hi = self.clock.now() - self.visibility_delay
        i = len(rows)   # rows are appended in ts order: scan back from the tail
        while i > 0 and parse_ts(rows[i - 1]["ts"]) > lo:
            i -= 1
        out = []
        for r in rows[i:]:
            if parse_ts(r["ts"]) > hi or len(out) >= limit:
                break
            out.append(dict(r))
        return out

    def events_since(self, cursor: float, limit: int = 50_000) -> list[Event]:
        self._call("events")
        self.pump()
        with self._lock:
            return self._since(self.events, cursor, limit)

    def catalog_updates_since(self, cursor: float, limit: int = 50_000) -> list[CatalogUpdate]:
        self._call("catalog_updates")
        self.pump()
        with self._lock:
            return self._since(self.catalog_updates, cursor, limit)

    def feedback_since(self, cursor: float, limit: int = 50_000) -> list[Feedback]:
        self._call("feedback")
        self.pump()
        with self._lock:
            return self._since(self.feedback, cursor, limit)

    def scratch_since(self, cursor: float, limit: int = 50_000, kind: str | None = None) -> list[dict]:
        """Scratch rows with ts > cursor - overlap (rows carry an ISO `ts`), optionally of one kind."""
        self._call("scratch")
        lo = cursor - self.overlap
        with self._lock:
            return [dict(r) for r in self.scratch
                    if parse_ts(r.get("ts") or r.get("ts_epoch")) > lo and (kind is None or r.get("kind") == kind)
                    ][:limit]

    def submissions_since(self, cursor: float, limit: int = 50_000) -> list[Submission]:
        """Our own answers that landed after cursor - overlap (RawTree has no ts on submissions; here
        we know the landing time)."""
        self._call("submissions")
        lo = cursor - self.overlap
        now = self.clock.now()
        with self._lock:
            return [dict(r) for at, r in self.submissions if lo < at <= now][:limit]

    def best_sellers(self) -> list[str]:
        """Not in the protocol: the organisers' baseline ranking (all-time best sellers; ties: higher id first)."""
        if hasattr(self.sim, "best_sellers"):
            return list(self.sim.best_sellers())
        if not hasattr(self, "_best"):
            hist = self.sim.history(days=getattr(getattr(self.sim, "cfg", None), "history_days", 60)).history_events
            buys = hist[hist["event_type"] == "purchase"].groupby("product_id").size()
            self._best = [p for _, p in sorted(((int(n), str(p)) for p, n in buys.items()), reverse=True)]
        return list(self._best)

    # ------------------------------------------------------------------ Store protocol: writes
    def insert_submissions(self, rows: list[Submission], dedup_token: str | None = None) -> None:
        self._call("insert_submissions", write=True)
        token = dedup_token or _canon(rows)
        ambiguous = self.faults.ambiguous_insert and self._rng.random() < self.faults.ambiguous_insert
        with self._lock:
            if token not in self._tokens:
                self._tokens.add(token)
                landed = self.clock.now() + self.insert_latency
                for r in rows:
                    row = {"request_id": r["request_id"], "items": list(r["items"]),
                           "model_version": r.get("model_version", "")}
                    self.submissions.append((landed, row))
                    self._pending_landing.append((landed, row))
        if ambiguous:
            raise StoreError("injected ambiguous insert failure (rows were written)")

    def insert_scratch(self, rows: list[dict]) -> None:
        self._call("insert_scratch", write=True)
        with self._lock:
            self.scratch.extend(dict(r) for r in rows)

    def query(self, sql: str) -> list[dict]:
        raise NotImplementedError("FakeStore has no SQL engine")

    # ------------------------------------------------------------------ report (what the organisers would see)
    def report(self, since: float | None = None, until: float | None = None) -> dict:
        """Reliability as the organisers measure it, over requests whose feedback is due."""
        self.pump()
        with self._lock:
            recs = [r for r in self._done if (since is None or r.ts >= since) and (until is None or r.ts < until)]
        n = len(recs)
        reasons = Counter(r.feedback["fallback_reason"] or "ok" for r in recs)
        lat = sorted((r.received_at - r.ts) for r in recs if r.received_at is not None)
        landed = sorted((r.first_landed - r.ts) for r in recs if r.first_landed is not None)
        exp = [r.expected for r in recs if r.expected is not None]
        mv = Counter(r.model_version for r in recs if r.received_at is not None)
        return {
            "requests": n,
            "on_time_valid": reasons.get("ok", 0),
            "reliability": reasons.get("ok", 0) / n if n else 0.0,
            "reasons": dict(reasons),
            "duplicate_answers": sum(max(0, r.n_answers - 1) for r in recs),
            "conflicting_answers": sum(1 for r in recs if len(r.distinct_answers) > 1),
            "latency_received_s": _pcts(lat),
            "latency_landed_s": _pcts(landed),
            "expected_purchases_per_100": 100 * sum(exp) / n if exp and n else None,
            "model_versions": dict(mv),
            # A2's simulator scores what it judged: team / baseline / oracle expected purchases, score
            "sim": self.sim.report() if callable(getattr(self.sim, "report", None)) else None,
        }


def _pcts(xs: list[float]) -> dict:
    if not xs:
        return {}
    def q(p):
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)
    return {"p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": round(xs[-1], 3), "n": len(xs)}
