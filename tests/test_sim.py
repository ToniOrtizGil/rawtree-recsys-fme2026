"""The synthetic shop: shapes match rtrec.schemas, rates are plausible, the hidden model behaves
(oracle >= any list, propensities exact), and the stream does not depend on how we poll it."""
import numpy as np
import pandas as pd
import pytest

from rtrec.schemas import CatalogUpdate, Event, HistorySession, Product, Request
from sim.dump import write_dump
from sim.params import SimConfig
from sim.shop import make_simulator


@pytest.fixture(scope="module")
def sim():
    s = make_simulator(3, SimConfig.small())
    s.history(4)
    return s


@pytest.fixture(scope="module")
def tick(sim):
    return sim.advance(sim.stream_start + 120)


def test_history_shapes(sim):
    h = sim.history(4)
    assert set(h.catalog.columns) == set(Product.__annotations__)
    assert set(h.history_sessions.columns) == set(HistorySession.__annotations__)
    assert set(h.history_events.columns) == set(Event.__annotations__)
    assert h.catalog.product_id.is_unique
    assert h.catalog.subcategory.nunique() == 90 and h.catalog.category.nunique() == 10
    hs = h.history_sessions
    assert (hs.shown.map(len) == 10).all() and (hs.shown.map(lambda x: len(set(x))) == 10).all()
    assert (hs.propensity.map(lambda p: all(0 < x <= 1 for x in p))).all()
    # purchase implies click on the same product; at most one click per impression
    bought = hs[hs.purchased.notna()]
    assert len(bought) > 0 and (bought.purchased == bought.clicked).all()
    assert hs.ts.is_monotonic_increasing and h.history_events.ts.is_monotonic_increasing
    assert set(h.history_events.event_type) == {"view", "add_to_cart", "purchase"}


def test_history_window(sim):
    h2 = sim.history(2)
    cut = pd.Timestamp(sim.stream_start - 2 * 86400, unit="s", tz="UTC")
    assert pd.to_datetime(h2.history_sessions.ts).min() >= cut
    with pytest.raises(ValueError):
        sim.history(5)


def test_live_shapes_and_rates(sim, tick):
    assert 250 < len(tick.requests) < 480                      # ~3/s
    assert 30 * 120 < len(tick.events) < 80 * 120               # ~50/s
    assert set(tick.requests[0]) == set(Request.__annotations__)
    assert set(tick.events[0]) == set(Event.__annotations__)
    assert tick.catalog_updates and set(tick.catalog_updates[0]) == set(CatalogUpdate.__annotations__)
    assert {u["change"] for u in tick.catalog_updates} >= {"price_change", "out_of_stock"}
    ts = [r["ts"] for r in tick.requests]
    assert ts == sorted(ts) and ts[0] >= "2026-11-14T09:00:00"
    new = np.mean([r["is_new_user"] for r in tick.requests])
    assert 0.25 < new < 0.65
    for r in tick.requests[:50]:
        assert all(v["ts"] <= r["ts"] for v in r["recent_views"])


def test_oracle_beats_everything(sim, tick):
    for r in tick.requests[:30]:
        orc = sim.oracle(r)
        assert len(set(orc)) == 10 and sim.is_valid(orc)
        best = sim.expected_purchase(r, orc)
        base = [sim.world.pid[i] for i in sim.baseline_items()]
        assert best >= sim.expected_purchase(r, base) - 1e-12
        assert best >= sim.expected_purchase(r, orc[::-1]) - 1e-12   # order matters
        assert 0 <= best < 1


def test_judge_rules(sim, tick):
    r1, r2, r3 = tick.requests[-3:]
    t1 = sim._live[r1["request_id"]][2]
    orc = sim.oracle(r1)
    fb = sim.judge({"request_id": r1["request_id"], "items": orc, "model_version": "t"}, t1 + 1.0)
    assert fb["served_by"] == "team" and fb["shown"] == orc and fb["fallback_reason"] == ""
    assert fb["latency_ms"] == pytest.approx(1000.0, abs=1)
    again = sim.judge({"request_id": r1["request_id"], "items": orc[::-1], "model_version": "t"}, t1 + 1.5)
    assert again == fb                                                     # only one answer counts
    late = sim.judge({"request_id": r2["request_id"], "items": orc, "model_version": "t"},
                     sim._live[r2["request_id"]][2] + 3.2)
    assert late["fallback_reason"] == "late" and late["served_by"] == "fallback"
    bad = sim.judge({"request_id": r3["request_id"], "items": orc[:9] + ["p_nope"], "model_version": "t"},
                    sim._live[r3["request_id"]][2] + 0.5)
    assert bad["fallback_reason"] == "invalid"
    rep = sim.report()
    assert rep["n"] == 3 and rep["oracle"] >= rep["baseline"]


