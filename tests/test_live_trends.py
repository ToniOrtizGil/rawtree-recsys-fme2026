import random
import time

from rtrec import live
from rtrec.contract import State
from rtrec.live._util import DecayedCounter, to_epoch


def _prod(pid, sub):
    return {"product_id": pid, "category": "c", "subcategory": sub, "brand": "b", "price_eur": 10.0,
            "description": "", "created_at": "2026-01-01T00:00:00Z", "is_active": True}


def _ev(t, pid, et="view", uid="u1", sid="s1", device="ios", region="Catalunya"):
    return {"ts": t, "event_type": et, "user_id": uid, "session_id": sid, "product_id": pid,
            "price_eur": 10.0, "device": device, "region": region}


T0 = to_epoch("2026-11-15T09:00:00Z")


def test_decayed_counter_out_of_order_is_exact():
    a, b = DecayedCounter(60), DecayedCounter(60)
    for t in (0, 10, 50, 100):
        a.add("k", 1.0, T0 + t)
    for t in (100, 10, 50, 0):
        b.add("k", 1.0, T0 + t)
    assert abs(a.value("k", T0 + 200) - b.value("k", T0 + 200)) < 1e-9
    assert abs(a.value("k", T0 + 100) - (1 + 2 ** (-50 / 60) + 2 ** (-90 / 60) + 2 ** (-100 / 60))) < 1e-9


def test_rate_is_unbiased_during_warmup():
    c = DecayedCounter(900)
    for i in range(600):                       # 2 events/s for 5 minutes
        c.add("k", 1.0, T0 + i * 0.5)
    assert abs(c.rate("k", T0 + 300) - 2.0) < 0.1


def test_trend_lift_detects_surge_and_dedupes():
    s = State()
    live.init(s, [_prod(f"p{i}", f"s{i % 3}") for i in range(30)])
    rng = random.Random(0)
    rows = []
    for i in range(20_000):                    # 2 h of background ~2.8 ev/s, uniform
        t = T0 + i * 0.36
        rows.append(_ev(t, f"p{rng.randrange(30)}", uid=f"u{i % 500}", sid=f"s{i % 500}"))
    for i in range(400):                       # last 5 min: p7 goes viral, with purchases
        t = T0 + 7200 + i * 0.75
        rows.append(_ev(t, "p7", et="purchase" if i % 5 == 0 else "view", uid=f"v{i}", sid=f"v{i}"))
    live.on_events(rows, s)
    live.on_events(rows[-1000:], s)            # cursor overlap: duplicates ignored
    lr = live.learner(s)
    assert lr.stats["dup_events"] == 1000
    lr.flush(s)
    lift = s.live["trend_lift"]
    assert lift["p7"] > 3.0
    others = sorted(v for k, v in lift.items() if k != "p7")
    assert others[len(others) // 2] < 1.3
    assert s.live["trend_top"][0] == "p7" and "p7" in s.live["trend_rising"][:3]
    assert s.live["ctx_top"][("ios", "Catalunya")][0] == "p7"
    assert s.live["subcat_lift"]["s1"] > s.live["subcat_lift"]["s0"]   # p7 is in s1


def test_profiles_and_coviews():
    s = State()
    live.init(s, [_prod(f"p{i}", "s") for i in range(10)])
    live.on_events([_ev(T0 + 5, "p2", uid="u9", sid="x"), _ev(T0 + 1, "p1", uid="u9", sid="x"),
                    _ev(T0 + 9, "p3", et="add_to_cart", uid="u9", sid="x"),
                    _ev(T0 + 12, "p3", uid="u9", sid="x")], s)
    live.learner(s).flush(s)
    prof = s.users["u9"]
    assert [p for _, p in prof.views] == ["p1", "p2", "p3"] or [p for _, p in prof.views] == ["p2", "p1", "p3"]
    assert prof.views == sorted(prof.views)
    assert [p for _, p in prof.carts] == ["p3"]
    nb = dict(s.live["coview"]["p2"])
    assert "p1" in nb and "p3" in nb


def test_event_throughput():
    s = State()
    live.init(s, [_prod(f"p{i}", f"s{i % 90}") for i in range(5000)])
    rng = random.Random(1)
    rows = [_ev(T0 + i * 0.02, f"p{rng.randrange(5000)}", et=rng.choice(["view"] * 8 + ["add_to_cart", "purchase"]),
                uid=f"u{rng.randrange(20000)}", sid=f"s{rng.randrange(20000)}",
                region=rng.choice(["A", "B", "C"])) for i in range(20_000)]
    t0 = time.perf_counter()
    for i in range(0, len(rows), 500):
        live.on_events(rows[i:i + 500], s)
    dt = time.perf_counter() - t0
    assert dt < 10.0, dt                       # >2,000 rows/s; nominal need is ~50/s


def test_live_features_neutral_for_unknown_product():
    from rtrec.live.features import LIVE_FEATURES, live_features
    s = State()
    live.init(s, [_prod("p1", "s")])
    f = live_features({"request_id": "r", "recent_views": [], "context": {}}, "zzz", s)
    assert set(f) == set(LIVE_FEATURES)
    assert f["trend_lift_log"] == 0.0 and f["is_new_product"] == 0.0 and f["price_ratio_to_base"] == 1.0
