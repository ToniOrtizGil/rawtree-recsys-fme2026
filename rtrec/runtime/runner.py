"""Runs the level-1 policy with a time budget, on a worker thread, so a bad model cannot stall serving.

Why a thread: Policy.recommend is plain Python and cannot be interrupted. If it hangs or gets slow
(a bug, a huge candidate set, a GC pause), the serving thread stops waiting after `budget_s`, answers
the rest of the batch with the fallback policy and skips level 1 until the stuck call returns.
A circuit breaker also benches a policy that keeps raising.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from rtrec.contract import Policy, State
from rtrec.schemas import Request

log = logging.getLogger("rtrec.runner")


class PolicyRunner:
    def __init__(self, policy: Policy | None, *, budget_s: float = 0.15, breaker_errors: int = 20,
                 breaker_cooldown_s: float = 15.0):
        self.policy = policy
        self.budget_s = budget_s
        self.breaker_errors = breaker_errors
        self.breaker_cooldown_s = breaker_cooldown_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="policy")
        self._stuck: Future | None = None
        self._consecutive_errors = 0
        self._benched_until = 0.0
        self.errors = 0
        self.timeouts = 0
        self._lock = threading.Lock()

    def set_policy(self, policy: Policy) -> None:
        with self._lock:
            self.policy = policy
            self._consecutive_errors = 0
            self._benched_until = 0.0

    @property
    def name(self) -> str:
        return getattr(self.policy, "name", "none")

    def available(self) -> bool:
        if self.policy is None or time.monotonic() < self._benched_until:
            return False
        if self._stuck is not None:
            if not self._stuck.done():
                return False
            self._stuck = None
        return True

    def run(self, reqs: list[Request], state: State) -> dict[str, list[str]]:
        """request_id -> items for the requests level 1 answered in time (possibly none)."""
        if not reqs or not self.available():
            return {}
        policy = self.policy
        deadline = time.perf_counter() + self.budget_s
        out: dict[str, list[str]] = {}
        errs = [0]

        def work():
            for r in reqs:
                if time.perf_counter() > deadline:
                    break
                try:
                    items = policy.recommend(r, state)
                    out[r["request_id"]] = list(items) if items is not None else []
                except Exception:  # noqa: BLE001
                    errs[0] += 1
                    if self.errors + errs[0] <= 5:
                        log.exception("policy %s raised", getattr(policy, "name", "?"))
            return errs[0]

        fut = self._pool.submit(work)
        try:
            fut.result(timeout=self.budget_s + 0.02)
        except FutureTimeout:
            self.timeouts += 1
            self._stuck = fut
            log.warning("policy %s exceeded %.0f ms; serving fallback until it returns",
                        getattr(policy, "name", "?"), self.budget_s * 1000)
            try:   # whatever finished before the deadline is still valid work
                return dict(out)
            except RuntimeError:   # the worker inserted while we copied
                return {}
        self.errors += errs[0]
        if errs[0] and not out:
            self._consecutive_errors += errs[0]
        elif out:
            self._consecutive_errors = 0
        if self._consecutive_errors >= self.breaker_errors:
            self._benched_until = time.monotonic() + self.breaker_cooldown_s
            self._consecutive_errors = 0
            log.error("policy %s benched for %.0f s after repeated errors", getattr(policy, "name", "?"),
                      self.breaker_cooldown_s)
        return out

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
