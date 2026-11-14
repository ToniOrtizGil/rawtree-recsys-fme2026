"""Live features of one (request, product) pair, for A3's ranker: no ids, only relations and rates.

Every value is cheap (dict lookups) and has a neutral default when the stream has not seen the
product yet, so the same features work for new products and right after a restart.
"""
from __future__ import annotations

import math

from rtrec.contract import State
from rtrec.schemas import Request

LIVE_FEATURES = ("trend_rate_log", "trend_lift_log", "subcat_lift_log", "buy_rate_log",
                 "price_ratio_to_base", "price_last_change", "price_changed_recently",
                 "is_new_product", "minutes_since_new", "back_in_stock_recent", "calib_mult_log",
                 "calib_subcat_log", "impressions_log", "ctx_rank", "coview_session")


def live_features(req: Request, pid: str, state: State, now: float | None = None) -> dict[str, float]:
    live = state.live
    now = live.get("now", 0.0) if now is None else now
    sub = (state.catalog.get(pid) or {}).get("subcategory")
    pi = (live.get("price") or {}).get(pid)
    new_ts = (live.get("new_product_ts") or {}).get(pid)
    back = (live.get("back_in_stock_ts") or {}).get(pid)
    ctx = req.get("context") or {}
    ctx_list = (live.get("ctx_top") or {}).get((ctx.get("device"), ctx.get("region")), [])
    cov = live.get("coview") or {}
    co = 0.0
    for v in req.get("recent_views") or []:
        for q, w in cov.get(v["product_id"], ()):
            if q == pid:
                co += w
                break
    return {
        "trend_rate_log": math.log1p((live.get("trend_rate") or {}).get(pid, 0.0)),
        "trend_lift_log": math.log((live.get("trend_lift") or {}).get(pid, 1.0)),
        "subcat_lift_log": math.log((live.get("subcat_lift") or {}).get(sub, 1.0)),
        "buy_rate_log": math.log1p((live.get("buy_rate") or {}).get(pid, 0.0)),
        "price_ratio_to_base": pi.ratio_to_base if pi else 1.0,
        "price_last_change": pi.last_change_ratio if pi and pi.changed_at else 1.0,
        "price_changed_recently": float(bool(pi and pi.changed_at and now - pi.changed_at < 3600)),
        "is_new_product": float(pid in state.new_products),
        "minutes_since_new": (now - new_ts) / 60.0 if new_ts else -1.0,
        "back_in_stock_recent": float(bool(back and now - back < 1800)),
        "calib_mult_log": math.log((live.get("calib_product") or {}).get(pid, 1.0)),
        "calib_subcat_log": math.log((live.get("calib_subcat") or {}).get(sub, 1.0)),
        "impressions_log": math.log1p((live.get("impressions") or {}).get(pid, 0)),
        "ctx_rank": float(ctx_list.index(pid)) if pid in ctx_list else 99.0,
        "coview_session": math.log1p(co),
    }
