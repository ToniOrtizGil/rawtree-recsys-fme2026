# Plan ganador · RawTree Challenge

Redactado el 14-nov-2026 12:30 CET. Estado: **propuesto, sin ejecutar**.

## 1. Tesis: dónde se gana

1. **La conversión (71 % del marcador) se mide contra un oráculo personalizado.** El baseline es una lista fija igual para todos. Cualquier uso de la sesión (`recent_views`) ya da un salto grande; el resto es acercarse a la función del simulador.
2. **El simulador es un modelo paramétrico oculto y tenemos datos etiquetados por él**: 2 M impresiones históricas (≈20 M pares producto‑hueco con clic/compra y propensión) + ~260.000 peticiones/día de feedback propio durante la práctica. El problema real es **aprender esa función de compra** `P(compra | comprador, contexto, producto, hueco)` y ordenar por ella.
3. **La final son datos nuevos.** Lo que generaliza son las *relaciones* (afinidad sesión‑producto, precio, contexto, contenido), no los IDs memorizados. El modelo se apoya en atributos y en estado construido en vivo.
4. **La fiabilidad (14 %) es casi gratis si se diseña primero**, y es un multiplicador: cada petición perdida recibe el baseline (0 puntos de conversión).
5. **La práctica es un laboratorio**: no puntúa, así que el sábado se explora de forma agresiva y se hacen tests A/B; el domingo se explota.
6. **30 puntos son del jurado** y piden exactamente lo que este diseño produce: validación offline, aprendizaje del stream, features en SQL, y una explicación que entienda un product manager.

## 2. Arquitectura

```
                  ┌────────────── hilos de fondo (no bloquean) ──────────────┐
shop.events ─────▶│ tendencias, co‑visitas en vivo, perfil de usuario        │
catalog_updates ─▶│ catálogo vigente, stock, precios, productos nuevos       │──▶ ESTADO EN MEMORIA
team.feedback ───▶│ calibración, bandit, métricas A/B, latencia real         │        │
                  └──────────────────────────────────────────────────────────┘        │
shop.requests ──▶ sondeo 150–250 ms ──▶ candidatos (~300) ──▶ ranker ──▶ validador ──▶ insert por lote
                   (cursor + solape)                                   (10 distintos, en stock; si algo falla ⇒ lista segura)
```

Principio: **ninguna consulta SQL en el camino caliente**. Todo lo que necesita una petición está en memoria; las consultas a RawTree van en hilos de fondo con su propio ritmo.

### 2.1 Bucle de servicio (rápido y que no se cae)
- Sondeo incremental con cursor (`WHERE ts > cursor − solape`) y deduplicación por `request_id`; conexión HTTP persistente.
- Presupuesto: visible ≈0,3–0,6 s tras el `ts` + sondeo ≤0,25 s + cómputo <50 ms por lote + insert ≈0,2 s ⇒ **objetivo p99 < 1,8 s** (límite 3 s). Se verifica con `latency_ms` del feedback, que es la medida de los organizadores.
- **Red de seguridad en tres niveles**: (1) ranker completo; (2) si lanza excepción o tarda, heurística de sesión; (3) lista segura precalculada de best sellers en stock. Nunca se deja una petición sin responder.
- **Validador final obligatorio**: exactamente 10, distintos, activos y en stock según el catálogo vigente (refrescado cada ≤0,5 s). Un producto sin stock invalida toda la lista.
- Supervisor con reinicio automático, estado persistido en disco (arranque en segundos), latido en `team_NN.scratch`, y **instancia de reserva** en otra máquina que solo responde si el latido se pierde.

### 2.2 Generación de candidatos (~300 por petición)
- Los propios productos de `recent_views` (hipótesis H1: recomendar lo recién visto convierte).
- Vecinos por co‑visita / co‑compra de cada vista (matrices calculadas en SQL sobre 12 M eventos + actualización en vivo).
- Mejores de las subcategorías y marcas de la sesión y del historial del usuario.
- Tendencias en vivo y populares por región/dispositivo/página.
- Productos nuevos similares por contenido (subcategoría, marca, precio, embedding de la descripción).
- Relleno: best sellers por tasa de conversión, no por ventas brutas.

### 2.3 Ranker
- **Modelo puntual de `P(compra)`**: gradient boosting (LightGBM) entrenado con `history_sessions` (hueco como variable, negativos submuestreados) y reentrenado con el feedback propio.
- Familias de variables: producto (categoría, subcategoría, marca, precio relativo dentro de su subcategoría, antigüedad, tasas de clic/compra corregidas por propensión y suavizadas); sesión (coincidencia con lo visto en producto/subcategoría/categoría/marca ponderada por recencia, ratio de precio, puntuación de co‑visita, similitud de texto); usuario (afinidades históricas, ya comprado, precio medio); contexto (dispositivo, región, página, hora); en vivo (lift de tendencia, variación de precio, tiempo desde alta o reposición).
- **Sin IDs como variables**: así puntúa igual un producto o comprador que no existía ayer.
- Orden final por probabilidad descendente (los primeros huecos se miran más); penalización a productos ya comprados; precio como desempate (criterio de desempate oficial = ingreso).
- Inferencia: ~3.000 filas por lote ⇒ 10–30 ms.

