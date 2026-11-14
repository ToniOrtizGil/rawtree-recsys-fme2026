"""Exact view of the live catalogue: which products we may recommend *right now*.

Why it matters: one inactive or out-of-stock id invalidates the whole answer. The current catalogue
is shop.catalog with the latest shop.catalog_updates rows applied on top.

Semantics are A4's (master decision, 14-nov): rtrec.live.catalog.CatalogTracker(owner=True) keeps two
clocks per product (product fields vs stock), so a late stock row behind a newer price change still
counts, and on a same-ts tie the safest change wins (back_in_stock < new_product < price_change <
out_of_stock). A cross-check against ground truth lives in tests/test_live_crosscheck.py.

What this wrapper adds is the runtime's concurrency rule: the tracker writes into a private State,
and publish() swaps *new* objects into the shared State (catalog dict, available and new_products
frozensets). Assignments are atomic, so a policy never sees a half-applied catalogue. The serving
thread is still the only caller (catalogue first, then the batch).
"""
from __future__ import annotations

from rtrec.contract import State
from rtrec.live.catalog import CatalogTracker as _LiveTracker
from rtrec.schemas import CatalogUpdate, Product


class CatalogTracker:
    def __init__(self) -> None:
        self._state = State()
        self._t = _LiveTracker(owner=True)
        self._dirty = True

    def load(self, rows: list[Product]) -> None:
        """shop.catalog. Products already updated (restored from a snapshot) keep their newer row."""
        fresh = [r for r in rows if self._t.prods.get(str(r["product_id"])) is None
                 or self._t.prods[str(r["product_id"])].row_ts == float("-inf")]
        self._t.init(self._state, fresh)
        self._dirty = True

    def apply(self, updates: list[CatalogUpdate]) -> int:
        """Apply updates (any order, duplicates allowed); returns how many changed something."""
        n = self._t.apply(updates, self._state)
        self._dirty = self._dirty or bool(n)
        return n

    def available(self) -> frozenset[str]:
        return self._state.available

    @property
    def products(self) -> dict[str, Product]:
        return self._state.catalog

    def publish(self, state: State) -> None:
        """Swap fresh snapshots into the shared State (atomic reference assignments)."""
        if not self._dirty and state.available is self._state.available:
            return
        state.catalog = dict(self._state.catalog)
        state.available = self._state.available
        state.new_products = self._state.new_products
        self._dirty = False
