"""Data access, abstracted so the whole system runs without an API key.

    RawTreeStore (A1)  real HTTP client for api.rawtree.com (needs TEAM_ID + RAWTREE_API_KEY)
    FakeStore    (A1)  in-memory tables driven by a sim.base.Simulator (A2) — same behaviour, no network

Every reader is incremental: it takes a cursor (epoch seconds of the last row already seen) and
returns rows with ts > cursor - overlap; callers deduplicate by key. Owner of this protocol: master.
"""
from __future__ import annotations

from typing import Protocol

from .schemas import CatalogUpdate, Event, Feedback, Product, Request, Submission


class Store(Protocol):
    team: str

    # --- static tables (read once at start, or downloaded to Parquet by scripts/) ---
    def catalog(self) -> list[Product]: ...

    # --- live append-only tables ---
    def requests_since(self, cursor: float, limit: int = 500) -> list[Request]: ...
    def events_since(self, cursor: float, limit: int = 50_000) -> list[Event]: ...
    def catalog_updates_since(self, cursor: float, limit: int = 50_000) -> list[CatalogUpdate]: ...
    def feedback_since(self, cursor: float, limit: int = 50_000) -> list[Feedback]: ...

    # --- writes into team_NN ---
    def insert_submissions(self, rows: list[Submission]) -> None: ...
    def insert_scratch(self, rows: list[dict]) -> None: ...

    # --- escape hatch for SQL features (RawTreeStore only; FakeStore may raise NotImplementedError) ---
    def query(self, sql: str) -> list[dict]: ...
