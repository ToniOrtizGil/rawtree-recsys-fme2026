from rtrec import live
from rtrec.contract import State
from rtrec.validate import finalize, is_valid


def _prod(pid, sub="s1", price=10.0, active=True, brand="b"):
    return {"product_id": pid, "category": "c1", "subcategory": sub, "brand": brand,
            "price_eur": price, "description": "red cotton shirt", "created_at": "2026-01-01T00:00:00Z",
            "is_active": active}


def _upd(pid, ts, change, **kw):
    r = _prod(pid, **kw)
    r.update(ts=ts, change=change)
    return r


def _state(n=20):
    s = State()
    live.init(s, [_prod(f"p{i}") for i in range(n)] + [_prod("dead", active=False)])
    return s


def test_init_available_excludes_inactive():
    s = _state()
    assert "p0" in s.available and "dead" not in s.available
    assert len(s.available) == 20 and s.new_products == frozenset()


def test_out_of_stock_back_and_out_of_order():
    s = _state()
    live.on_catalog_updates([_upd("p1", "2026-11-15T09:00:05Z", "out_of_stock")], s)
    assert "p1" not in s.available
    # an older back_in_stock arriving late must NOT resurrect it
    live.on_catalog_updates([_upd("p1", "2026-11-15T09:00:01Z", "back_in_stock")], s)
    assert "p1" not in s.available
    live.on_catalog_updates([_upd("p1", "2026-11-15T09:00:09Z", "back_in_stock")], s)
    assert "p1" in s.available
    # same ts tie: out_of_stock wins whatever the order
    live.on_catalog_updates([_upd("p2", "2026-11-15T09:01:00Z", "out_of_stock"),
                             _upd("p2", "2026-11-15T09:01:00Z", "back_in_stock")], s)
    assert "p2" not in s.available


def test_price_change_keeps_stock_and_records_history():
    s = _state()
    live.on_catalog_updates([_upd("p3", "2026-11-15T09:00:00Z", "out_of_stock")], s)
    live.on_catalog_updates([_upd("p3", "2026-11-15T09:00:02Z", "price_change", price=7.0)], s)
    assert "p3" not in s.available             # price change does not put it back in stock
    pi = s.live["price"]["p3"]
    assert pi.price == 7.0 and pi.base_price == 10.0 and abs(pi.ratio_to_base - 0.7) < 1e-9
    assert s.catalog["p3"]["price_eur"] == 7.0


def test_new_product_and_inactive_update_and_duplicates():
    s = _state()
    rows = [_upd("n1", "2026-11-15T09:00:00Z", "new_product", sub="s2")]
    live.on_catalog_updates(rows, s)
    live.on_catalog_updates(rows, s)          # duplicate is harmless
    assert "n1" in s.available and s.new_products == {"n1"}
    assert s.is_cold({"is_new_user": False, "recent_views": [{"product_id": "n1", "ts": "x"}]})
    live.on_catalog_updates([_upd("p4", "2026-11-15T09:00:03Z", "price_change", active=False)], s)
    assert "p4" not in s.available


def test_finalize_never_returns_out_of_stock():
    s = _state()
    live.on_catalog_updates([_upd(f"p{i}", "2026-11-15T09:00:00Z", "out_of_stock") for i in range(5)], s)
    out = finalize([f"p{i}" for i in range(12)], s)
    assert is_valid(out, s) and not set(out) & {f"p{i}" for i in range(5)}


def test_burst_is_cheap():
    import time
    s = State()
    live.init(s, [_prod(f"p{i}", sub=f"s{i % 90}") for i in range(5000)])
    rows = [_upd(f"p{i % 5000}", f"2026-11-15T09:{i // 60 % 60:02d}:{i % 60:02d}Z",
                 "price_change", price=5 + i % 7) for i in range(5000)]
    t0 = time.perf_counter()
    live.on_catalog_updates(rows, s)
    assert time.perf_counter() - t0 < 1.0
