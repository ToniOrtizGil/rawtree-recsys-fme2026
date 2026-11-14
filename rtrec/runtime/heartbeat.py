"""Heartbeats in team.scratch and the standby rule.

Two instances can run on two machines against the same team database:
  * primary: always writes answers, and writes a heartbeat row every `interval` seconds;
  * standby: computes every answer too (warm state, ready instantly) but only *writes* them while
    no new primary heartbeat has been seen for `takeover_after_s`. When the primary comes back, the
    standby goes quiet again.

Clock skew between machines does not matter: the standby never compares the primary's timestamps
with its own clock; it only notes, on its own clock, when the primary's sequence number last moved.
During a hand-over both may answer the same request for a moment; both answers are valid and only
one counts, so the overlap is harmless (a gap would not be).
"""
from __future__ import annotations

import logging
import os
import socket

from .clock import to_iso

log = logging.getLogger("rtrec.heartbeat")

def default_instance() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Heartbeat:
    def __init__(self, store, clock, *, role: str = "primary", instance: str | None = None,
                 takeover_after_s: float = 2.5, lookback_s: float = 120.0, model_version=None):
        assert role in ("primary", "standby")
        self.store = store
        self.clock = clock
        self.role = role
        self.instance = instance or default_instance()
        self.takeover_after_s = takeover_after_s
        self.lookback_s = lookback_s
        self.seq = 0
        self.started = clock.now()
        self._primary_seq: dict[str, int] = {}
        self._primary_seen_at: float | None = None   # our clock, last time a primary seq moved
        self.active = role == "primary"
        self.takeovers = 0
        self.model_version = model_version or (lambda: "")

    def beat(self, extra: dict | None = None) -> None:
        self.seq += 1
        now = self.clock.now()
        # protocol fields (rtrec/store.py): kind, ts, role, host, model_version; the rest is ours
        row = {"kind": "heartbeat", "ts": to_iso(now), "role": self.role, "host": socket.gethostname(),
               "model_version": self.model_version(), "instance": self.instance, "active": self.active,
               "seq": self.seq, "ts_epoch": now}
        row.update(extra or {})
        self.store.insert_scratch([row])

    def _read(self) -> list[dict]:
        # the cursor uses our clock against the primary's ts: a generous lookback absorbs any skew,
        # and the decision itself only looks at sequence numbers
        return self.store.scratch_since(self.clock.now() - self.lookback_s, kind="heartbeat")

    def check(self) -> bool:
        """Standby: update `active` from the primary's heartbeats. Returns True if it changed."""
        if self.role == "primary":
            return False
        now = self.clock.now()
        latest: dict[str, int] = {}
        for r in self._read():
            if r.get("role") != "primary" or r.get("instance") == self.instance:
                continue
            inst, seq = str(r.get("instance")), int(r.get("seq", 0))
            latest[inst] = max(seq, latest.get(inst, seq))
        for inst, seq in latest.items():
            if seq != self._primary_seq.get(inst):
                self._primary_seen_at = now
                self._primary_seq[inst] = seq
        # never seen a primary: wait one takeover period after our own start, then serve
        ref = self._primary_seen_at if self._primary_seen_at is not None else self.started
        should = now - ref > self.takeover_after_s
        if should != self.active:
            self.active = should
            if should:
                self.takeovers += 1
            log.warning("standby %s: %s", self.instance, "TAKING OVER (primary heartbeat lost)" if should
                        else "primary is back, going quiet")
            return True
        return False
