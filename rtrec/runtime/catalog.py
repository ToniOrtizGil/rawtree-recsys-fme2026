"""Exact view of the live catalogue: which products we may recommend *right now*.

Why it matters: one inactive or out-of-stock id invalidates the whole answer. The current catalogue
is shop.catalog with the latest shop.catalog_updates row of each product applied on top.

We are deliberately conservative: a product is available only if its latest row says is_active
AND its latest stock change is not out_of_stock (we do not assume a stock-out flips is_active).
Updates are applied in ts order; an older update arriving late never overrides a newer one.
State fields are replaced, never mutated in place, so a policy reading them mid-update sees
either the old or the new snapshot, never a half-applied one.
"""
from __future__ import annotations

from rtrec.contract import State
from rtrec.schemas import CatalogUpdate, Product

from .clock import parse_ts


class CatalogTracker:
    def __init__(self) -> None:
        self.products: dict[str, Product] = {}
        self.in_stock: dict[str, bool] = {}
        self.last_ts: dict[str, float] = {}       # ts of the latest update applied per product
        self.new: set[str] = set()                # new_product during the event

    def load(self, rows: list[Product]) -> None:
        for r in rows:
            pid = str(r["product_id"])
            if pid in self.last_ts:
                continue   # restored from a snapshot with a newer update applied: keep that
            self.products[pid] = _clean(r)
            self.in_stock.setdefault(pid, True)

    def apply(self, updates: list[CatalogUpdate]) -> int:
        """Apply updates (any order); returns how many changed something."""
        n = 0
        for u in sorted(updates, key=lambda u: parse_ts(u.get("ts"))):
            pid = str(u["product_id"])
            t = parse_ts(u.get("ts"))
            if t < self.last_ts.get(pid, float("-inf")):
                continue
            self.last_ts[pid] = t
            change = u.get("change")
            prev = self.products.get(pid)
            prod = _clean(u)
            if prev is not None:
                # an update row is "the full product after the change"; keep old fields if any is missing
                prod = {**prev, **{k: v for k, v in prod.items() if v is not None}}
            self.products[pid] = prod
            if change == "out_of_stock":
                self.in_stock[pid] = False
            elif change in ("back_in_stock", "new_product"):
                self.in_stock[pid] = True
            else:
                self.in_stock.setdefault(pid, True)
            if change == "new_product":
                self.new.add(pid)
            n += 1
        return n

    def available(self) -> frozenset[str]:
        return frozenset(p for p, prod in self.products.items()
                         if _truthy(prod.get("is_active", True)) and self.in_stock.get(p, True))

    def publish(self, state: State) -> None:
        """Swap fresh snapshots into the State (atomic reference assignments)."""
        state.catalog = dict(self.products)
        state.available = self.available()
        state.new_products = frozenset(self.new)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes")
    return bool(v)


def _clean(r: dict) -> Product:
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return Product(
        product_id=str(r.get("product_id")), category=_s(r.get("category")), subcategory=_s(r.get("subcategory")),
        brand=_s(r.get("brand")), price_eur=num(r.get("price_eur")), description=_s(r.get("description")),
        created_at=_s(r.get("created_at")), is_active=_truthy(r.get("is_active", True)))


def _s(x):
    return None if x is None else str(x)
