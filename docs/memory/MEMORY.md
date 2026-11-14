# MEMORIA · RawTree Challenge (Datathon FME 2026)

Índice de la memoria del proyecto. Leer este archivo primero; el detalle está en los demás.

| Archivo | Contenido |
|---|---|
| `01_reto_reglas_puntuacion.md` | Qué hay que construir, reglas duras, calendario, cómo se puntúa |
| `02_datos_esquemas.md` | Todas las tablas de RawTree (campos, volúmenes, matices) |
| `03_rawtree_api_sql.md` | API, límites, particularidades SQL (Dynamic, casts), qué NO se puede hacer |
| `04_starter_kit.md` | Qué hace el baseline, sus debilidades, la evaluación offline incluida |
| `05_entorno_operativa.md` | Entorno de esta sesión, bloqueos, sesión máster + agentes, acciones pendientes del usuario |
| `06_plan_ganador.md` | Estrategia completa, arquitectura, fases, reparto por agentes |

Copias literales de las fuentes: `/home/user/datathon/sources/` (repo clonado, `challenge.html`, `llms.txt`, `docs/`).

## Lo imprescindible en 12 líneas

1. Tienda simulada (~5.000 productos, 10 categorías, 90 subcategorías). Cada ~0,33 s llega un comprador a `shop.requests`.
2. Hay que escribir en `team_NN.submissions` una lista **ordenada de 10 product_id distintos, activos y en stock** en **< 3 s desde el `ts`** de la petición (reloj de los organizadores, sondeo cada 0,5 s).
3. Respuesta tardía / inválida / ausente ⇒ *fallback* (best sellers) y se pierde fiabilidad. **Un solo producto sin stock invalida la lista entera.**
4. **Solo puntúa domingo 10:00–12:00 CET** (≈21.600 peticiones) sobre datos nuevos: compradores nuevos, productos nuevos, tendencias nuevas. El marcador se oculta (solo luz verde/roja).
5. Práctica: sábado 10:00 → domingo 10:00, marcador público cada 10 min (muestra la última hora). No cuenta.
6. Puntos: Conversión 50 · Cold start 10 · Fiabilidad 10 · Jurado 30 (Método 10, Aprendizaje del stream 10, Uso de RawTree 5, Comunicación 5).
7. `score = 100 × (equipo − baseline) / (oráculo − baseline)`, recortado a [0,100]. Baseline = top‑10 best sellers históricos en stock (fijo). Oráculo = los 10 que elegiría el propio modelo oculto.
8. La conversión usa la **probabilidad** de compra del modelo oculto, no el resultado aleatorio ⇒ no hay suerte; maximizar compras reales.
9. Un comprador que ya compró un producto tiende a no quererlo otra vez.
10. Desempate: ingreso esperado por petición.
11. Todo es sintético (simulador con semilla secreta). Las recomendaciones deben salir de código, sin intervención manual; el sistema debe correr desatendido.
12. Todo lo que no se sepa explicar en la presentación puede penalizar; los mejores equipos comparten repo.

## Estado (actualizar)

- 2026-11-14 12:30 CET — Fuentes leídas, memoria creada, plan redactado.
- 2026-11-14 12:37 CET — Plan aprobado. Repo `ToniOrtizGil/rawtree-recsys-fme2026` con el contrato entre agentes; 5 sesiones-agente lanzadas (ver 05). **Sin API key** a propósito (el equipo aún elige entre dos retos): todo se desarrolla contra un simulador local (`sim/`) detrás de `rtrec.store.Store`. Pendiente del usuario: decisión de reto → TEAM_ID + clave (formulario seguro) y máquina de producción.
