"""SQL feature queries (rtrec/sql) and their pandas twins (rtrec/data/features.py) agree on the
synthetic dump. SQL runs in an embedded ClickHouse (chdb), not in RawTree: Dynamic-column
behaviour is not covered here."""
import numpy as np
import pandas as pd
import pytest

from rtrec.data import features as F
from rtrec.data.load import from_simulator, load_history
from rtrec.sql import names, render
from sim.dump import write_dump
from sim.params import SimConfig
from sim.shop import make_simulator

chdb = pytest.importorskip("chdb")
from rtrec.sql.local import LocalShop  # noqa: E402

SINCE, UNTIL = "2026-11-11T00:00:00Z", "2026-11-14T09:00:00Z"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    sim = make_simulator(9, SimConfig.small(history_days=3))
    d = tmp_path_factory.mktemp("dump")
    write_dump(sim.history(3), d, {"source": "synthetic"})
    tk = sim.advance(sim.stream_start + 900)
    upd, ev = pd.DataFrame(tk.catalog_updates), pd.DataFrame(tk.events)
    shop = LocalShop(d)
    shop.add_table("catalog_updates", upd)
    shop.add_table("events", ev)
    return sim, load_history(d), shop, upd, ev


def _same(a: pd.DataFrame, b: pd.DataFrame, keys):
    a = a.sort_values(keys).reset_index(drop=True)
    b = b[a.columns].sort_values(keys).reset_index(drop=True)
    assert len(a) == len(b)
    for c in a.columns:
        if a[c].dtype.kind == "f" or b[c].dtype.kind == "f":
            assert np.allclose(a[c].astype(float), b[c].astype(float), rtol=1e-6), c
        else:
            assert (a[c].astype(str).to_numpy() == b[c].astype(str).to_numpy()).all(), c


def test_every_sql_file_has_a_twin():
    for n in names():
        assert hasattr(F, n), n


def test_current_catalog(env):
    _, h, shop, upd, _ = env
    _same(shop.query_df(render("current_catalog")), F.current_catalog(h.catalog, upd), ["product_id"])


def test_popularity_and_affinity(env):
    _, h, shop, _, _ = env
    _same(shop.query_df(render("popularity_by_context", table="history_events", since=SINCE, until=UNTIL)),
          F.popularity_by_context(h.events, SINCE, UNTIL), ["product_id", "device", "region"])
    _same(shop.query_df(render("user_affinity", table="history_events", since=SINCE, until=UNTIL)),
          F.user_affinity(h.events, h.catalog, SINCE, UNTIL), ["user_id", "subcategory"])


def test_coviews(env):
    _, h, shop, _, _ = env
    a = shop.query_df(render("coviews", table="history_events", since=SINCE, until=UNTIL, k=1000))
    _same(a, F.coviews(h.events, SINCE, UNTIL, k=1000), ["product_id", "other_id"])


def test_position_tables(env):
    _, h, shop, _, _ = env
    _same(shop.query_df(render("position_bias", since=SINCE, until=UNTIL)),
          F.position_bias(h.sessions, SINCE, UNTIL), ["slot"])
    _same(shop.query_df(render("product_slot_stats", since=SINCE, until=UNTIL)),
          F.product_slot_stats(h.sessions, SINCE, UNTIL), ["product_id", "slot"])


def test_trend_lift(env):
    sim, h, shop, upd, ev = env
    now = "2026-11-14T09:15:00Z"
    a = shop.query_df(render("trend_lift", table="events", now=now, recent_min=5, base_min=10))
    _same(a, F.trend_lift(ev, h.catalog, upd, now, 5, 10), ["subcategory"])


def test_position_fit_recovers_hidden_curve():
    """On a bigger synthetic log, the alternating fit recovers the simulator's examination curve
    (which the raw clicks-per-slot curve overstates, because best sellers sit on top)."""
    sim = make_simulator(1, SimConfig.small(history_days=8, history_requests_per_day=4000))
    h = from_simulator(sim)
    st = F.product_slot_stats(h.sessions, "2026-01-01", "2027-01-01")
    e, _ = F.fit_position_bias(st)
    true = np.asarray(sim.cfg.hidden.examine)
    true = true / true[0]
    assert np.abs(e.to_numpy()[:5] - true[:5]).max() < 0.15

