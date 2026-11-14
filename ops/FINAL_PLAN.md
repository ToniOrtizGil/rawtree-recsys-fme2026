# Plan final · domingo 15‑nov · `team_36`

Escrito el sáb 14‑nov ~19:00 CET. Complementa `HANDOFF.md` y `ops/RUNBOOK.md` (que siguen valiendo).

## Qué hay en marcha ahora (sábado noche)
- Servidor en el portátil (`nohup ops/run.sh`, `caffeinate`), desde una **copia congelada del código**
  (`releases/…`): **A/B de 3 brazos puros** baseline / v1 / v2 (≈33 % cada uno, sal `ab-night-2`), sin
  exploración. v2 = `artifacts/ranker_v3.txt`.
- **Ventana de decisión fijada de antemano: 18:09:13 → 04:00 UTC** (`ops/final.env`). Antes de las
  04:00 UTC el informe solo sirve para ver la salud del sistema, no para decidir.
- El portátil debe seguir **enchufado, con la tapa abierta y con red**.

```bash
set -a; . ops/final.env; set +a; .venv/bin/python ops/ab_report.py --since $AB_SINCE
```

## Regla de decisión (acordada el 14‑nov, `rtrec/live/decide.py`)
- Se excluyen las respuestas de NUESTRO respaldo (modelo no cargado).
- Principal: **Beta‑Binomial** P(B convierte más que A). Comprobación: **bootstrap por comprador**
  (`user_id`). Clics: se informan como señal secundaria.
- Se parte de lo más sencillo (baseline → v1 → v2). Uno más complejo sustituye al elegido solo si
  P_beta ≥ 0,95 **y** P_boot ≥ 0,90. Si no, se queda el más sencillo.
- Tamaño: el informe da las peticiones por brazo necesarias (+30 % ≈ 33.000).

## Regla del reentreno (no hay tiempo de validarlo en vivo)
- 05:00 CET: `ops/final_refit.sh` descarga el flujo de práctica, **aparta las últimas 2 h** (nunca se usan
  para entrenar ni elegir), reentrena con la ventana más reciente y lo compara con el modelo actual en
  ese tramo (`scripts/compare_models.py`, bootstrap por comprador).
- Entra solo si P(acierto de clic mejor) ≥ 0,95 y P(acierto de compra mejor) ≥ 0,50 → escribe
  `artifacts/ranker_final.ok`. Si no, se queda el modelo actual.

## Resultados offline (histórico real, test = último 20 % sin tocar, 50.000 impresiones)
| política | IPS cond. (compras/100) | IPS hueco 1 | clic en top‑10 |
|---|---|---|---|
| baseline | 0,53 | 0,35 | 38,5 % |
| v1 heurística | 1,03 | 0,42 | 29,5 % |
| **v2 (LightGBM + KNN de sesión)** | **2,36** | **1,04** | **61,9 %** |

AUC de validación 0,957. Variables con más peso: historia del usuario (carritos, afinidad, estilo de
descripción), `sknn` (KNN de sesión), conversión y tendencia de subcategoría.
**Corrección por propensity (v4, pesos min(1/p, 20)):** IPS cond. 2,21 vs 2,36, hueco 1 1,20 vs 1,04,
clic top‑10 62,3 % vs 61,9 %, SNIPS 6,16 vs 6,09 (±1,8): sin ganador claro → se queda la v3 (más sencilla).
**Límite conocido:** el histórico casi no tiene usuarios nuevos (15 de 50.000), así que el cold start
se mide en vivo (≈30 % de usuarios nuevos en el flujo) — columna `new buy/100` del A/B — y el
reentreno final incluye el flujo de práctica para que el modelo vea ese régimen.

## Domingo, hora a hora (CET)
| hora | qué | cómo |
|---|---|---|
| 05:00 | **Reentreno + filtro** (automático, no toca el servidor) | `ops/final_refit.sh` → `logs/final_refit.log` |
| 06:30 | **Cambio a un solo modelo** (automático): regla Beta‑Binomial sobre la ventana fijada; v2 usa `ranker_final` solo si pasó el filtro | `ops/go_final.py` → `logs/go_final.log` |
| 06:40 | **Comprobar**: `policy ... live`, 100 % válidas, p99 < 2 s, ningún `DEGRADED` | `.venv/bin/python ops/live_report.py --minutes 5` |
| 07:00 | Prueba de reinicio: `pkill -f "rtrec.runtime.serve"` → vuelve en ~20 s (caché) | log |
| **09:00** | **Congelar.** No tocar código, no reentrenar, no lanzar nada pesado en el portátil | |
| 10:00–12:00 | Final. Vigilar `live_report --minutes 5` cada ~15 min. **No reiniciar** salvo caída | |
| después | Presentación · pedir que **anulen la clave** de `team_36` | |

Programar los dos pasos automáticos (una vez, en la carpeta del repo): `ops/schedule_sunday.sh`.
Si algo de 05:00/06:30 falla, el servidor del sábado sigue sirviendo (A/B baseline/v1/v2): es válido para la
final. Para pasar a mano a un solo modelo:
```bash
pkill -TERM -f rtrec.runtime.supervisor
RTREC_POLICY=rtrec.model.policies:RankerPolicy RTREC_MODEL=artifacts/ranker_v3.txt nohup ops/run.sh >/dev/null 2>&1 &
```

## Lecciones del sábado (ya corregidas)
- **Caída por carrera en métricas** (`deque mutated during iteration`): ventanas con lock y el informe
  de estado ya no puede tumbar el servidor.
- **Reinicio lento** (170 s sin modelo): caché en disco `artifacts/cache/` (solo la usa el servidor; los
  tests la sobrescribían) → ~15 s.
- **Desajuste silencioso modelo/código**: el modelo guarda la huella de sus variables y del código que
  las calcula y el servidor se niega a cargarlo si no coincide; el servidor corre desde `releases/`;
  alerta `DEGRADED` + notificación si el respaldo sirve > 50 % durante 3 min.
- **No editar el código del modelo con el servidor en marcha**: si el supervisor reinicia, carga el
  código nuevo con el modelo viejo y sirve el respaldo.
- **No entrenar en la misma máquina durante la final**: con el entrenamiento en paralelo el p99 subió
  a 2,6 s y hubo un 7 % de respuestas perdidas.
