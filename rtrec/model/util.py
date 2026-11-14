"""Small shared helpers for the model package (timestamps, request accessors)."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

from rtrec.schemas import Request


def to_epoch(ts) -> float:
    """ISO-8601 UTC string (RawTree format), datetime/Timestamp or number -> epoch seconds; '' -> 0."""
    if ts is None or ts == "":
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()
    return _parse(str(ts))


@lru_cache(maxsize=200_000)
def _parse(s: str) -> float:
    s = s.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def view_ids(req: Request) -> list[str]:
    """Product ids of recent_views in chronological order (oldest first), as RawTree sends them."""
    return [v["product_id"] for v in (req.get("recent_views") or []) if v and v.get("product_id")]


def context(req: Request) -> tuple[str, str, str]:
    c = req.get("context") or {}
    return c.get("device", ""), c.get("region", ""), c.get("page", "")


def clean_id(x) -> str | None:
    """Logged product id or None: RawTree history uses null, feedback uses "", pandas may give NaN."""
    if x is None or x != x or x == "":  # x != x is NaN
        return None
    return str(x)
