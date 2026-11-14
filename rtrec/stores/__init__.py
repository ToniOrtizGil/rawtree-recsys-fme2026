"""Store implementations. Pick one with RTREC_STORE=fake|rawtree; the rest of the code never knows which.

    make_store("fake")     FakeStore over sim/shop.py (A2) if present, else the stub simulator
    make_store("rawtree")  RawTreeStore (needs TEAM_ID and RAWTREE_API_KEY; UNTESTED LIVE until we get a key)
"""
from __future__ import annotations

import os


def make_simulator(seed: int = 0, start: float | None = None, rate_multiplier: float = 1.0, small: bool = False):
    """A2's realistic simulator (sim/shop.py) unless RTREC_SIM=stub; the runtime's stub otherwise.
    Both start their live stream at `start` (default: now), so real-time runs see fresh requests.
    Construction errors propagate: a silently misconfigured simulator once replayed hours of requests."""
    import time
    start = time.time() if start is None else start
    try:
        from sim.shop import make_simulator as make_shop
    except ImportError:
        make_shop = None
    if make_shop is not None and os.environ.get("RTREC_SIM", "shop") == "shop":
        from sim.params import SimConfig
        cfg = SimConfig.small() if small else SimConfig()
        return make_shop(seed=seed, config=cfg, stream_start=float(start),
                         requests_per_s=cfg.requests_per_s * rate_multiplier,
                         organic_sessions_per_s=cfg.organic_sessions_per_s * rate_multiplier)
    from .stub_sim import StubParams, StubSimulator
    p = StubParams()
    p.request_rate *= rate_multiplier
    p.event_rate *= rate_multiplier
    return StubSimulator(seed=seed, start=start, params=p)


def warm_up(sim) -> None:
    """Build the simulator's history now, not inside the first poll (it takes seconds and would make
    the first requests late). No advance(): stream rows must reach the FakeStore."""
    if callable(getattr(sim, "baseline_items", None)):
        sim.baseline_items()        # A2: builds history and freezes the organisers' best sellers


def make_store(kind: str | None = None, **kw):
    kind = kind or os.environ.get("RTREC_STORE", "fake")
    if kind == "rawtree":
        from .rawtree import RawTreeStore
        return RawTreeStore.from_env()
    if kind == "fake":
        from rtrec.runtime.clock import OffsetClock

        from .fake import FakeStore
        sim = make_simulator(seed=kw.get("seed", 0), rate_multiplier=kw.get("rate_multiplier", 1.0),
                             small=kw.get("small", False))
        warm_up(sim)
        # real time, starting at the simulator's stream_start (Server picks the clock up from store.clock)
        clock = OffsetClock(getattr(sim, "stream_start", None) or __import__("time").time())
        return FakeStore(sim, clock, track_expected=kw.get("track_expected", False))
    raise ValueError(f"unknown store {kind!r}")
