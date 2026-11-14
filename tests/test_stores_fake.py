"""FakeStore must reproduce the organisers' timing rules, or every reliability number we quote is wrong."""
from rtrec.runtime.clock import SimClock, parse_ts
from rtrec.stores.fake import Faults, FakeStore, StoreError
from rtrec.stores.stub_sim import StubParams, StubSimulator


def _env(**kw):
    clock = SimClock()
    sim = StubSimulator(seed=3, start=clock.now(), params=StubParams(request_rate=5.0))
    return clock, sim, FakeStore(sim, clock, **kw)


def _valid_items(sim):
    return sorted(sim.available())[:10]


def test_request_visible_only_after_delay():
    clock, sim, store = _env(visibility_delay=0.4)
    clock.advance(5)
    rows = store.requests_since(clock.now() - 10)
    assert rows
    for r in rows:
        assert parse_ts(r["ts"]) + 0.4 <= clock.now()
    assert len(store.requests) > len(rows) - 1   # some exist that are not yet visible (or equal)


def test_on_time_late_missing_and_duplicates():
    clock, sim, store = _env(insert_latency=0.1, feedback_delay=5.0)
    clock.advance(3)
    reqs = store.requests_since(clock.now() - 10)
    assert len(reqs) >= 3
    on_time, dup, late = reqs[-1], reqs[-2], reqs[-3]   # newest: can still be on time
    store.insert_submissions([{"request_id": on_time["request_id"], "items": _valid_items(sim), "model_version": "t"},
                              {"request_id": dup["request_id"], "items": _valid_items(sim), "model_version": "t"}])
    store.insert_submissions([{"request_id": dup["request_id"], "items": _valid_items(sim)[::-1], "model_version": "t"}])
    clock.advance(4)
    store.insert_submissions([{"request_id": late["request_id"], "items": _valid_items(sim), "model_version": "t"}])
    clock.advance(10)
    rep = store.report()
    fb = {f["request_id"]: f for f in store.feedback_since(0)}
    assert fb[on_time["request_id"]]["fallback_reason"] == ""
    assert fb[late["request_id"]]["fallback_reason"] == "late"
    assert rep["duplicate_answers"] == 1 and rep["conflicting_answers"] == 1
    assert rep["reasons"]["missing"] == rep["requests"] - 3
    # received at an organiser poll (multiples of 0.5 s after start), never before landing
    rec = store.requests[on_time["request_id"]]
    assert rec.received_at >= rec.first_landed
    assert abs(((rec.received_at - store._t0) / 0.5) - round((rec.received_at - store._t0) / 0.5)) < 1e-6


def test_invalid_when_out_of_stock_or_wrong_length():
    clock, sim, store = _env()
    clock.advance(2)
    reqs = store.requests_since(0)
    store.insert_submissions([{"request_id": reqs[0]["request_id"], "items": _valid_items(sim)[:9], "model_version": ""}])
    clock.advance(10)
    fb = {f["request_id"]: f for f in store.feedback_since(0)}
    assert fb[reqs[0]["request_id"]]["fallback_reason"] == "invalid"


def test_dedup_token_makes_retry_a_noop_and_ambiguous_failure_writes():
    clock, sim, store = _env(faults=Faults(ambiguous_insert=1.0))
    clock.advance(2)
    r = store.requests_since(0)[0]
    rows = [{"request_id": r["request_id"], "items": _valid_items(sim), "model_version": ""}]
    for _ in range(3):
        try:
            store.insert_submissions(rows)
        except StoreError:
            pass
    assert len(store.submissions) == 1


def test_feedback_after_deadline_plus_delay():
    clock, sim, store = _env(feedback_delay=2.0, visibility_delay=0.0)
    clock.advance(1)
    r = store.requests_since(0)[0]
    t = parse_ts(r["ts"])
    clock.advance(t + 3 + 1.5 - clock.now())
    assert not [f for f in store.feedback_since(0) if f["request_id"] == r["request_id"]]
    clock.advance(1.0)
    assert [f for f in store.feedback_since(0) if f["request_id"] == r["request_id"]]
