"""Adapter for A1's runtime HookSet (rtrec/runtime/hooks.py): one object, all live updaters.

    from rtrec.live.hook import LiveHook
    Server(..., hooks=[LiveHook()])

A1's CatalogTracker is the single writer of state.catalog / available / new_products and has
already applied each catalogue batch when on_catalog_updates is called; here the live catalogue
tracker runs with owner=False and only derives features (price/stock history, new-product
neighbours).

on_served also records which model_version answered each request and the request itself, so
feedback (which carries neither user_id nor model_version) is attributed to the right shopper,
cold segment and A/B arm.
"""
from __future__ import annotations

from rtrec.contract import State

from . import learner


class LiveHook:
    def on_start(self, state: State) -> None:
        lr = learner(state)
        lr.catalog.owner = False
        lr.init(state, list(state.catalog.values()))

    def on_catalog_updates(self, rows, state: State) -> None:
        lr = learner(state)
        lr.catalog.owner = False
        lr.on_catalog_updates(rows, state)

    def on_events(self, rows, state: State) -> None:
        learner(state).on_events(rows, state)

    def on_feedback(self, rows, state: State) -> None:
        learner(state).on_feedback(rows, state)

    def on_served(self, answers, state: State) -> None:
        lr = learner(state)
        lr.on_requests([a[0] for a in answers], state)
        for req, _items, mv in answers:
            lr.versions[req["request_id"]] = mv
            if len(lr.versions) > lr.max_requests:
                lr.versions.pop(next(iter(lr.versions)))

    def on_tick(self, state: State, now: float) -> None:
        lr = learner(state)
        if lr.trends.maybe_publish(state):
            lr.coview.publish(state, lr.trends.now)
