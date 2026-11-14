# Starter kit (repo `rawtreedb/datathon-fme-2026`)

Clonado en `/home/user/datathon/sources/repo/`. Archivos: `README.md`, `.env.example` (`TEAM_ID=team_NN`, `RAWTREE_API_KEY=rt_...`; nunca subir `.env`), `starter/recommender.py`, `starter/queries.sql`, `starter/offline_eval.ipynb`, `starter/requirements.txt` (requests, pandas, numpy, jupyter).

## `starter/recommender.py` — el baseline (y el fallback)

Bucle `pull → predict → push` cada 0,3 s:
- `best_sellers()`: `SELECT product_id, count() FROM shop.history_events WHERE event_type='purchase' GROUP BY product_id`, ordenado desc (empates: mayor product_id). **Se calcula una sola vez al arrancar; nunca se refresca.**
- `out_of_stock()`: productos cuyo último cambio de stock en `catalog_updates` es `out_of_stock` (se consulta 1 vez/s).
- `pull()`: `SELECT request_id, recent_views FROM shop.requests ORDER BY ts DESC LIMIT 50` + conjunto `seen` en memoria.
- `recommend()`: los 10 primeros best sellers que no estén sin stock. **Ignora `recent_views`, usuario y contexto.**
- `push()`: un insert por lote en `team_NN.submissions`, `model_version="baseline"`.
- Ante cualquier excepción: imprime, duerme 2 s y sigue.

### Debilidades que explotaremos
1. Cero personalización: misma lista para todos.
2. `LIMIT 50` sin cursor: si hay una pausa >~15 s (50 peticiones a 3/s) **se pierden peticiones**; cada sondeo relee 50 filas completas.
3. `seen` crece sin límite y se pierde al reiniciar (tras un reinicio re‑responde las últimas 50 ⇒ duplicados).
4. Solo excluye `out_of_stock`; no comprueba `is_active` ni productos nuevos.
5. Síncrono y monohilo: una consulta lenta bloquea todo; `sleep(2)` tras error = peticiones tardías.
6. No lee feedback, ni eventos, ni precios.

## `starter/queries.sql` — 7 consultas de ejemplo
1. Catálogo vigente (`catalog` + última fila de `catalog_updates` con `argMax`).
2. Popularidad por categoría en la última hora (`LIMIT 5 BY category`).
3. Tendencias: vistas por subcategoría, últimos 15 min vs media de las 3 h anteriores (lift).
4. Co‑visitas: productos vistos en la misma sesión que uno dado.
5. Feedback propio: CTR, conversión e ingreso por petición por hora y `served_by`.
6. Fiabilidad: motivos de fallback y p50/p95 de `latency_ms` en los últimos 30 min.
7. Sesgo de posición en el log antiguo: clics por hueco (`indexOf(shown, clicked)`).

## `starter/offline_eval.ipynb` — evaluación offline
- Separa los **últimos 7 días** de `history_sessions` como test (muestra aleatoria de 100.000 impresiones); políticas construidas solo con datos anteriores al corte.
- Métricas: **hit rate@10** (¿el producto clicado está en nuestro top‑10? sesgado a populares) e **IPS / SNIPS del CTR en el hueco 1** (solo impresiones donde nuestro primer producto coincide con el que mostró el sistema antiguo, ponderadas por `1/propensity[0]`). Avisa de que pocas coincidencias ⇒ estimación ruidosa.
- Política de ejemplo `coview`: co‑visitas de los productos vistos en la sesión (peso `0.7^k / (rank+1)`, más reciente primero) y relleno con baseline.
- Importa `recommender.py`: evalúa el mismo código que corre en vivo (buena práctica a mantener).
