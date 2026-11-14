# HANDOFF · RawTree Challenge (Datathon FME 2026) · equipo `team_36`

Documento para que otra IA (o la misma sesión tras reiniciarse) retome el proyecto sin perder contexto.
Escrito el sáb 14-nov-2026 ~14:00 CET. **Todo el trabajo está en este repo**; léelo junto con `docs/memory/MEMORY.md`.
El usuario habla español. Respóndele en español, breve: **le preocupa gastar tokens**.

---

## 0. En 10 líneas

1. Reto: tienda simulada (~5.000 productos). Cada ~0,33 s llega un comprador a `shop.requests`. Hay que escribir en `team_36.submissions` una **lista ordenada de 10 `product_id` distintos, activos y en stock en < 3 s desde el `ts`** de la petición.
2. **Solo puntúa domingo 15-nov 10:00–12:00 CET (09:00–11:00 UTC)**, con datos nuevos (compradores, productos y tendencias nuevos). La práctica (sáb 10:00 → dom 10:00) no puntúa pero es el laboratorio.
3. Nota: 70 pts automáticos (conversión 50, cold start 10, fiabilidad 10) + 30 del jurado (método 10, aprendizaje del stream 10, uso de RawTree 5, comunicación 5). `score = 100·(equipo − baseline)/(oráculo − baseline)`; baseline = best sellers fijos en stock; oráculo = lo que elegiría el modelo oculto.
4. **Un solo producto sin stock invalida toda la lista** (se sirve el fallback). Respuesta tardía o inválida = fallback = 0 de conversión.
5. Repo del equipo: **`ToniOrtizGil/rawtree-recsys-fme2026`** (privado). `main` está al día, ~121 tests en verde.
6. **Acceso real a RawTree funciona** (ver §3). La clave NO está en ningún archivo.
7. Se construyó un sistema completo (runtime, modelo v1/v2, aprendizaje en vivo, panel, runbook, presentación) con 5 agentes. **Los 5 agentes están ARCHIVADOS** (el usuario los paró para ahorrar tokens). No hay nada escribiendo en `team_36` ahora mismo.
8. Mejor resultado medido: **v1 = 64–67 de 100 en el simulador normal, 39 en el alternativo**. Con datos reales aún **no hay ninguna puntuación de modelo**.
9. Real medido: el baseline de la organización convierte **1,14 %**; nuestro baseline en vivo, ~1,0 % (ruido).
10. **Bloqueo principal del usuario:** no ha dicho dónde correrá la producción de mañana (VPS o portátil que no se duerma) ni quién pilota/vigila el domingo.

---

## 1. Qué pide el usuario ahora

- Que **no se gasten tokens**: no lances agentes en masa, no hagas sondeos periódicos, no imprimas salidas largas (usa `head`/`tail`/`grep`), ejecuta solo los tests relevantes mientras iteras y la batería completa antes de subir.
- Retomar cuando se reinicien sus tokens. Pregúntale **qué quiere reactivar** antes de despertar agentes.
- Lo que más valor tiene al retomar (en orden): (1) máquina de producción y roles del domingo; (2) **ejecución continua de práctica** con v1 + ~20 % de exploración para generar datos de entrenamiento; (3) evaluar con datos reales; (4) congelar a las 09:00 CET del domingo.

---

## 2. Reglas y puntos finos del reto (README oficial manda)

- Una sola respuesta por petición cuenta. Agrupar inserts (~cada 0,5 s). El orden importa: las primeras posiciones se miran mucho más (curva medida en sim: hueco 10 ≈ 0,14 × hueco 1; top‑3 ≈ 58 % de la atención).
- Petición **fría** = `is_new_user` **o** `recent_views` contiene un producto añadido durante el evento (`new_product` en `catalog_updates`). Pesa 10 pts aparte.
- La conversión del marcador usa la **probabilidad** de compra del modelo oculto, no la moneda. Quien ya compró un producto tiende a no querer repetirlo.
- Desempate: ingreso esperado por petición. Las recomendaciones deben salir de código; el sistema debe correr desatendido. Lo que no se sepa explicar al jurado puede penalizar.
- Final: marcador oculto; solo luz verde/roja (verde si >90 % de respuestas válidas y a tiempo en 5 min). Se puede seguir cambiando código, pero **no reiniciar nada** durante la final.
- Todo es sintético (simulador con semilla secreta). Compartir claves/feedback con otros equipos está prohibido.

---

## 3. RawTree: acceso y particularidades

