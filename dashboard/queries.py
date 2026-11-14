"""SQL twins of the dashboard metrics, run inside RawTree once we have a key.

Why: (1) "Use of RawTree" is judged; these show the monitoring itself is computed in the database, next to
the data; (2) a cross-check: the page shows the Python number and the SQL number side by side.

RawTree quirks applied everywhere (docs/memory/03_rawtree_api_sql.md): every column is Dynamic, so join keys,
comparisons inside aggregates and quantile arguments are cast (::String, ::Float64, ::DateTime64(3)).
`{now}` defaults to now64(3); tests substitute a fixed instant. All queries are SELECT-only.

Validated against embedded ClickHouse (chdb) with Dynamic columns on DemoStore data
(tests/test_dashboard_sql.py). NOT yet run against live RawTree: run `python -m dashboard sqlcheck` on key day.
Assumption to verify then: shop.requests exposes the original JSON row as `__raw_data` (used to read
recent_views[].product_id without depending on how RawTree types an array of objects).
"""
from __future__ import annotations

import re

NOW = "now64(3)"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")

_FEEDBACK_WINDOW = """
    SELECT request_id::String AS request_id, ts::DateTime64(3) AS ts, served_by::String AS served_by,
           fallback_reason::String AS fallback_reason, latency_ms::Float64 AS latency_ms,
           clicked::String AS clicked, purchased::String AS purchased, revenue_eur::Float64 AS revenue_eur
    FROM {team}.feedback
    WHERE request_id::String != '__init__'
      AND ts::DateTime64(3) > {now} - INTERVAL {minutes} MINUTE AND ts::DateTime64(3) <= {now}"""


def _fw(team: str, minutes: int, now: str) -> str:
    return _FEEDBACK_WINDOW.format(team=team, minutes=minutes, now=now)


def light(team: str, now: str = NOW) -> str:
    """Our estimate of the organisers' green light: % valid and on time over the last 5 minutes."""
    return f"""
SELECT count() AS answers,
       countIf(served_by = 'team' AND fallback_reason = '') AS ok,
       round(100 * ok / greatest(answers, 1), 2) AS ok_pct,
       countIf(fallback_reason = 'missing') AS missing,
       countIf(fallback_reason = 'late')    AS late,
       countIf(fallback_reason = 'invalid') AS invalid,
       if(answers < 20, 'unknown', if(ok_pct > 90, 'green', 'red')) AS light
FROM ({_fw(team, 5, now)}
)"""


def latency(team: str, now: str = NOW) -> str:
    """Organisers' latency over the last 5 minutes (exact, linearly interpolated percentiles)."""
    return f"""
SELECT count() AS answers,
       round(quantileExactInclusive(0.50)(latency_ms), 1) AS p50_ms,
       round(quantileExactInclusive(0.95)(latency_ms), 1) AS p95_ms,
       round(quantileExactInclusive(0.99)(latency_ms), 1) AS p99_ms,
       max(latency_ms) AS max_ms
FROM ({_fw(team, 5, now)}
)
WHERE fallback_reason != 'missing' AND latency_ms > 0"""


def by_version(team: str, minutes: int = 60, now: str = NOW) -> str:
    """Purchases per 100 requests per model_version (A/B), joining our submissions to the feedback."""
    return f"""
SELECT if(f.served_by = 'team' AND f.fallback_reason = '',
          if(s.model_version = '', '(no submission row)', s.model_version), '(fallback)') AS model_version,
       count() AS requests,
       countIf(f.purchased != '') AS purchases,
       round(100 * purchases / requests, 3) AS purchases_per_100,
       round(100 * countIf(f.clicked != '') / requests, 3) AS ctr_pct,
       round(sum(f.revenue_eur) / requests, 4) AS revenue_per_request
FROM ({_fw(team, minutes, now)}
) AS f
LEFT JOIN (
    SELECT request_id::String AS request_id, any(model_version::String) AS model_version
    FROM {team}.submissions
    WHERE request_id::String IN (SELECT request_id FROM ({_fw(team, minutes, now)}))
    GROUP BY request_id
) AS s ON s.request_id = f.request_id
GROUP BY model_version
ORDER BY requests DESC"""


