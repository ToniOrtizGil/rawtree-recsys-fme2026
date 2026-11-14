import pickle
import random

from rtrec import live
from rtrec.contract import State
from rtrec.live import persist
from test_live_trends import T0, _ev, _prod


def _fed_state():
    s = State()
    live.init(s, [_prod(f"p{i}", f"s{i % 4}") for i in range(50)])
    rng = random.Random(0)
    rows = [_ev(T0 + i, f"p{rng.randrange(50)}", uid=f"u{i % 7}", sid=f"x{i % 7}") for i in range(3000)]
    live.on_events(rows, s)
    live.learner(s).flush(s)
    return s, rows


def test_snapshot_roundtrip_keeps_learning_and_hooks(tmp_path):
    s, rows = _fed_state()
    pickle.dumps(s.live)                                   # what A1's snapshotter does must work
    n = persist.save(s, tmp_path / "live.pkl")
    assert n > 0
    r = State()
    live.init(r, [_prod(f"p{i}", f"s{i % 4}") for i in range(50)])
    assert persist.load(r, tmp_path / "live.pkl")
    assert r.live["trend_rate"] == s.live["trend_rate"]
    assert r.users["u3"].views == s.users["u3"].views
    lr = live.learner(r)
    before = lr.stats["events"]
    live.on_events(rows[-10:], r)                          # already seen before the snapshot: dedupe survives
    live.on_events([_ev(T0 + 5000, "p1")], r)
    assert lr.stats["events"] == before + 1
    assert not persist.load(r, tmp_path / "missing.pkl")
