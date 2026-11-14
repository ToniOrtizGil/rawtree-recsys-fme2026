-- What is trending: views per subcategory in the last $recent_min minutes vs the $base_min minutes before
-- (rate ratio, +1 smoothing). Includes products created during the event (new_product updates).
WITH cat AS (
    SELECT product_id::String AS p, any(subcategory::String) AS sub
    FROM (SELECT product_id, subcategory FROM shop.catalog
          UNION ALL SELECT product_id, subcategory FROM shop.catalog_updates WHERE change = 'new_product')
    GROUP BY p
)
SELECT cat.sub AS subcategory,
       countIf(e.t >= now_ - INTERVAL $recent_min MINUTE) AS views_recent,
       countIf(e.t <  now_ - INTERVAL $recent_min MINUTE) AS views_base,
       (views_recent + 1) / $recent_min / ((views_base + 1) / $base_min) AS lift
FROM (SELECT product_id::String AS p, ts::DateTime64(3) AS t, parseDateTime64BestEffort('$now', 3) AS now_
      FROM shop.$table
      WHERE event_type = 'view'
        AND ts::DateTime64(3) >= parseDateTime64BestEffort('$now', 3) - INTERVAL ($recent_min + $base_min) MINUTE
        AND ts::DateTime64(3) <  parseDateTime64BestEffort('$now', 3)) AS e
INNER JOIN cat ON cat.p = e.p
GROUP BY subcategory
ORDER BY lift DESC, subcategory
