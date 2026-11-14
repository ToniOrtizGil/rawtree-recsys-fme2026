"""Catalogue, price and stock tracker: keeps `state.catalog`, `state.available`, `state.new_products` exact.

Why it matters: one out-of-stock or inactive id invalidates a whole answer (fallback, lost reliability
and conversion). So the tracker is conservative:

- the latest update *by its own ts* wins per product (out-of-order rows never resurrect old state);
- on a tie at the same ts, an `out_of_stock` beats `back_in_stock` (when unsure, hide the product);
- stock and activity are tracked separately: available = is_active AND in_stock;
- duplicates (store cursor overlap) are idempotent.

It also records price history so models can use "price just dropped" (`state.live["price"]`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rtrec.contract import State

from ._util import to_epoch

# priority for tie-breaking rows with the same ts: higher wins (safety first)
_PRIO = {"back_in_stock": 0, "new_product": 1, "price_change": 2, "out_of_stock": 3}


@dataclass
class PriceInfo:
    price: float            # current price
    base_price: float       # price in shop.catalog at start (or at creation for new products)
    prev_price: float       # price before the latest change
    changed_at: float       # epoch of latest price change (0 if never)

    @property
    def ratio_to_base(self) -> float:
        return self.price / self.base_price if self.base_price > 0 else 1.0

    @property
    def last_change_ratio(self) -> float:
        return self.price / self.prev_price if self.prev_price > 0 else 1.0


@dataclass
class _ProdState:
    row_ts: float = float("-inf")     # ts of the row that set the current product fields
    row_prio: int = -1
    stock_ts: float = float("-inf")   # ts of the latest stock-relevant change
    stock_prio: int = -1
    in_stock: bool = True


@dataclass
class CatalogTracker:
    prods: dict[str, _ProdState] = field(default_factory=dict)
    new_at: dict[str, float] = field(default_factory=dict)        # new product -> creation ts
    oos_at: dict[str, float] = field(default_factory=dict)        # currently out of stock -> since
    back_at: dict[str, float] = field(default_factory=dict)       # last back-in-stock ts
    n_rows: int = 0
    n_stale: int = 0      # rows ignored because a newer row was already applied

    # ------------------------------------------------------------------ init
    def init(self, state: State, products) -> None:
        """Load `shop.catalog` (list of Product dicts or a DataFrame). In stock == is_active at start."""
        if hasattr(products, "to_dict"):
            products = products.to_dict("records")
        price: dict[str, PriceInfo] = state.live.setdefault("price", {})
        for p in products:
            pid = str(p["product_id"])
            row = dict(p)
            row["product_id"] = pid
            row["is_active"] = _as_bool(row.get("is_active", True))
            row["price_eur"] = float(row.get("price_eur") or 0.0)
            state.catalog[pid] = row
            self.prods.setdefault(pid, _ProdState())
            pr = row["price_eur"]
            price.setdefault(pid, PriceInfo(pr, pr, pr, 0.0))
        self._publish(state)

    # ------------------------------------------------------------------ stream
    def apply(self, rows, state: State) -> int:
        """Apply shop.catalog_updates rows. Returns how many changed something."""
        price: dict[str, PriceInfo] = state.live.setdefault("price", {})
        changed = 0
        for r in rows:
            self.n_rows += 1
            pid = str(r["product_id"])
            ch = r.get("change") or ""
            t = to_epoch(r["ts"])
            prio = _PRIO.get(ch, 1)
            ps = self.prods.get(pid)
            if ps is None:
                ps = self.prods[pid] = _ProdState()
            touched = False

            # product fields (price, is_active, ...): latest row wins
            if (t, prio) > (ps.row_ts, ps.row_prio):
                old = state.catalog.get(pid)
                row = {k: v for k, v in r.items() if k not in ("ts", "change")}
                row["product_id"] = pid
                row["is_active"] = _as_bool(row.get("is_active", True))
                row["price_eur"] = float(row.get("price_eur") or 0.0)
                state.catalog[pid] = row
                ps.row_ts, ps.row_prio = t, prio
                new_price = row["price_eur"]
                pi = price.get(pid)
                if pi is None:
                    price[pid] = PriceInfo(new_price, new_price, new_price, 0.0)
                elif new_price != pi.price:
                    price[pid] = PriceInfo(new_price, pi.base_price, pi.price, t)
                touched = touched or old != row
            else:
                self.n_stale += 1

            # stock: only stock-changing rows move it, again latest ts wins, OOS wins ties
            if ch in ("out_of_stock", "back_in_stock", "new_product") and (t, prio) > (ps.stock_ts, ps.stock_prio):
                ps.stock_ts, ps.stock_prio = t, prio
                was = ps.in_stock
                ps.in_stock = ch != "out_of_stock"
                if not ps.in_stock:
                    self.oos_at[pid] = t
                else:
                    self.oos_at.pop(pid, None)
                    if ch == "back_in_stock":
                        self.back_at[pid] = t
                touched = touched or was != ps.in_stock

            if ch == "new_product" and pid not in self.new_at:
                self.new_at[pid] = t
                touched = True
            changed += touched
        if changed:
            self._publish(state)
        return changed

    def _publish(self, state: State) -> None:
        """Swap in fresh immutable sets (atomic for readers on other threads)."""
        cat = state.catalog
        state.available = frozenset(
            pid for pid, ps in self.prods.items()
            if ps.in_stock and (row := cat.get(pid)) is not None and row.get("is_active", True))
        state.new_products = frozenset(self.new_at)
        live = state.live
        live["new_product_ts"] = dict(self.new_at)
        live["oos_since"] = dict(self.oos_at)
        live["back_in_stock_ts"] = dict(self.back_at)


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes")
    return bool(v)
