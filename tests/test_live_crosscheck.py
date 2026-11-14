"""Cross-check of catalogue semantics: A4's live tracker vs A1's runtime tracker vs ground truth.

Ground truth = every update applied in true time order, with "out_of_stock" taken last among rows
sharing the same ts (when the stream cannot tell us the order, hiding the product is the safe
answer: an out-of-stock id invalidates the whole carousel, a hidden one only costs one slot).
"""
import random

import pytest

from rtrec.contract import State
from rtrec.live.catalog import CatalogTracker as A4Tracker
from rtrec.runtime.catalog import CatalogTracker as A1Tracker

PRIO = {"back_in_stock": 0, "new_product": 1, "price_change": 2, "out_of_stock": 3}


def prod(pid, price=10.0, active=True):
    return {"product_id": pid, "category": "c", "subcategory": "s", "brand": "b", "price_eur": price,
            "description": "", "created_at": "", "is_active": active}


def upd(pid, t, change, **kw):
    return {**prod(pid, **kw), "ts": f"2026-11-15T09:{t // 60:02d}:{t % 60:02d}Z", "change": change}


BASE = [prod(f"p{i}") for i in range(30)]


def truth(rows):
    stock = {p["product_id"]: True for p in BASE}
    active = {p["product_id"]: True for p in BASE}
    for r in sorted(rows, key=lambda r: (r["ts"], PRIO[r["change"]])):
        pid = r["product_id"]
        active[pid] = r["is_active"]
        if r["change"] == "out_of_stock":
            stock[pid] = False
        elif r["change"] in ("back_in_stock", "new_product"):
            stock[pid] = True
    return frozenset(p for p in stock if stock[p] and active[p])


def a4(batches):
    s = State()
    tr = A4Tracker()
    tr.init(s, BASE)
    for b in batches:
        tr.apply(b, s)
    return s.available


def a1(batches):
    s = State()
    tr = A1Tracker()
    tr.load(BASE)
    for b in batches:
        tr.apply(b)
    tr.publish(s)
    return s.available


def stream(seed, price_changes, ties, n=1500):
    rng = random.Random(seed)
    rows, used = [], set()
    for _ in range(n):
        pid = f"p{rng.randrange(30)}"
        t = rng.randrange(3600)
        if not ties:
            while (pid, t) in used:
                t = rng.randrange(3600)
        used.add((pid, t))
        kinds = ["out_of_stock", "back_in_stock"] + (["price_change"] * 2 if price_changes else [])
        rows.append(upd(pid, t, rng.choice(kinds), price=float(rng.randrange(1, 9))))
    arrival = rows + rng.sample(rows, 200)     # duplicates from the cursor overlap
    rng.shuffle(arrival)                       # out of order (late rows)
    return rows, [arrival[i:i + 50] for i in range(0, len(arrival), 50)]


@pytest.mark.parametrize("seed", range(5))
def test_both_trackers_agree_on_stock_only_streams_without_ties(seed):
    rows, batches = stream(seed, price_changes=False, ties=False)
    assert a4(batches) == truth(rows) == a1(batches)


@pytest.mark.parametrize("seed", range(5))
def test_a4_tracker_matches_truth_with_price_changes_and_ties(seed):
    rows, batches = stream(seed, price_changes=True, ties=True)
    assert a4(batches) == truth(rows)


def test_late_stock_row_behind_newer_price_change():
    # truth: OOS @0:10, back @0:20, price @0:30  -> in stock. The back_in_stock row arrives last.
    rows = [upd("p1", 10, "out_of_stock"), upd("p1", 30, "price_change"), upd("p1", 20, "back_in_stock")]
    batches = [[r] for r in rows]
    assert "p1" in truth(rows) and "p1" in a4(batches)


def test_same_ts_tie_hides_the_product():
    rows = [upd("p1", 10, "out_of_stock"), upd("p1", 10, "back_in_stock")]
    for order in (rows, rows[::-1]):
        assert "p1" not in a4([[r] for r in order])


@pytest.mark.xfail(reason="A1 tracker: one clock per product, last arrival wins ties (reported to master)",
                   strict=False)
def test_a1_tracker_matches_truth_with_price_changes_and_ties():
    for seed in range(5):
        rows, batches = stream(seed, price_changes=True, ties=True)
        assert a1(batches) == truth(rows)