### 2.4 Aprendizaje del stream
- **Tendencias**: contadores con decaimiento exponencial por producto/subcategoría sobre `shop.events`; lift = ritmo reciente / ritmo de fondo.
- **Precios y stock**: `catalog_updates` cada ≤0,5 s; el precio vigente entra en las variables.
- **Perfil en vivo por usuario**: vistas, carritos y compras durante el evento (clave en la final: los compradores nuevos solo tienen historial en vivo).
- **Calibración por feedback**: corrección multiplicativa Beta‑Bernoulli por producto/subcategoría (observado vs. predicho); reentrenamiento periódico del ranker con el feedback acumulado.
- **Exploración**: en práctica, 10–15 % del tráfico con huecos bajos aleatorizados y orden permutado (con propensión registrada) para medir sesgo de posición y cubrir el catálogo. En la final, solo Thompson sampling en los huecos 9–10 para productos nuevos con incertidumbre.
- **Tests A/B continuos**: reparto por hash de `request_id`, etiquetado en `model_version`, comparación de compras por 100 peticiones con intervalo de confianza. Solo se promociona una versión si gana con significación.

### 2.5 Cold start (14 % del marcador)
- Comprador nuevo con vistas: tratar la sesión como único historial (el ranker ya funciona sin variables de usuario).
- Comprador nuevo sin vistas: mejor lista por contexto (región × dispositivo × página) ponderada por tendencia y tasa de conversión.
- Sesión con producto nuevo: ese producto + vecinos por contenido; los productos nuevos heredan el prior de su subcategoría/marca/precio hasta acumular eventos orgánicos (llegan en minutos a ~50 eventos/s).
- Se mide por separado en la práctica: mismo criterio que usa el organizador para "petición fría".

## 3. Preguntas que la exploración debe cerrar en las 2 primeras horas

| # | Pregunta | Decide |
|---|---|---|
| H1 | ¿Convierte mostrar un producto ya visto en la sesión? | Si los vistos van en cabeza |
| H2 | Curva de sesgo de posición (clics por hueco, corregida por propensión) | Cuánto vale ordenar bien; dónde explorar |
| H3 | ¿Qué explica el clic: misma subcategoría, categoría, marca, precio cercano? | Variables y candidatos |
| H4 | ¿Compra dado clic depende de precio × dispositivo/región? | Sensibilidad a precio |
| H5 | ¿El historial largo del usuario predice? ¿Cuánto penaliza haber comprado ya? | Peso del perfil de usuario |
| H6 | ¿Las tendencias de `events` predicen la conversión del carrusel? ¿Con qué retardo? | Vida media de los contadores |
| H7 | Efecto de una bajada de precio | Variable de variación de precio |
| H8 | ¿Hay como mucho un clic/compra por impresión? ¿Compra implica clic? | Forma del objetivo |
| H9 | Estructura de `propensity` | Estimador offline (IPS por hueco vs. lista) |
| H10 | En vivo: retardo de aparición de las peticiones, % de nuevos, % sin vistas, repetición de usuarios | Presupuesto de latencia y peso del cold start |
| H11 | ¿Qué pasa con productos que se agotan justo después de pedir? (`fallback_reason = invalid`) | Margen de seguridad de stock |

## 4. Validación offline (criterio "Método")

- Corte temporal: entrenar con días 1–53, evaluar con los últimos 7.
- Métricas: AUC / log‑loss del modelo puntual; **compras por 100 peticiones estimadas con IPS, SNIPS y doblemente robusto** usando `propensity`; hit rate@10 como control. Siempre con intervalo de confianza y número de coincidencias.
- Evaluar el **mismo código** que corre en vivo (como hace el starter).
- Segmento frío simulado: usuarios sin historial antes del corte y productos creados en la última semana.
- Regla: nada sale a producción sin ganar offline; nada se queda sin ganar en A/B en vivo.

## 5. Uso de RawTree (5 pts) y comunicación (5 pts)

- En SQL dentro de RawTree: catálogo vigente, popularidad por contexto, co‑visitas, tendencias, sesgo de posición, panel de fiabilidad y de A/B.
- En `team_NN.scratch`: latidos, registro de experimentos y versiones, embeddings/vecinos de productos nuevos, métricas agregadas (alimentan el panel de la demo).
- Demo: panel en vivo (peticiones entrando, lista recomendada y por qué, conversión por versión, tendencias, latencia) + 5 diapositivas con la historia "de best sellers a un sistema que aprende".