def test_stock_changes_are_respected(sim, tick):
    oos = [u for u in tick.catalog_updates if u["change"] == "out_of_stock"]
    assert oos and not oos[0]["is_active"]
    i = sim.world.pid_index[oos[-1]["product_id"]]
    if not sim.world.products.active[i]:
        r = tick.requests[0]
        items = [oos[-1]["product_id"]] + [p for p in sim.oracle(r) if p != oos[-1]["product_id"]][:9]
        assert not sim.is_valid(items)


def test_propensities_match_logging_policy():
    s = make_simulator(5, SimConfig.small())
    w = s.carousel.weights(np.arange(200.0), np.ones(200, bool))
    shown, prop = s.carousel.sample(w, 20000, np.random.default_rng(0))
    # slot-0 frequencies equal slot-0 propensities
    freq = np.bincount(shown[:, 0], minlength=200) / len(shown)
    top = np.argsort(-freq)[:5]
    assert np.allclose(freq[top], w[top] / w.sum(), rtol=0.08)
    assert np.allclose(prop[:, 0], w[shown[:, 0]] / w.sum())


def test_chunking_invariance():
    a = make_simulator(11, SimConfig.small())
    b = make_simulator(11, SimConfig.small())
    t0 = a.stream_start
    ta = a.advance(t0 + 40)
    rb, eb = [], []
    for k in range(1, 81):
        tk = b.advance(t0 + k * 0.5)
        rb += tk.requests
        eb += tk.events
    assert [r["request_id"] for r in ta.requests] == [r["request_id"] for r in rb]
    assert len(ta.events) == len(eb)


def test_cold_and_new_products():
    s = make_simulator(2, SimConfig.small(new_products_per_hour=2000))
    tk = s.advance(s.stream_start + 300)
    newp = {u["product_id"] for u in tk.catalog_updates if u["change"] == "new_product"}
    assert newp
    viewed_new = {e["product_id"] for e in tk.events if e["product_id"] in newp}
    assert viewed_new   # new products get organic interactions within minutes
    assert any(s.is_cold(r) for r in tk.requests)


def test_dump_layout(tmp_path):
    s = make_simulator(1, SimConfig.small(history_days=2))
    write_dump(s.history(2), tmp_path, {"source": "synthetic"})
    assert (tmp_path / "catalog.parquet").exists()
    days = sorted(p.name for p in (tmp_path / "history_sessions").glob("*.parquet"))
    assert len(days) == 3          # 2 x 24 h ending Saturday 09:00 UTC span 3 calendar days
    df = pd.read_parquet(tmp_path / "history_sessions" / days[0])
    assert len(df.iloc[0]["shown"]) == 10 and "device" in df.iloc[0]["context"]


def test_alt_preset_is_a_different_shop():
    a = make_simulator(3, "small")
    b = make_simulator(3, SimConfig.small(hidden=SimConfig.alt().hidden))
    assert a.cfg.hidden != b.cfg.hidden and b.start_ts == b.stream_start
    assert make_simulator(1, "alt").cfg.hidden.examine == SimConfig.alt().hidden.examine


def test_expected_purchase_and_oracle_are_pure(sim, tick):
    r = tick.requests[5]
    ledger = {u: dict(d) for u, d in sim.model.ledger.items()}
    before = sim.world.products.active.copy(), sim.now, len(sim._feedback)
    for _ in range(3):
        o = sim.oracle(r)
        assert sim.oracle(r) == o
        assert sim.expected_purchase(r, o) == sim.expected_purchase(r, o)
    assert sim.model.ledger == ledger
    assert (sim.world.products.active == before[0]).all() and sim.now == before[1] and len(sim._feedback) == before[2]
