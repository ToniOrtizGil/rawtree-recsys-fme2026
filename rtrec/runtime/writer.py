"""The single writer of team.submissions.

Why one writer: batching (the rules ask for it, and 1 insert per request would hit rate limits), and
a single place that decides what is retried. At most one insert is in flight; whatever accumulates
while it is in flight becomes the next batch, so batches grow automatically under load.

Retry rules (an insert error does NOT prove nothing was written):
  * a failed batch is retried with *identical* rows, so the store's dedup token (hash of the
    content) makes the retry a no-op if the first attempt landed; even if it is not deduplicated,
    the duplicate carries the same items, so whichever copy counts, the answer is the same;
  * 429 -> wait `retry_after`; other errors -> short backoff (50, 100, 200 ms ...);
  * hedging: if an insert has not returned after `hedge_after_s`, an identical copy is sent in
    parallel and the first success wins (same dedup token, so at most one is stored). This cuts the
    tail when one HTTP call stalls, which is what decides p99;
  * rows that can no longer arrive in time (`give_up_s` after the request ts on our clock) are
    dropped from the batch: sending them only wastes the rate limit.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from rtrec.schemas import Submission

from .journal import Journal
from .metrics import Metrics

log = logging.getLogger("rtrec.writer")


class Writer:
    def __init__(self, store, clock, journal: Journal, metrics: Metrics, *, give_up_s: float = 4.0,
                 max_batch: int = 500, hedge_after_s: float | None = 0.35):
        self.store = store
        self.clock = clock
        self.journal = journal
        self.metrics = metrics
        self.give_up_s = give_up_s
        self.max_batch = max_batch
        self._pending: list[tuple[Submission, float]] = []
        self._inflight: list[tuple[Submission, float]] | None = None
        self._attempts = 0
        self._next_try = 0.0
        self._cv = threading.Condition()
        self.hedge_after_s = hedge_after_s
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="insert") if hedge_after_s else None

    def _insert(self, rows: list[Submission]) -> None:
        if self._pool is None:
            self.store.insert_submissions(rows)
            return
        first = self._pool.submit(self.store.insert_submissions, rows)
        done, _ = wait([first], timeout=self.hedge_after_s)
        if done:
            first.result()
            return
        self.metrics.inc("insert_hedged")
        pending = {first, self._pool.submit(self.store.insert_submissions, rows)}
        error: BaseException | None = None
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                if f.exception() is None:
                    return
                error = f.exception()
        raise error

    # -------------------------------------------------------------- producer side
    def enqueue(self, rows: list[tuple[Submission, float]]) -> None:
        """rows: (submission, request_ts). Journalled *before* any insert attempt."""
        if not rows:
            return
        self.journal.decided(rows)
        with self._cv:
            self._pending.extend(rows)
            self._cv.notify()

    def backlog(self) -> int:
        with self._cv:
            return len(self._pending) + len(self._inflight or ())

    # -------------------------------------------------------------- consumer side
    def flush_once(self) -> bool:
        """One insert attempt. Returns True if something was sent successfully."""
        now = self.clock.now()
        with self._cv:
            if self._inflight is None:
                if not self._pending:
                    return False
                self._inflight, self._pending = self._pending[:self.max_batch], self._pending[self.max_batch:]
                self._attempts = 0
            if now < self._next_try:
                return False
            batch = self._inflight
        alive = [(s, t) for s, t in batch if now - t <= self.give_up_s]
        if len(alive) < len(batch):
            self.metrics.inc("dropped_too_late", len(batch) - len(alive))
            self.journal.acked([s["request_id"] for s, t in batch if now - t > self.give_up_s])
        if not alive:
            with self._cv:
                self._inflight = None
            return False
        rows = [s for s, _ in alive]
        try:
            self._insert(rows)
        except Exception as e:  # noqa: BLE001 - every failure is retried or dropped, never fatal
            self._attempts += 1
            retry_after = getattr(e, "retry_after", None)
            delay = float(retry_after) if retry_after else min(0.05 * 2 ** (self._attempts - 1), 0.4)
            self._next_try = self.clock.now() + delay
            self.metrics.inc("insert_errors")
            if retry_after:
                self.metrics.inc("insert_429")
            if self._attempts <= 3 or self._attempts % 20 == 0:
                log.warning("insert of %d rows failed (attempt %d): %s", len(rows), self._attempts, e)
            with self._cv:
                self._inflight = alive
            return False
        done = self.clock.now()
        for _, t in alive:
            self.metrics.ack_s.add(done, done - t)
        self.metrics.inc("submitted", len(rows))
        self.metrics.inc("insert_batches")
        if self._attempts:
            self.metrics.inc("insert_retried_ok")
        self.journal.acked([s["request_id"] for s in rows])
        with self._cv:
            self._inflight = None
            self._attempts = 0
        return True

    def run(self, stop: threading.Event) -> None:
        """Thread body: insert as soon as rows are pending."""
        while not stop.is_set():
            with self._cv:
                if self._inflight is None and not self._pending:
                    self._cv.wait(timeout=0.05)
            wait = self._next_try - self.clock.now()
            if self._inflight is not None and wait > 0:
                stop.wait(min(wait, 0.05))
                continue
            try:
                self.flush_once()
            except Exception:  # pragma: no cover - flush_once already guards the store call
                log.exception("writer loop error")
                stop.wait(0.05)
