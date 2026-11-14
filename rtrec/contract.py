"""The contract between the serving loop, the live state and the models.

Owner: master session. This file is the interface every agent codes against:

    runtime (A1)  ── builds Request dicts, owns the loop, calls ──▶  Policy.recommend(req, state)
    live    (A4)  ── keeps ──▶  State up to date from events / catalog_updates / feedback
    model   (A3)  ── implements ──▶  Policy (candidates + ranker), reading only from State
    validate      ── always runs last ──▶  finalize(items, state) -> exactly 10 valid ids

Rules
- Policy.recommend is PURE: no I/O, no SQL, no sleeping, no global mutation. Everything it needs
  is in State. Target < 5 ms per request (a batch of 20 must take < 100 ms).
- State fields may be empty (cold start, first seconds after a restart): every policy must
  still return something sensible, and finalize() always guarantees a valid answer.
- Add new State fields freely in your own sub-objects (e.g. state.live.*, state.model.*),
  but do not rename or remove the fields below without asking the master.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .schemas import Product, Request


@dataclass
class UserProfile:
    """What we know about one shopper (history + live). Lists hold (ts_epoch, product_id)."""
    views: list[tuple[float, str]] = field(default_factory=list)
    carts: list[tuple[float, str]] = field(default_factory=list)
    purchases: list[tuple[float, str]] = field(default_factory=list)
    carousel_shown: int = 0
    carousel_clicks: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class State:
    """Everything a policy may read. Mutated only by the live-state updaters (A4) and the runtime (A1)."""
    # --- catalogue (A1 keeps these exact; validity of every answer depends on them) ---
    catalog: dict[str, Product] = field(default_factory=dict)       # current product, latest update applied
    available: frozenset[str] = frozenset()                          # active AND in stock right now
    new_products: frozenset[str] = frozenset()                       # added during the event (new_product updates)
    safe_list: list[str] = field(default_factory=list)               # best sellers, used by finalize() to pad
    # --- shoppers ---
    users: dict[str, UserProfile] = field(default_factory=dict)
    # --- precomputed offline artefacts (A2/A3): co-views, priors, embeddings, model... ---
    offline: dict[str, Any] = field(default_factory=dict)
    # --- live statistics (A4): trends, calibration, bandit state, A/B assignment... ---
    live: dict[str, Any] = field(default_factory=dict)
    # --- bookkeeping ---
    updated_at: float = field(default_factory=time.time)

    def is_cold(self, req: Request) -> bool:
        """Same definition as the organisers: new shopper, or a viewed product added during the event."""
        return bool(req.get("is_new_user")) or any(
            v["product_id"] in self.new_products for v in req.get("recent_views") or [])


class Policy(Protocol):
    """A recommender. `name` is written as model_version in every submission (A/B tracking)."""
    name: str

    def recommend(self, req: Request, state: State) -> list[str]:
        """Ordered product ids, best first. May return fewer or more than 10, or invalid ids:
        finalize() repairs. Must not raise for ordinary inputs (the runtime still guards it)."""
        ...
