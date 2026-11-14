"""Local stand-in for the organisers' hidden shopper simulator.

We have no API key yet, so everything is built and tested against a synthetic shop that has the
same tables, rates and rules as the real one. Owner: A2 (data & simulator). Protocol owner: master.

The simulator is deterministic given a seed and a clock, so tests are reproducible:

    sim = make_simulator(seed=7)              # sim/shop.py (A2)
    sim.history(days=60) -> HistoryDump       # catalog + history_sessions + history_events (DataFrames)
    sim.advance(now)    -> Tick               # new requests / events / catalog_updates up to `now`
    sim.judge(sub, received_at) -> Feedback   # scores one submission like the organisers would
    sim.expected_purchase(req, items) -> float  # the "no luck" metric: P(purchase | 10 shown)
    sim.oracle(req) -> list[str]              # best 10 for that request (score = 100 anchor)

FakeStore (A1) wraps a Simulator and exposes it through rtrec.store.Store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from rtrec.schemas import CatalogUpdate, Event, Feedback, Request, Submission


@dataclass
class Tick:
    requests: list[Request] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    catalog_updates: list[CatalogUpdate] = field(default_factory=list)


@dataclass
class HistoryDump:
    catalog: "object"            # pandas.DataFrame, columns of rtrec.schemas.Product
    history_sessions: "object"   # pandas.DataFrame, columns of rtrec.schemas.HistorySession
    history_events: "object"     # pandas.DataFrame, columns of rtrec.schemas.Event


class Simulator(Protocol):
    def history(self, days: int = 60) -> HistoryDump: ...
    def advance(self, now: float) -> Tick: ...
    def judge(self, sub: Submission, received_at: float) -> Feedback: ...
    def expected_purchase(self, req: Request, items: list[str]) -> float: ...
    def oracle(self, req: Request) -> list[str]: ...