- API: `https://api.rawtree.com`, `POST /v1/query` `{"sql": "...", "format": "JSONEachRow"}` y `POST /v1/tables/{tabla}?database=team_36` para insertar JSON. Auth `Authorization: Bearer <clave>`.
- **TEAM_ID = `team_36`.** La clave la inyecta un **proxy del entorno Default** de Claude Code (se guardó con el formulario seguro `request_credential` para `api.rawtree.com`). **Desde el código se usa `RAWTREE_API_KEY=proxy-injected`** (cualquier valor). Si lo ejecuta otra máquina o IA fuera de ese entorno, hace falta la clave real: **pídesela al usuario, no la pegues en archivos ni en commits**.
- La clave se pegó dos veces en el chat: **tratarla como expuesta**; pedir que la anulen al acabar el evento.
- Lección: la primera copia guardada daba 401 aunque la clave valía; se resolvió volviéndola a guardar. Prueba de humo: `SELECT count() FROM shop.catalog` con `?database=team_36` → debe dar **5000**.
- Columnas **`Dynamic`**: casteos obligatorios (`product_id::String`, `ts::DateTime64(3)`, `price_eur::Float64`). `count(col)` cuenta el envoltorio: usa `countIf(col IS NOT NULL)`. Arrays de objetos son JSON (`recent_views.product_id` falla); arrays planos (`shown::Array(String)`) sí funcionan.
- Solo `SELECT/WITH/INSERT…SELECT`, `EXPLAIN`, `DESCRIBE`; sin vistas, `UPDATE`, `DELETE` ni `INSERT VALUES`. Límites: 1.000 inserts/s por tabla y 10.000 consultas/s por base, **compartidos con todos los equipos** (consultas pocas y en serie), consulta máx. 30 s y 100 MiB.
- Una tabla cruda vacía responde "Empty list of columns in projection": tratarlo como sin filas.
- Formato real de `shop.requests`: `context.device/page/region` aplanadas; `recent_views` = array de `{product_id, ts}`; `ts` tipo `2026-11-14 12:23:47.880000000` (sin `Z`, UTC).
- `team_36.feedback`: `latency_ms` es `null` cuando es `missing`; `fallback_reason` manda sobre la latencia (la organización contó como "a tiempo" una respuesta de 3.297 ms).
- Volúmenes reales (14-nov 12:24 UTC): catalog 5.000 · history_sessions 2.000.000 · history_events 11.996.560 · requests ~37k (≈3/s desde 09:00 UTC) · events ~814k (≈66/s) · catalog_updates 930.

---

## 4. Qué sabemos de los datos reales (medido)

- Histórico 2026-09-15 → 11-14: CTR 3,75 %, compra 1,40 % por impresión, P(compra|clic) 37 %; eventos: 86 % vistas, 9 % carritos, 4,6 % compras. ≈28.000 compras en el histórico de sesiones. 10 categorías, 89 subcategorías, 80 marcas, precio mediano 30,99.
- Live: ~3,0 req/s y ~66 eventos/s. ~30 % de compradores nuevos y ~20 % de peticiones sin vistas (A4; A2 estimó 50 %/25 %: **sin reconciliar**).
- **Tendencias reales y grandes, a nivel de subcategoría, tipo escalón, ~45 min**: Acuarios 09:30–10:15 UTC, Hervidores 11:30–12:15 UTC. En tendencia: vistas ×4,5–4,8, carritos+compras ×11–14, compras por vista ×2–3. El detector de A4 las pone en el puesto 0/89 **un minuto** tras empezar. Sin tendencias de marca por ahora.
- Catálogo: 3 agotados cada lote de 5 min (marcas redondas, `is_active=False`), vuelven exactamente 60 min después; **100 productos nuevos de golpe a las 10:00 UTC** (primer evento orgánico: mediana ~1 min; compran algo más que su cuota: 2,7 % de compras con 2,0 % de productos); **620 bajadas de precio de golpe a las 10:30** (mediana −19 %, máx. −20 %). Sin duplicados ni desorden en el almacenamiento. En el simulador esto estaba muy exagerado (150 nuevos/h y 400 agotados/h vs ~29 y ~37 reales).
- Baseline de la organización sobre nuestras peticiones sin responder: **1,14 %** (34/2.993). Nuestro baseline en vivo: ~1,0 % (n=1.785; ~0,79 % en otra ventana pequeña) — intervalos solapados, sin diferencia demostrada.

---

