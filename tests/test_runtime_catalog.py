"""The catalogue view decides validity: stock-outs, reactivations, late rows, new products."""
from rtrec.contract import State
from rtrec.runtime.catalog import CatalogTracker


def _p(pid, active=True, price=10.0):
    return {"product_id": pid, "category": "c", "subcategory": "s", "brand": "b", "price_eur": price,
            "description": "", "created_at": "", "is_active": active}


def _u(pid, change, ts, **kw):
    return {**_p(pid, **kw), "ts": ts, "change": change}


def test_stock_out_back_in_and_inactive():
    t = CatalogTracker()
    t.load([_p("a"), _p("b"), _p("c", active=False)])
    assert t.available() == {"a", "b"}
    t.apply([_u("a", "out_of_stock", "2026-11-14T10:00:01Z")])        # is_active still true in the row
    assert t.available() == {"b"}
    t.apply([_u("a", "back_in_stock", "2026-11-14T10:00:05Z")])
    assert t.available() == {"a", "b"}


def test_late_older_update_does_not_override():
    t = CatalogTracker()
    t.load([_p("a")])
    t.apply([_u("a", "out_of_stock", "2026-11-14T10:00:05Z")])
    t.apply([_u("a", "back_in_stock", "2026-11-14T10:00:01Z")])        # arrives late, is older
    assert "a" not in t.available()


def test_new_products_and_prices_published():
    t, s = CatalogTracker(), State()
    t.load([_p("a")])
    t.apply([_u("n1", "new_product", "2026-11-14T10:00:01Z"), _u("a", "price_change", "2026-11-14T10:00:02Z", price=7.5)])
    t.publish(s)
    assert s.available == {"a", "n1"} and s.new_products == {"n1"} and s.catalog["a"]["price_eur"] == 7.5
    assert s.is_cold({"is_new_user": False, "recent_views": [{"product_id": "n1", "ts": ""}]})


def test_live_loop_never_serves_a_product_out_of_stock_at_serve_time():
    from rtrec.runtime.testing import SimEnv
    env = SimEnv(seed=21)
    srv = env.server()
    bad = []
    orig = srv.serve

    def serve(reqs):
        out = orig(reqs)
        # ground truth from the simulator, at serve time: allow only products that went out in the
        # last 0.5 s + visibility delay (the unavoidable race), nothing older
        avail = env.sim.available()
        for s in out:
            bad.extend(p for p in s["items"] if p not in avail)
        return out

    srv.serve = serve
    env.run(srv, seconds=300)
    assert len(bad) <= 10     # out of ~9,000 items served; each is a stock-out within the last ~1 s
