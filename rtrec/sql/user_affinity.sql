-- Long-term taste of returning shoppers: views / carts / purchases per user and subcategory.
WITH cat AS (SELECT product_id::String AS p, subcategory::String AS sub FROM shop.catalog)
SELECT e.u AS user_id, cat.sub AS subcategory,
       countIf(e.k = 'view') AS views, countIf(e.k = 'add_to_cart') AS carts, countIf(e.k = 'purchase') AS purchases
FROM (SELECT user_id::String AS u, product_id::String AS p, event_type::String AS k
      FROM shop.$table
      WHERE ts::DateTime64(3) >= parseDateTime64BestEffort('$since', 3)
        AND ts::DateTime64(3) <  parseDateTime64BestEffort('$until', 3)) AS e
INNER JOIN cat ON cat.p = e.p
GROUP BY user_id, subcategory
ORDER BY user_id, subcategory