## 5. Arquitectura del repo

```
rtrec/schemas.py contract.py store.py validate.py   ← contrato (dueño: máster; no cambiar sin necesidad)
rtrec/runtime/   bucle de servicio (A1): sondeo con cursor, 3 niveles de respaldo, journal, latido, standby
rtrec/stores/    FakeStore (simulador con temporización oficial) y RawTreeStore (probado en vivo, ver §7)
rtrec/live/      aprendizaje en vivo (A4): catálogo/stock, tendencias+lift, perfiles, co-visitas, calibración,
                 Thompson, A/B, cold start, hook.py (LiveHook), persist.py (snapshot)
rtrec/model/     candidatos, variables, LightGBM, políticas Baseline / SessionHeuristic (v1) / Ranker (v2) (A3)
rtrec/offline/   evaluación offline (IPS/SNIPS/DR) · rtrec/sql + rtrec/data: variables en SQL y sus gemelas pandas (A2)
sim/             simulador sintético: `make_simulator(seed, "default"|"alt"|"small")`, judge/oracle/expected_purchase
scripts/         arbiter.py (puntúa políticas), download_history.py (histórico real → Parquet), train_ranker.py
dashboard/       panel en vivo + alertas (`python -m dashboard serve|check|sqlcheck`) · ops/ despliegue y RUNBOOK
docs/            memory/ (memoria completa), LIVE.md, MODEL.md, FORENSICS.md, presentation/ (5 diapositivas + guion)
```

Reglas de diseño (importantes): `Policy.recommend(req, state)` es **pura** (<5 ms, sin E/S); **todo pasa por `validate.finalize`**; **un solo hilo de servicio es dueño del `State`**; el `CatalogTracker` es el único escritor de `state.catalog/available/new_products` (el de A4 con dos relojes es más seguro; A4 y A1 dan el mismo `available` en el flujo real, 20/20 pruebas); **sin IDs de producto/usuario como variables**; `LiveWrapper` re-ordenaciones (calibración, mezcla fría) **empeoraron** la puntuación en el árbitro: no activarlas.

### Cómo se decide qué va a producción (árbitro)
`python scripts/arbiter.py rtrec.model.policies:SessionHeuristicPolicy --seed 7 --config default|alt --warmup-min 5 --minutes 15`
Simulador nuevo por política, hooks de `rtrec.live` en tiempo de evento, feedback con retraso de 5 s, baseline = best sellers fijos en stock. Salida JSON con `score`, `cold_score`, compras/100, errores, ms/petición. Regla: gana el mejor `score` sin empeorar `cold_score`, 0 errores, p99 < 1,8 s y, **con datos reales, además un A/B en vivo por reparto de `hash(request_id)`**.

---

## 6. Resultados medidos hasta ahora

Simulador (no son datos reales):

| Versión | Simulador normal | Simulador alt | Notas |
|---|---|---|---|
| Baseline | 0 | 0 | ancla |
| v1 heurística de sesión | **64–67** (semillas 7, 11, otra) | **39** | 2–4 ms/petición, 0 errores |
| v2 LightGBM solo histórico | 54,1 | — | pierde: solo ~900 compras en 150k impresiones simuladas |
| v2 histórico + 2 h de nuestro flujo con exploración | 67,5 ± 3,9 | — | empata con v1 dentro del error |
| Capa en vivo (A4) sobre v1 | ≈ +0,3 pt | — | en el sim las tendencias no se parecían a las reales |

- **Conclusión honesta:** la v1 es sólida pero ajustada al simulador normal; la v2 solo mejora con nuestro propio flujo de práctica. Con datos reales (≈28k compras en el histórico) la v2 podría aprender sola: **primera comprobación pendiente**.
- Prueba en vivo de A1 (10 min, 12:25–12:35 UTC, baseline): 1.783/1.785 respuestas válidas (99,9 %), 2 `invalid` (producto más vendido agotado justo en el lote de 12:30), 0 `missing/late` tras calentar; latencia medida por la organización: p50 993 ms, p95 1.527 ms, p99 1.976 ms, máx 2.445 ms. Desglose: ver petición p50 0,38 s / p99 1,26 s; confirmación del insert p50 0,81 s; cómputo < 2 ms.
- Prueba de carga simulada a 10× (30 req/s): 0 perdidas, p99 1,49–1,79 s; `kill -9` → reinicio en 1,4 s sin perder respuestas; relevo de la reserva ~3 s.

---

## 7. Estado de lo que pidió el usuario (mejoras de latencia y generalización)

