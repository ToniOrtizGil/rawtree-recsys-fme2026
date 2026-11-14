"""RawTreeStore's HTTP plumbing against the local emulator (NOT the live API: no key yet)."""
import pytest

from rtrec.runtime.clock import RealClock
from rtrec.runtime.policies import BaselinePolicy
from rtrec.runtime.serve import ServeConfig, Server
from rtrec.stores.fake import Faults, FakeStore, RateLimited
from rtrec.stores.fake_http import Emulator
from rtrec.stores.rawtree import QueryError, RawTreeStore, _request
from rtrec.stores.stub_sim import StubParams, StubSimulator


@pytest.fixture
def emu():
    clock = RealClock()
    sim = StubSimulator(seed=1, start=clock.now() - 0.01, params=StubParams(request_rate=10.0))
    store = FakeStore(sim, clock)
    e = Emulator(store, api_key="k")
    srv = e.serve()
    yield e, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_reads_are_normalised(emu):
    e, url = emu
    rt = RawTreeStore("team_00", "k", url)
    cat = rt.catalog()
    assert len(cat) == 400 and isinstance(cat[0]["is_active"], bool)
    import time
    time.sleep(1.5)
    reqs = rt.requests_since(0)
    assert reqs
    r = reqs[0]
    assert set(r) == {"request_id", "ts", "user_id", "is_new_user", "context", "recent_views"}
    assert set(r["context"]) == {"device", "region", "page"}
    assert rt.best_sellers()[:3] == e.store.best_sellers()[:3]


def test_bad_key_is_a_query_error(emu):
    _, url = emu
    with pytest.raises(QueryError):
        RawTreeStore("team_00", "wrong", url).catalog()


def test_429_becomes_rate_limited_with_retry_after(emu):
    e, url = emu
    e.store.faults = Faults(rate_limit=1.0)
    with pytest.raises(RateLimited) as ex:
        RawTreeStore("team_00", "k", url).insert_submissions([{"request_id": "x", "items": [], "model_version": ""}])
    assert ex.value.retry_after == pytest.approx(0.2)


def test_retried_insert_is_deduplicated_by_token(emu):
    e, url = emu
    rt = RawTreeStore("team_00", "k", url)
    rows = [{"request_id": "x", "items": ["a"] * 10, "model_version": "m"}]
    rt.insert_submissions(rows)
    rt.insert_submissions(rows)
    assert len(e.store.submissions) == 1


def test_request_parser_accepts_nested_or_string_json():
    a = _request({"request_id": "r", "ts": "t", "user_id": "u", "is_new_user": "true",
                  "context": '{"device":"ios","region":"X","page":"home"}',
                  "recent_views": '[{"product_id":"p1","ts":"t"}]'})
    assert a["is_new_user"] is True and a["context"]["device"] == "ios" and a["recent_views"][0]["product_id"] == "p1"
    b = _request({"request_id": "r", "ts": "t", "user_id": "u", "is_new_user": 0, "device": "web", "region": "Y",
                  "page": "product", "recent_views": [{"product_id": 5, "ts": "t"}]})
    assert b["context"] == {"device": "web", "region": "Y", "page": "product"} and b["recent_views"][0]["product_id"] == "5"


def test_threaded_server_over_http(emu, tmp_path):
    """The production code path: threads + RawTreeStore + HTTP, real time, 10 req/s for 8 s."""
    e, url = emu
    rt = RawTreeStore("team_00", "k", url)
    srv = Server(rt, BaselinePolicy(), config=ServeConfig(state_dir=str(tmp_path)))
    import time
    t_start = time.time()
    srv.run(duration=8)
    t_end = time.time() - 1.0   # requests in the last second may arrive after we stopped
    time.sleep(6)   # let the emulator emit feedback for the last requests
    rep = e.store.report(since=t_start, until=t_end)
    assert rep["requests"] > 30
    assert rep["reasons"].get("missing", 0) == 0 and rep["reasons"].get("late", 0) == 0
    assert rep["duplicate_answers"] == 0
    assert rep["latency_received_s"]["p99"] < 1.8
