# Reto, reglas y puntuación

Fuentes: README del repo `rawtreedb/datathon-fme-2026` (la más completa y la que manda) y https://datathon.rawtree.dev/#challenge.

## Qué se construye

Un sistema que, solo y sin que nadie pulse nada:
1. Lee peticiones de recomendación de `shop.requests` según llegan (~3/s, día y noche).
2. En menos de 3 s escribe una lista ordenada de 10 `product_id` en `team_NN.submissions`.
3. Aprende del feedback propio (`team_NN.feedback`) y del stream compartido (`shop.events`, `shop.catalog_updates`).

"Your system *is* your submission": no se sube ningún archivo. Lo que el sistema conteste el domingo 10:00–12:00 es lo que se puntúa.

## Reglas duras de la respuesta

- **Exactamente 10 IDs distintos y activos.** Respuesta inválida ⇒ fallback. El starter advierte: recomendar un producto sin stock invalida **toda** la respuesta.
- Debe llegar a RawTree **dentro de 3 s desde el `ts` de la petición**, medido por el reloj de los organizadores (sondean cada 0,5 s). Tarde ⇒ fallback.
- **Una sola respuesta por petición** (si hay varias, solo cuenta una).
- **Agrupar inserts** (p. ej. cada 0,5 s), no una llamada HTTP por petición.
- Referencia: el baseline sondea cada 0,3 s y responde en ~1 s (p95 1,5 s) desde el `ts`.
- **El orden importa**: los primeros huecos se miran mucho más que los últimos.
- `model_version` es un campo libre de texto por respuesta (útil para A/B).

## Calendario (hora de Barcelona, CET = UTC+1; los `ts` de los datos son UTC)

| Cuándo | Qué |
|---|---|
| Sábado 14-nov, apertura | Datos, claves y starter kit |
| Sáb 10:00 → Dom 10:00 | Ronda de práctica: stream en vivo, marcador público cada 10 min (última hora) |
| **Dom 10:00 → 12:00** | **Ronda final**: datos nuevos (compradores, productos y tendencias nuevos). Marcador oculto; solo luz verde/roja por equipo |
| Dom 12:00 | Se cierra. El stream para y aparece el ranking final |
| Dom tarde | Presentación corta con demo ante el jurado |

- Luz de estado: verde si >90 % de las respuestas de los últimos 5 min fueron válidas y a tiempo. No puntúa.
- Durante la final se sigue recibiendo feedback propio y se puede seguir cambiando el código.
- "Make sure your best version is running by 10:00."

## Puntuación

### 70 puntos automáticos (solo ronda final)

| Componente | Pts | Métrica |
|---|---|---|
| Conversión | 50 | Compras esperadas por 100 peticiones |
| Cold start | 10 | Lo mismo, solo en **peticiones frías**: comprador nuevo (`is_new_user`) **o** `recent_views` que incluye un producto añadido durante el evento (`new_product` en `shop.catalog_updates`) |
| Fiabilidad | 10 | % de peticiones respondidas válidamente y a tiempo |

```
score = 100 × (equipo − baseline) / (oráculo − baseline), recortado a [0, 100]
```
- **baseline (0)**: best sellers históricos en stock, ordenados una vez al inicio del stream. Es también el fallback.
- **oráculo (100)**: la mejor lista posible, los 10 que el propio modelo oculto elegiría para cada comprador.
- El marcador muestra además una **referencia** de los organizadores ("it can be beaten").
- En el marcador los 70 pts se normalizan a 100: ~71 conversión, ~14 cold start, ~14 fiabilidad.
- Nota final = 70 % de ese score + hasta 30 pts del jurado.

**Sin suerte**: se usa la probabilidad de compra calculada por el modelo oculto dadas las 10 mostradas, no la moneda. El feedback muestra clics/compras realizados; en media coinciden. Las compras reales sí alimentan algo el modelo: quien ya compró un producto tiende a no quererlo otra vez.

**Desempate**: ingreso esperado por petición en la final.

### 30 puntos del jurado

| Criterio | Pts | Qué miran |
|---|---|---|
| Método | 10 | ¿Enfoque sólido y justificado? ¿Cómo se validó offline antes de salir en vivo? |
| Aprendizaje del stream | 10 | Uso de feedback y eventos en vivo: exploración, tendencias, cambios de precio, cold start |
| Uso de RawTree | 5 | Features calculadas en SQL, embeddings, cómo lee y escribe el pipeline |
| Comunicación | 5 | ¿Lo entendería un product manager? |

## Otras reglas

- No compartir claves ni feedback con otros equipos.
- Recomendaciones producidas por código, no a mano.
- Sistema desatendido en una máquina que no se duerma ("a laptop works if it stays awake and online all night").
- Se pueden usar agentes de IA y LLMs, pero hay que saber explicar todo.
- Los mejores equipos comparten el repositorio: mantenerlo legible.

## Pistas explícitas de los organizadores ("The store moves")

- Compradores nuevos: muchos llegan sin historial.
- Productos nuevos: solo descripción, sin interacciones.
- Tendencias: visibles pronto en los eventos en vivo.
- Cambios de precio: anunciados en `catalog_updates`.
- Roturas de stock: productos inactivos son respuestas inválidas.
- "Teams that generalise beat teams that memorise."
- Siguientes pasos sugeridos en el notebook: gusto personal (factorización, item‑to‑item), embeddings de texto para cold start, sensibilidad al precio según `device`/`region`, usar el feedback propio.

## Discrepancia detectada

La web dice en un punto que el carrusel actual muestra "the best sellers of the category the shopper is looking at"; el README y la sección de scoring dicen "all‑time best sellers in stock". El código del starter confirma lo segundo (top‑10 global). Tomar el README como verdad.
