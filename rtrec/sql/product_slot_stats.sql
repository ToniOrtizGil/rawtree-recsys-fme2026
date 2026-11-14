-- Per (product, slot) impressions / clicks / purchases and inverse-propensity mass of the old carousel.
-- Small (<= 10 x products rows); the position curve and position-corrected product CTRs are fitted
-- from it client side (rtrec.data.features.fit_position_bias), because the same product appears at
-- different slots thanks to the old system's randomness.
SELECT p AS product_id,
       k AS slot,
       count() AS impressions,
       countIf(ifNull(clicked::Nullable(String), '') = p)   AS clicks,
       countIf(ifNull(purchased::Nullable(String), '') = p) AS purchases,
       sum(1 / w) AS inv_propensity
FROM shop.history_sessions
ARRAY JOIN shown::Array(String) AS p, propensity::Array(Float64) AS w, arrayEnumerate(shown::Array(String)) AS k
WHERE ts::DateTime64(3) >= parseDateTime64BestEffort('$since', 3)
  AND ts::DateTime64(3) <  parseDateTime64BestEffort('$until', 3)
GROUP BY product_id, slot
ORDER BY product_id, slot
