"""Record shapes of every RawTree table we read or write.

Owner: master session. Agents must not change these shapes without asking the master:
every module depends on them. Field names match the RawTree tables exactly
(see docs/memory/02_datos_esquemas.md and docs/upstream/CHALLENGE_README.md).
Timestamps are ISO-8601 UTC strings in RawTree; inside our process we use float epoch seconds.
"""
from __future__ import annotations

from typing import Literal, TypedDict

Device = Literal["web", "ios", "android"]
Page = Literal["home", "product", "category"]
EventType = Literal["view", "add_to_cart", "purchase"]
Change = Literal["new_product", "price_change", "out_of_stock", "back_in_stock"]
ServedBy = Literal["team", "fallback"]
FallbackReason = Literal["", "missing", "late", "invalid"]


class Context(TypedDict):
    device: Device
    region: str
    page: Page


class View(TypedDict):
    product_id: str
    ts: str


class Request(TypedDict):
    """shop.requests: one shopper arriving at the carousel (~3/s)."""
    request_id: str
    ts: str
    user_id: str
    is_new_user: bool
    context: Context
    recent_views: list[View]


class Product(TypedDict):
    """shop.catalog row; shop.catalog_updates rows add `ts` and `change`."""
    product_id: str
    category: str
    subcategory: str
    brand: str
    price_eur: float
    description: str
    created_at: str
    is_active: bool


class CatalogUpdate(Product):
    ts: str
    change: Change


class Event(TypedDict):
    """shop.events / shop.history_events: organic activity outside the carousel."""
    ts: str
    event_type: EventType
    user_id: str
    session_id: str
    product_id: str
    price_eur: float
    device: Device
    region: str


class HistorySession(TypedDict):
    """shop.history_sessions: one impression of the old carousel."""
    request_id: str
    ts: str
    user_id: str
    context: Context
    recent_views: list[View]
    shown: list[str]
    propensity: list[float]
    clicked: str | None
    purchased: str | None
    revenue_eur: float


class Submission(TypedDict):
    """team_NN.submissions: our answer. Exactly 10 distinct, active, in-stock ids."""
    request_id: str
    items: list[str]
    model_version: str


class Feedback(TypedDict):
    """team_NN.feedback: outcome of each answer, a few seconds after the deadline.
    clicked / purchased are "" when nothing happened. Ignore request_id == "__init__"."""
    request_id: str
    ts: str
    shown: list[str]
    served_by: ServedBy
    fallback_reason: FallbackReason
    latency_ms: float
    clicked: str
    purchased: str
    revenue_eur: float


N_ITEMS = 10
DEADLINE_S = 3.0
