# RawTree: API, límites y SQL

RawTree = base analítica para JSON, **fork de ClickHouse** (producto de Tinybird). Se inserta JSON y se consulta con SQL, sin esquema. Docs para agentes: https://rawtree.com/llms.txt (índice) y https://rawtree.com/docs/llms.txt (corpus completo). Copias locales en `/home/user/datathon/sources/docs/`.

## Acceso

- Base URL: `https://api.rawtree.com` · Auth: `Authorization: Bearer $RAWTREE_API_KEY`
- **Consulta**: `POST /v1/query` con `{"sql": "...", "format": opcional, "query_id": opcional}` (+ `?database=team_NN` opcional). Una sentencia por petición.
  - Respuesta por defecto JSON: `{"meta":[...], "data":[{col: valor}], "rows": n, "statistics": {elapsed, rows_read, bytes_read}, "hints": [...]}`.
  - Formatos: `JSON` (defecto), `JSONEachRow`, `JSONEachRowWithProgress` (streaming NDJSON), `JSONCompact`, `CSV`, `CSVWithNames`, `TSV`, `TSVWithNames`, `TabSeparated`. Aceptar gzip con `Accept-Encoding: gzip`.
  - `hints` = avisos de tipos incompatibles saltados; revisarlos aunque la consulta "funcione".
- **Inserción**: `POST /v1/tables/{tabla}?database=team_NN` con un objeto JSON o un array. Respuesta con `written_rows` (string). Crea la tabla si no existe.
  - Parámetros opcionales: `deduplicate_insert`, `insert_deduplication_token`.
  - Un error de inserción **no garantiza** que no se escribiera nada ⇒ cuidado al reintentar (riesgo de duplicar respuestas).
- CLI: `curl -fsSL https://rawtree.com/install.sh | bash`; `rtree query --api-key $RAWTREE_API_KEY --database $TEAM_ID "SELECT ..."`.
- MCP: `https://mcp.rawtree.com/mcp` con cabecera `Authorization: Bearer rt_...`.
- Logs de peticiones: `GET /v1/logs?start_time=...&end_time=...&limit=50`.
- OpenAPI: https://rawtree.com/openapi.json · Skill para agentes: `npx skills add rawtreedb/agent-skills`.

## Límites

| Cosa | Límite | Si se supera |
|---|---|---|
| Cuerpo de insert | 100 MiB | 413 |
| Inserts | 1.000 req/s por tabla | 429 `rate_limit_exceeded` (+ `retry-after`) |
| Cuerpo de consulta | 50 MiB | 413 |
| Resultado de consulta (`max_result_bytes`) | 100 MiB | 400 |
| Ejecución (`max_execution_time`) | 30 s | 408 |
| Consultas | 10.000 req/s por base de datos | 429 |

- Los límites se comparten entre quienes usan el mismo recurso (¡`shop` es compartida por todos los equipos!). Cabeceras `x-ratelimit-*`.
- Implicación: bajar el histórico (12 M eventos) **por trozos** (por día) y en formato compacto (CSV/TSV + gzip), no de una vez.

## Particularidades SQL (críticas)

- **Todas las columnas son `Dynamic`**: cada valor conserva su tipo. Filtros, ORDER BY y la mayoría de funciones funcionan tal cual.
- **Necesitan cast**: claves de JOIN, `argMax`/`argMin` y algunos agregados ⇒ `product_id::String`, `ts::DateTime64(3)`, `price_eur::Float64`, `is_active::Bool`. Error típico: *Illegal type Dynamic*.
- `count(col)` puede contar el envoltorio aunque sea null ⇒ usar `countIf(col IS NOT NULL)`.
- Un cast estricto puede convertir ausentes en valor por defecto (0, '') y crear falsos emparejamientos; `accurateCastOrNull(x,'Int64')` es la versión tolerante.
- Campos anidados con punto (`context.device`). Fila original en `__raw_data`.
- Una ruta desconocida ⇒ `UNKNOWN_IDENTIFIER` (por eso fallaban las tablas en vivo vacías).
- Disponible sintaxis ClickHouse: `LIMIT n BY col`, `countIf`, `quantile(0.95)(x)`, `argMax`, `toStartOfHour`, `now() - INTERVAL 1 HOUR`, `indexOf(array, x)`, `ARRAY JOIN`.

## Qué admite y qué no el Query API

- Sí: `SELECT`, `WITH`, `EXPLAIN`, `DESCRIBE`, `INSERT INTO <tabla> SELECT ...` (la tabla destino debe existir; requiere lectura+escritura), `TRUNCATE TABLE` (con escritura).
- Requiere **admin** (probablemente no lo tenemos): `CREATE TABLE ... ENGINE = ReplicatedRawMergeTree [ORDER BY ...]`, roles, handlers (endpoints HTTP definidos en SQL), workloads.
- No expuesto: `CREATE VIEW`, `CREATE MATERIALIZED VIEW`, `CREATE DICTIONARY`, `UPDATE`, `DELETE`, `OPTIMIZE`, `RENAME`, `INSERT ... VALUES`, `INSERT ... FORMAT`.
- Consecuencia: `submissions`, `feedback` y `scratch` son append-only para nosotros; no se puede corregir una respuesta enviada.
- "SQL workflows" (SQL programado con destinos) existen pero dependen de que estén habilitados y de permisos; no contar con ellos.

## Buenas prácticas de rendimiento

- Seleccionar solo las columnas necesarias, filtrar por tiempo, agregar en SQL antes de devolver filas, `LIMIT` al explorar.
- Comparar `statistics.rows_read` / `bytes_read`; `EXPLAIN indexes = 1` para ver uso de índices.
- Para el bucle en vivo: consultas incrementales con `WHERE ts > cursor` y conexión HTTP persistente.
