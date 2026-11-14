-- Organic popularity per product and context (device x region) in a time window.
-- Feeds context-popularity candidates and cold-start lists for shoppers without views.
SELECT product_id::String AS product_id,
       device::String     AS device,
       region::String     AS region,
       countIf(event_type = 'view')        AS views,
       countIf(event_type = 'add_to_cart') AS carts,
       countIf(event_type = 'purchase')    AS purchases
FROM shop.$table
WHERE ts::DateTime64(3) >= parseDateTime64BestEffort('$since', 3)
  AND ts::DateTime64(3) <  parseDateTime64BestEffort('$until', 3)
GROUP BY product_id, device, region
ORDER BY product_id, device, region
