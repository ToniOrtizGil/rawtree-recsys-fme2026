"""Helpers to run the serving loop on a simulated clock (used by tests, the load test and A3/A4).

    env = SimEnv(seed=1)                    # SimClock + stub (or A2) simulator + FakeStore
    srv = env.server(policy=MyPolicy())     # a Server wired to the env
    env.run(srv, seconds=300)               # 5 minutes of traffic, deterministic, ~1 s of CPU
    env.store.report()                      # reliability / latency / expected purchases
"""
from __future__ import annotations

from rtrec.stores.fake import Faults, FakeStore
from rtrec.stores.stub_sim import StubParams, StubSimulator

from .clock import SimClock
from .serve import ServeConfig, Server


class SimEnv:
    def __init__(self, seed: int = 1, sim=None, params: StubParams | None = None, faults: Faults | None = None,
                 track_expected: bool = False, **store_kw):
        self.clock = SimClock()
        self.sim = sim or StubSimulator(seed=seed, start=self.clock.now(), params=params)
        self.store = FakeStore(self.sim, self.clock, faults=faults, track_expected=track_expected, **store_kw)

    def server(self, policy=None, state_dir=None, **cfg) -> Server:
        cfg.setdefault("state_dir", state_dir)
        return Server(self.store, policy, clock=self.clock, config=ServeConfig(**cfg))

    def run(self, servers, seconds: float, dt: float = 0.1, on_step=None) -> None:
        servers = servers if isinstance(servers, list) else [servers]
        for _ in range(int(seconds / dt)):
            self.clock.advance(dt)
            for s in servers:
                if s is not None:
                    s.step()
            if on_step:
                on_step(self)
