"""Store implementations. Pick one with RTREC_STORE=fake|rawtree; the rest of the code never knows which.

    make_store("fake")     FakeStore over sim/shop.py (A2) if present, else the stub simulator
    make_store("rawtree")  RawTreeStore (needs TEAM_ID and RAWTREE_API_KEY; UNTESTED LIVE until we get a key)
"""
from __future__ import annotations

import os


def make_simulator(seed: int = 0, start: float | None = None, rate_multiplier: float = 1.0):
    """A2's realistic simulator when it is installed, otherwise the runtime's stub."""
    import time
    start = time.time() if start is None else start
    try:
        from sim.shop import make_simulator as make_shop  # A2, branch agent/a2-data
    except ImportError:
        make_shop = None
    if make_shop is not None and os.environ.get("RTREC_SIM", "shop") == "shop":
        try:
            return make_shop(seed=seed, start=start, rate_multiplier=rate_multiplier)
        except TypeError:
            return make_shop(seed=seed)
    from .stub_sim import StubParams, StubSimulator
    p = StubParams()
    p.request_rate *= rate_multiplier
    p.event_rate *= rate_multiplier
    return StubSimulator(seed=seed, start=start, params=p)


def make_store(kind: str | None = None, **kw):
    kind = kind or os.environ.get("RTREC_STORE", "fake")
    if kind == "rawtree":
        from .rawtree import RawTreeStore
        return RawTreeStore.from_env()
    if kind == "fake":
        from .fake import FakeStore
        sim = make_simulator(seed=kw.get("seed", 0), rate_multiplier=kw.get("rate_multiplier", 1.0))
        return FakeStore(sim, track_expected=kw.get("track_expected", False))
    raise ValueError(f"unknown store {kind!r}")