**Latencia**
| # | Mejora | Estado |
|---|---|---|
| 1 | Producción cerca de la API de RawTree (ahorro estimado ~0,5 s) | **PENDIENTE: depende de la máquina que elija el usuario.** Mide el RTT de una consulta simple desde cada candidata: `curl -s -o /dev/null -w "%{time_total}\n" -X POST https://api.rawtree.com/v1/query -H "Authorization: Bearer $K" -H "Content-Type: application/json" -d '{"sql":"SELECT 1"}'` varias veces y elige la menor. |
| 2 | Varios sondeos escalonados | **HECHO** (A1: 3 sondeos, rama integrada en `main`; probar con el ensayo en vivo nº 2) |
| 3 | Conexión caliente (sesión HTTP persistente + inserción de calentamiento) | **Parcial**: sesión persistente existe; verifica que haya una inserción de calentamiento al arrancar y que se salten las peticiones con >3 s de antigüedad tras el arranque (se perdieron ~32 por eso) |
| 4 | Insertar enseguida en lotes pequeños | **Hecho en parte**: A1 tiene inserciones "hedged" (duplicados inofensivos: 5 en 1.811); confirma que no espera ventana |
| 5 | No reiniciar durante la final | **Regla operativa** (RUNBOOK); congelar código a las 09:00 CET |

**Generalización**
| # | Mejora | Estado |
|---|---|---|
| 1 | Evaluar con datos reales | **PENDIENTE.** A2 bajó el histórico real a **su** contenedor (que se archivó: se perdió). Repetir: `RAWTREE_API_KEY=proxy-injected python scripts/download_history.py --out data` (≈15 min, secuencial, reanudable). |
| 2 | A/B v1 vs v2 con flujo de práctica | **PENDIENTE.** Requiere arrancar la ejecución continua. Tamaños: +50 % relativo (1,1→1,65 %) ≈ 12.000 peticiones (~1 h); +20 % ≈ 87.000 (~8 h). Si hay duda, la más simple. |
| 3 | Variables relacionales, nunca IDs | **Hecho en diseño** (v1/v2); mantener |
| 4 | Tendencias detectadas en vivo sin memorizar | **Hecho en A4** (`subcat_lift`/`brand_lift` frente a la cuota histórica, `live.seed_history`); **falta** que A3 lo use como variable fuerte y término fuerte de la v1 |
| 5 | Elegir por el peor caso en 3 simuladores | **Parcial**: `arbiter --config default|alt` listo; **falta** el preset `SimConfig.real()` (A2 con las cifras reales de §4) |
| 6 | Mezclar ranker + heurística | **PENDIENTE** (A3) |
| 7 | Valores iniciales de productos nuevos por subcategoría/marca/precio | `live.coldstart.new_product_prior` existe; **falta** integrarlo en el ranker de A3 |

---

## 8. Cómo arrancar el sistema (A1; comandos reales)

- Primario: `ops/run.sh` · reserva: `ops/run.sh --standby` · simulado: `ops/run.sh --fake` · humo: `ops/run.sh --smoke`.
- Solo baseline: `RTREC_POLICY=rtrec.runtime.policies:BaselinePolicy ops/run.sh`.
- Promoción de la reserva: automática si pasan 2,5 s sin latido del primario; manual: parar la reserva y lanzar `ops/run.sh` allí.
- Informe en vivo: `python ops/live_report.py --minutes 10`. Panel: `TEAM_ID=team_36 RAWTREE_API_KEY=proxy-injected python -m dashboard serve --store rawtree` (el primer cargado tarda ~10 s: aparece WARMING_UP). Comprobación de una línea (exit 0/1/2): `python -m dashboard check --store rawtree`.
- Latidos en `team_36.scratch`: `{"kind":"heartbeat","ts":ISO,"role":"primary|standby","host","model_version"}`.
- **Un solo escritor** en `team_36.submissions` a la vez. Hoy no corre ninguno.
- Para poner la v1 en vivo hacen falta sus artefactos (mejores vendidos, co-visitas, priors…) construidos desde `data/` (histórico real). A3 iba a dar un comando único para construirlos: **si no existe, créalo** (`scripts/train_ranker.py` y `rtrec/model/policies.py` son el punto de partida).

---

## 9. Pendientes, por prioridad

