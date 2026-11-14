-- Current catalogue: shop.catalog with the latest shop.catalog_updates row of each product on top.
-- available = active and not out of stock (one out-of-stock item invalidates a whole answer).
-- Inner columns are prefixed (x_) so the outer aliases do not shadow them inside the aggregates.
SELECT x_pid                    AS product_id,
       argMax(x_cat, v)         AS category,
       argMax(x_sub, v)         AS subcategory,
       argMax(x_brand, v)       AS brand,
       argMax(x_price, v)       AS price_eur,
       argMin(x_price, v)       AS launch_price_eur,
       argMax(x_active, v)      AS is_active,
       argMax(x_change, v)      AS last_change,
       is_active AND last_change != 'out_of_stock' AS available
FROM (
    SELECT product_id::String AS x_pid, category::String AS x_cat, subcategory::String AS x_sub,
           brand::String AS x_brand, price_eur::Float64 AS x_price, is_active::Bool AS x_active,
           'catalog' AS x_change, toDateTime64('1970-01-01 00:00:00', 3, 'UTC') AS v
    FROM shop.catalog
    UNION ALL
    SELECT product_id::String, category::String, subcategory::String, brand::String, price_eur::Float64,
           is_active::Bool, change::String, ts::DateTime64(3)
    FROM shop.catalog_updates
)
GROUP BY x_pid
ORDER BY product_id
