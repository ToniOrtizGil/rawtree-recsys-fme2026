-- Co-views: for each product, the $k products most often viewed in the same session.
-- Run per day window on 12M events (result and time limits) and sum the counts client side.
WITH v AS (
    SELECT DISTINCT session_id::String AS s, product_id::String AS p
    FROM shop.$table
    WHERE event_type = 'view'
      AND ts::DateTime64(3) >= parseDateTime64BestEffort('$since', 3)
      AND ts::DateTime64(3) <  parseDateTime64BestEffort('$until', 3)
)
SELECT a.p AS product_id, b.p AS other_id, count() AS sessions
FROM v AS a
INNER JOIN v AS b ON a.s = b.s
WHERE a.p != b.p
GROUP BY product_id, other_id
ORDER BY product_id, sessions DESC, other_id
LIMIT $k BY product_id
