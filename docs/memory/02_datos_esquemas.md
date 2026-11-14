# Datos y esquemas (todo vive en RawTree)

Una API key por equipo: **lee** la base compartida `shop` y **lee/escribe** su base `team_NN`.

## Disponibles desde el inicio

### `shop.catalog` (~5.000 productos)
`product_id` (string opaco) · `category` (10 valores) · `subcategory` (90 valores) · `brand` · `price_eur` (precio inicial; los cambios llegan por `catalog_updates`) · `description` (1–2 frases) · `created_at` · `is_active` (bool).

### `shop.history_sessions` (60 días, ≈2 M impresiones del carrusel antiguo)
`request_id` · `ts` · `user_id` · `context` {`device`: web/ios/android, `region`, `page`: home/product/category} · `recent_views` [{product_id, ts}] · `shown` (10 productos en orden) · `propensity` (prob. de que el sistema antiguo mostrara cada producto en ese hueco **dados los de los huecos anteriores**) · `clicked` (string o null) · `purchased` (string o null) · `revenue_eur`.

- El sistema antiguo es "best sellers + un poco de aleatoriedad" ⇒ log **sesgado hacia lo popular**. `propensity` permite corregirlo (IPS/SNIPS).
- Es el único dato histórico etiquetado con el mismo mecanismo que se puntúa (carrusel ⇒ clic ⇒ compra). ≈20 M pares (impresión, producto, hueco).

### `shop.history_events` (60 días, ≈12 M eventos orgánicos, fuera del carrusel)
`ts` · `event_type` (view / add_to_cart / purchase) · `user_id` · `session_id` · `product_id` · `price_eur` (precio en ese momento) · `device` · `region`.

- Sirve para aprender gustos más allá de lo que mostró el sistema antiguo (co‑visitas, afinidades usuario‑categoría, sensibilidad a precio).

## Durante el evento (append-only)

### `shop.requests` (~3/s)
```json
{"request_id":"r_01JD8K3...","ts":"2026-11-14T16:02:11.482Z","user_id":"u_7f21c9","is_new_user":false,
 "context":{"device":"ios","region":"Catalunya","page":"product"},
 "recent_views":[{"product_id":"p_5a1e","ts":"2026-11-14T16:01:40Z"},{"product_id":"p_0b9d","ts":"2026-11-14T16:01:58Z"}]}
```

### `shop.events` (~50/s junto con catalog_updates)
Mismo formato que `history_events`. Aquí se ven las tendencias en tiempo real.

### `shop.catalog_updates`
Un registro por cambio: el producto completo tras el cambio + `ts` + `change` ∈ {`new_product`, `price_change`, `out_of_stock`, `back_in_stock`}. Catálogo vigente = `shop.catalog` con la última actualización de cada producto encima.

### `team_NN.feedback` (privado)
Se escribe unos segundos después del deadline.
```json
{"request_id":"r_01JD8K3...","ts":"2026-11-14T16:02:16.900Z","shown":["p_91aa","p_5a1e","..."],
 "served_by":"team","fallback_reason":"","latency_ms":840,"clicked":"p_91aa","purchased":"p_91aa","revenue_eur":34.99}
```
- `served_by`: `team` o `fallback`; `fallback_reason`: `missing` / `late` / `invalid`.
- `clicked` y `purchased` son `""` cuando no pasa nada (en el histórico son null).
- Primer registro `request_id = "__init__"`: marcador de posición, ignorar.
- `latency_ms` = latencia medida por los organizadores ⇒ verdad absoluta para ajustar nuestro margen.

### `team_NN.submissions` (lo que escribimos)
```json
{"request_id":"r_01JD8K3...","items":["p_91aa", "...10 ids..."],"model_version":"v3-mf-bandit"}
```

### `team_NN.scratch`
Tabla vacía y libre (features, embeddings, salidas de modelo, latidos, logs de experimentos). Sin esquema.

## Observaciones útiles para la estrategia

- En el ejemplo oficial, `p_5a1e` aparece en `recent_views` **y** en `items`/`shown` (hueco 2): recomendar productos recién vistos es válido y probablemente convierte. A verificar con datos.
- Antes de las 10:00 del sábado las tablas en vivo estaban vacías y las consultas fallaban con *Unknown expression identifier*.
- Volumen de la final: ~3/s × 7.200 s ≈ 21.600 peticiones. En práctica: ~260.000 peticiones/día ⇒ mucho feedback propio para aprender.
- Petición fría = `is_new_user` **o** algún producto de `recent_views` creado durante el evento.