1. **Usuario:** máquina de producción (recomendado VPS pequeño cerca de la API; alternativa: portátil enchufado sin suspensión; el contenedor cloud NO vale para la final) y **roles del domingo** (piloto, vigilante, reserva) para `ops/RUNBOOK.md` §0.
2. Descargar histórico real, construir artefactos v1, **ensayo en vivo nº 2** (5–10 min, 3 sondeos, confirmar p99 < 1,8 s), luego **ejecución continua de práctica** (v1 + ~20 % de exploración con propensiones registradas; filas `kind:"served"` en `scratch`; ver `rtrec/model/streamlog.py`).
3. A3: tendencia como variable fuerte; entrenar/evaluar con datos reales (IPS/SNIPS con recorte de pesos; 52 % de propensiones < 1e-3 en el sim); comando único de reentreno y regla de promoción (cuánto N).
4. A2: `SimConfig.real()` con los patrones reales de §4 (varias variantes de calendario), reconciliar % de nuevos usuarios, documentar números reales en `docs/FORENSICS.md`.
5. A4: resultado de la prueba de carga de 24 h; interruptor de impulso de tendencia para A/B.
6. A1: mitigación opcional de la carrera de stock (esperar ≤1,5 s tras marcas :00/:05… si es sencillo); `hook_chunk ≤ 400`.
7. **Domingo:** congelar a las 09:00 CET, ensayo de reinicio y de caída, reserva verificada, `dashboard sqlcheck` una vez, luz verde 10:00–12:00, presentación (`docs/presentation/slides.html`, guion y marcadores `<placeholder>` en `docs/presentation/README.md`).
8. Al terminar el evento: pedir que **anulen la clave** de `team_36` (se pegó en el chat) y decidir si el repo pasa a público para el jurado.

---

## 10. Sesiones de agentes (todas ARCHIVADAS; se pueden desarchivar con `unarchive_session`)

| Agente | Sesión | Rama | Coste aprox. |
|---|---|---|---|
| A1 Runtime | `session_01XduZa7bWed8icL7CDD4hBu` | `agent/a1-runtime` | 13,5 $ |
| A2 Datos y simulador | `session_01XduZa7ZveaS8oXNcVTnrEA` | `agent/a2-data` | 9,8 $ |
| A3 Modelo | `session_01XduZa6oWCiBF3WijBEYKsJ` | `agent/a3-model` | 8,9 $ |
| A4 Aprendizaje en vivo | `session_01XduZa5VmpfkUiCyUonCiDp` | `agent/a4-live` | 12,6 $ |
| A5 Observabilidad y demo | `session_01XduZa6RtBhjLfcHutNiT9H` | `agent/a5-demo` | 10,3 $ |

- Sesión máster original: `session_01XduZa73MPSwYUepqrdFyRP` (entorno `env_01JwXiBpBQ4t1bLVfx7YJcTw`, "Default").
- Los contenedores no comparten disco: todo viaja por git. Cada agente trabaja en su rama `agent/aN-*`; la máster integra en `main`. Reglas en `CLAUDE.md`; reparto en `docs/TASKS.md`; formato de informe: `[A<n>] DONE|PROGRESS|BLOCKED — <una línea> · branch <x> @ <sha> · needs from user: <…>`.
- **Antes de reactivar agentes**, valora si hace falta: con pocos tokens, lo más barato es que una sola sesión haga A1 (despliegue) y A3 (modelo) en serie. Pídeles a todos respuestas de ≤3 líneas.
- Entorno: la red del entorno Default solo permite los hosts que se han añadido (`rawtree.com`, `datathon.rawtree.dev`) más `api.rawtree.com` por la credencial. El shell de la sesión no tiene `gh` autenticado para crear repos (los crea el usuario).
- Git: usa la identidad `Claude <noreply@anthropic.com>` en los commits (un hook marca como no verificados los demás). Cada commit termina con las líneas de atribución que indique el sistema. No abras PRs salvo que el usuario lo pida.

---

## 11. Archivos clave para leer primero

1. `docs/memory/MEMORY.md` y `docs/memory/01…06` (reto, datos, API, starter, entorno, plan).
2. `ops/RUNBOOK.md` (procedimiento del domingo, tabla de decisión por síntoma).
3. `docs/LIVE.md` (claves de `state.live` y medidas del flujo real), `docs/MODEL.md`, `docs/FORENSICS.md`.
4. `rtrec/contract.py`, `rtrec/runtime/serve.py`, `rtrec/model/policies.py`, `scripts/arbiter.py`.
5. `ops/reports/loadtest.md` (pruebas de carga simuladas).