def cold_warm(team: str, minutes: int = 60, now: str = NOW) -> str:
    """Purchases per 100 for cold vs warm shoppers, organisers' definition: new shopper, or a recent view of
    a product added during the event. Only answers we served (fallback rows say nothing about our model)."""
    return f"""
WITH (SELECT groupUniqArray(product_id::String) FROM shop.catalog_updates
      WHERE change::String = 'new_product') AS new_products
SELECT if(r.is_new_user OR hasAny(r.views, new_products), 'cold', 'warm') AS segment,
       count() AS requests,
       countIf(f.purchased != '') AS purchases,
       round(100 * purchases / requests, 3) AS purchases_per_100
FROM ({_fw(team, minutes, now)}
) AS f
INNER JOIN (
    SELECT request_id::String AS request_id, is_new_user::Bool AS is_new_user,
           arrayMap(v -> JSONExtractString(v, 'product_id'),
                    JSONExtractArrayRaw(__raw_data::String, 'recent_views')) AS views
    FROM shop.requests
    WHERE ts::DateTime64(3) > {now} - INTERVAL {minutes + 5} MINUTE
) AS r ON r.request_id = f.request_id
WHERE f.served_by = 'team' AND f.fallback_reason = ''
GROUP BY segment
ORDER BY segment"""


def per_minute(team: str, minutes: int = 60, now: str = NOW) -> str:
    return f"""
SELECT toStartOfMinute(ts) AS minute,
       count() AS answers,
       round(100 * countIf(served_by = 'team' AND fallback_reason = '') / answers, 2) AS ok_pct,
       round(quantileExactInclusive(0.95)(latency_ms), 1) AS p95_ms,
       round(100 * countIf(purchased != '') / answers, 3) AS purchases_per_100
FROM ({_fw(team, minutes, now)}
)
GROUP BY minute
ORDER BY minute DESC"""


def trends(now: str = NOW) -> str:
    """Organisers' query 3, tightened to 5 minutes vs the 15 before: which subcategories are taking off."""
    return f"""
SELECT c.subcategory AS subcategory,
       countIf(e.ts > {now} - INTERVAL 5 MINUTE) AS views_recent,
       round(countIf(e.ts <= {now} - INTERVAL 5 MINUTE) / 3, 1) AS views_base,
       round(views_recent / greatest(views_base, 1), 2) AS lift
FROM (SELECT product_id::String AS product_id, ts::DateTime64(3) AS ts FROM shop.events
      WHERE event_type::String = 'view' AND ts::DateTime64(3) > {now} - INTERVAL 20 MINUTE
        AND ts::DateTime64(3) <= {now}) AS e
INNER JOIN (SELECT product_id::String AS product_id, subcategory::String AS subcategory FROM shop.catalog) AS c
    ON c.product_id = e.product_id
GROUP BY subcategory
HAVING views_recent >= 5
ORDER BY lift DESC
LIMIT 8"""


def last_heartbeat(team: str) -> str:
    return f"""
SELECT 'heartbeat' AS kind, toString(ts::DateTime64(3)) AS ts, role::String AS role, host::String AS host,
       model_version::String AS model_version
FROM {team}.scratch
WHERE kind::String = 'heartbeat'
ORDER BY ts::DateTime64(3) DESC
LIMIT 1"""


def versions_for(team: str, request_ids: list[str]) -> str:
    ids = [r for r in request_ids if _SAFE_ID.match(r)]
    in_list = ", ".join(f"'{r}'" for r in ids) or "''"
    return f"""
SELECT request_id::String AS request_id, any(model_version::String) AS model_version
FROM {team}.submissions
WHERE request_id::String IN ({in_list})
GROUP BY request_id"""


def panels(team: str, now: str = NOW) -> dict[str, tuple[str, str]]:
    """name -> (title, sql) for every panel that has a SQL twin."""
    return {
        "light": ("Green light (last 5 min)", light(team, now).strip()),
        "latency": ("Latency p50/p95/p99 (last 5 min)", latency(team, now).strip()),
        "by_version": ("Purchases/100 by model_version (60 min)", by_version(team, 60, now).strip()),
        "cold_warm": ("Cold vs warm (60 min)", cold_warm(team, 60, now).strip()),
        "per_minute": ("Per-minute reliability and conversion", per_minute(team, 60, now).strip()),
        "trends": ("Trending subcategories (organic views)", trends(now).strip()),
        "heartbeat": ("Last heartbeat in scratch", last_heartbeat(team).strip()),
    }
