"""The arbiter must reproduce the leaderboard's anchors: baseline scores 0, oracle scores 100."""
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import arbiter  # noqa: E402
from sim.base import HistoryDump, Tick  # noqa: E402

T0 = 1_794_000_000.0
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731


class StubSim:
    """Tiny shop: 60 products in 3 subcategories; a shopper buys more from the subcategory they viewed."""

    def __init__(self, seed=1):
        self.rng = random.Random(seed)
        self.pids = [f"p_{i:03d}" for i in range(60)]
        self.sub = {p: f"s{i % 3}" for i, p in enumerate(self.pids)}
        self.t, self.k = T0, 0

    def history(self, days=60):
        cat = pd.DataFrame([{"product_id": p, "category": "c", "subcategory": self.sub[p], "brand": "b",
                             "price_eur": 10.0, "description": "x", "created_at": iso(T0 - 9e6), "is_active": True}
                            for p in self.pids])
        ev = pd.DataFrame([{"ts": iso(T0 - 100), "event_type": "purchase", "user_id": "u", "session_id": "s",
                            "product_id": p, "price_eur": 10.0, "device": "web", "region": "r"}
                           for i, p in enumerate(self.pids) for _ in range(60 - i)])
        return HistoryDump(catalog=cat, history_sessions=pd.DataFrame(), history_events=ev)

    def advance(self, now):
        reqs = []
        while self.t + 0.33 <= now:
            self.t += 0.33
            self.k += 1
            v = self.rng.choice(self.pids)
            reqs.append({"request_id": f"r{self.k}", "ts": iso(self.t), "user_id": f"u{self.k}",
                         "is_new_user": self.k % 4 == 0, "context": {"device": "web", "region": "r", "page": "home"},
                         "recent_views": [{"product_id": v, "ts": iso(self.t - 5)}]})
        return Tick(requests=reqs)

    def _want(self, req):
        return self.sub[req["recent_views"][0]["product_id"]]

    def expected_purchase(self, req, items):
        w = self._want(req)
        return sum((0.05 if self.sub[p] == w else 0.005) / (1 + k) for k, p in enumerate(items))

    def oracle(self, req):
        w = self._want(req)
        return [p for p in self.pids if self.sub[p] == w][:10]

    def judge(self, sub, received_at):
        return {"request_id": sub["request_id"], "ts": iso(received_at + 3), "shown": sub["items"],
                "served_by": "team", "fallback_reason": "", "latency_ms": 900.0,
                "clicked": "", "purchased": "", "revenue_eur": 0.0}


class OraclePolicy:
    name = "oracle"

    def __init__(self, state, dump):
        self.sim = StubSim()

    def recommend(self, req, state):
        return self.sim.oracle(req)


def _run(factory):
    return arbiter.run(factory, StubSim(), warmup_s=5, eval_s=60, start=T0)


def test_baseline_scores_zero_and_oracle_scores_hundred():
    base = _run(lambda s, d: arbiter.BestSellerPolicy(arbiter.best_sellers(d.history_events)))
    orc = _run(lambda s, d: OraclePolicy(s, d))
    assert base.overall.n > 100 and base.cold.n > 20
    assert base.as_dict()["score"] == 0.0
    assert orc.as_dict()["score"] == 100.0
    assert orc.invalid_before_finalize == 0 and orc.errors == 0
