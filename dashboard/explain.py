"""Why is this product in the list? A plain-language tag per recommended item, for the live feed and demo.

These tags are the dashboard's *reading* of a list (computed from the request, the catalogue and the live
trends), not the model's internal score: they explain the families of signals the ranker uses (session
match, newness, trends, popularity) in words a product manager understands. If a policy logs its own
reasons later, the page can show those instead.
"""
from __future__ import annotations


def tags(pid: str, views: tuple[str, ...] | list[str], catalog: dict[str, dict], new_products: set[str],
         trending_subs: set[str], popular: set[str]) -> list[str]:
    p = catalog.get(pid) or {}
    viewed = [catalog.get(v) or {} for v in views]
    out = []
    if pid in views:
        out.append("viewed")
    elif p.get("subcategory") and p.get("subcategory") in {v.get("subcategory") for v in viewed}:
        out.append("same subcategory")
    elif p.get("category") and p.get("category") in {v.get("category") for v in viewed}:
        out.append("same category")
    if p.get("brand") and pid not in views and p.get("brand") in {v.get("brand") for v in viewed}:
        out.append("same brand")
    if pid in new_products:
        out.append("new")
    if p.get("subcategory") in trending_subs:
        out.append("trending")
    if pid in popular:
        out.append("selling now")
    return out or ["other"]
