-- Raw position curve of the old carousel: impressions, clicks and purchases per slot (1..10).
-- Confounded (the old system put best sellers on top); see product_slot_stats + fit_position_bias.
SELECT k AS slot,
       count() AS impressions,
       countIf(ifNull(clicked::Nullable(String), '') = p)   AS clicks,
       countIf(ifNull(purchased::Nullable(String), '') = p) AS purchases,
       avg(w)  AS mean_propensity
FROM shop.history_sessions
ARRAY JOIN shown::Array(String) AS p, propensity::Array(Float64) AS w, arrayEnumerate(shown::Array(String)) AS k
WHERE ts::DateTime64(3) >= parseDateTime64BestEffort('$since', 3)
  AND ts::DateTime64(3) <  parseDateTime64BestEffort('$until', 3)
GROUP BY slot
ORDER BY slot