## 6. Fases (hora de Barcelona)

| Fase | Ventana | Entregable | Criterio de salida |
|---|---|---|---|
| F0 Arranque | Sáb 12:30–13:30 | Credenciales, repo, máquina; volcado del histórico a Parquet; **v0 = baseline robusto en vivo** | En el marcador, >99,5 % a tiempo, p95 < 1,5 s |
| F1 Forense | Sáb 13:00–16:00 | Informe H1–H11; arnés de evaluación offline | Preguntas cerradas con números |
| F2 Heurística | Sáb 14:00–17:00 | **v1** sesión + co‑visitas + stock + ya comprados; A/B operativo | Gana a v0 en A/B |
| F3 Modelo | Sáb 16:00–21:00 | **v2** ranker LightGBM con estado en vivo | Gana offline y en A/B a v1 |
| F4 Aprendizaje | Sáb 19:00–Dom 02:00 | **v3** tendencias, precios, calibración, exploración, cold start | Mejora medida en segmento frío y global |
| F5 Noche | Dom 00:00–07:00 | Exploración automática, reentrenos, vigilancia horaria de la máster | Sin intervención; alertas si baja la fiabilidad |
| F6 Congelación | Dom 07:00–09:30 | Elección de versión final, ensayo de reinicio y de caída, reserva verificada | Código congelado a las 09:00 |
| F7 Final | Dom 10:00–12:00 | Solo vigilancia; cambios únicamente si la luz se pone roja | Luz verde las 2 h |
| F8 Jurado | Dom 12:00– | Diapositivas, demo, repo limpio | — |

## 7. Reparto: sesión máster + agentes

Máster (esta sesión): orquesta, integra ramas en `main`, decide qué versión va a producción, vigila con comprobaciones programadas e informa al usuario indicando qué requiere acción suya.

| Agente | Misión | Entregables | Depende de |
|---|---|---|---|
| **A1 Runtime** | Bucle de servicio de grado producción | Sondeo con cursor, inserts por lote, validador, 3 niveles de seguridad, supervisor, latido, reserva, despliegue en un comando, v0 en vivo | Credenciales, máquina |
| **A2 Forense de datos** | Entender el simulador | Volcado a Parquet, informe H1–H11, features en SQL | Credenciales |
| **A3 Modelo** | Candidatos + ranker + evaluación offline | Arnés IPS/SNIPS/DR, v1 heurística, v2 LightGBM exportable | A2 (volcado), interfaz de A1 |
| **A4 Aprendizaje en vivo** | Estado en streaming y bandit | Tendencias, precios/stock, perfil en vivo, calibración, exploración, cold start, marco A/B | A1 (interfaz), A3 (ranker) |
| **A5 Observabilidad y demo** | Que se vea y se entienda | Panel en vivo, alertas, guía del domingo, diapositivas, README | A1 (métricas) |

Contrato entre agentes (lo fija la máster antes de lanzar): `recommend(request, state) -> list[str]` puro y sin E/S; `State` con interfaz estable; cada agente en su rama `agent/<nombre>`; todo cambio con test; nada toca `team_NN.submissions` salvo el proceso de producción (los demás prueban en modo sombra).

Orden de lanzamiento: A1 y A2 a la vez en cuanto haya credenciales; A3 cuando A2 tenga el volcado; A4 y A5 cuando A1 publique la interfaz.

## 8. Dónde corre producción (decisión del usuario)

| Opción | A favor | En contra |
|---|---|---|
| **VPS pequeño (recomendada)** | No se duerme, red estable, latencia baja | Hay que crearlo (5–10 min) y desplegar con `git clone` + un comando |
| Portátil del equipo | Inmediato | Suspensión, wifi del evento, batería; hay que llevárselo encendido |
| Contenedor de una sesión cloud | Ya existe | Efímero: se recicla por inactividad. Solo como **reserva**, nunca como principal |

Propuesta: principal en VPS o portátil enchufado, reserva en una sesión cloud con relevo automático por latido.

## 9. Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| Caída o cuelgue durante la final | Supervisor, reserva por latido, luz de estado vigilada, guía de actuación |
| Producto agotado entre el cálculo y la evaluación | Refresco de stock ≤0,5 s, validador final, medir `invalid` en práctica (H11) |
| Sobreajuste a la práctica | Variables sin IDs, validación en segmento frío, A/B con significación |
| Duplicar respuestas (dos procesos escribiendo) | Un único escritor; el resto en modo sombra; relevo solo por latido perdido |
| Límite de peticiones compartido en `shop` | Pocas consultas incrementales y agregadas; nada pesado en el camino caliente |
| No saber explicarlo | Modelo interpretable por familias de variables, informe forense, panel |
| Repo público visible para rivales | Repo privado hasta el domingo o no subir el modelo entrenado |
