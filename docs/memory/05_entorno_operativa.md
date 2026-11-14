# Entorno, operativa y sesión máster

## Esta sesión (máster)

- ID: `session_01XduZa73MPSwYUepqrdFyRP` · entorno cloud `Default` (`env_01JwXiBpBQ4t1bLVfx7YJcTw`), personal.
- Contenedor efímero: 4 CPU, 15 GB RAM, ~30 GB de disco. **Se recicla tras inactividad** ⇒ todo lo importante debe estar en git. No es un sitio fiable para correr el sistema de producción.
- Directorio de trabajo: `/home/user/datathon/` (`sources/` = fuentes, `memory/` = esta memoria).

## Red

- Política "limited" + gestores de paquetes. Hosts añadidos el 14-nov (aprobado por el usuario): `rawtree.com`, `datathon.rawtree.dev`.
- **Pendiente**: `api.rawtree.com` (imprescindible para todo). Se habilita al guardar la API key con el formulario seguro (`request_credential`), que además inyecta la clave en las peticiones a ese host desde todas las sesiones del entorno. Con ese mecanismo el código puede enviar una clave ficticia: el proxy la sustituye.
- `github.com` y PyPI funcionan.
- Las sesiones hijas heredan el entorno `Default` (misma red y credencial).

## Sesiones hijas (agentes)

- Se crean con `create_session` (heredan entorno; cada una tiene **su propio contenedor**, no comparten disco con la máster).
- Por tanto el contexto compartido viaja por: (a) el prompt inicial, (b) **un repositorio git compartido** (ramas `agent/<nombre>`), (c) `team_NN.scratch` en RawTree.
- Los agentes informan a la máster con `send_message` al ID de arriba; la máster vigila con `list_sessions(children=true)` / `get_session` / `list_events`.
- La máster integra en `main`, decide qué versión va a producción e informa al usuario indicando si algo requiere acción suya.
- No crear pull requests salvo que el usuario lo pida: se trabaja con ramas y la máster fusiona.

## Repositorio del equipo

- **`ToniOrtizGil/rawtree-recsys-fme2026`** (privado), creado por el usuario el 14-nov. `main` = integración (solo la máster). Ramas `agent/aN-*`.
- `aitorboligecubas/Datathon-26` NO es el repo de este reto.
- La sesión no puede crear repos en GitHub: los crea el usuario y la máster los engancha con `add_repo`.

## Sesiones-agente activas (lanzadas 14-nov 12:37 CET)

| Agente | Sesión | Rama |
|---|---|---|
| A1 Runtime | `session_01XduZa7bWed8icL7CDD4hBu` | `agent/a1-runtime` |
| A2 Datos y simulador | `session_01XduZa7ZveaS8oXNcVTnrEA` | `agent/a2-data` |
| A3 Modelo | `session_01XduZa6oWCiBF3WijBEYKsJ` | `agent/a3-model` |
| A4 Aprendizaje en vivo | `session_01XduZa5VmpfkUiCyUonCiDp` | `agent/a4-live` |
| A5 Observabilidad y demo | `session_01XduZa6RtBhjLfcHutNiT9H` | `agent/a5-demo` |
- No hay carpetas del ordenador del usuario conectadas por Remote Control (lista vacía).

## Acciones pendientes del usuario (bloquean la ejecución)

1. **OK al plan.**
2. **Credenciales** (EN ESPERA: el equipo duda entre dos retos y no pedirá la clave hasta decidir; mientras, todo se construye contra un simulador local): `TEAM_ID` (p. ej. `team_07`, se puede decir en el chat) y `RAWTREE_API_KEY` (**no pegar en el chat**; se introduce en el formulario seguro para `api.rawtree.com`).
3. ~~Repositorio del equipo~~ HECHO: `ToniOrtizGil/rawtree-recsys-fme2026`.
4. **Máquina de producción** que no se duerma (ver `06_plan_ganador.md` §8): VPS recomendado, o portátil enchufado con suspensión desactivada, idealmente con cable o red estable.

## Reloj

- Sesión iniciada 14-nov 11:10 UTC (12:10 CET). La práctica lleva abierta desde las 10:00 CET.
- Final: 15-nov 09:00–11:00 UTC (10:00–12:00 CET). Congelación de código prevista 08:00 UTC.

## Decisiones de integración (máster)

- 14-nov 12:52 CET — **Un solo hilo de servicio es dueño del `State`** (diseño de A1): aplica las actualizaciones de catálogo justo antes de cada lote y ejecuta los hooks entre lotes, así que no hacen falta locks. El `CatalogTracker` de A1 (`rtrec/runtime/catalog.py`) es el **único escritor** de `state.catalog`, `state.available` y `state.new_products`. A4 solo escribe features derivadas en `state.live` y se engancha mediante `rtrec/live/hook.py:LiveHook` (hooks `on_start`, `on_catalog_updates`, `on_events`, `on_feedback`, `on_served`, `on_tick`). Sustituye a la decisión de las 12:46 (stock en A4).
- Promoción a producción: `scripts/arbiter.py` (ver `docs/TASKS.md`).
- Fuga de fiabilidad conocida: productos que se agotan entre nuestra respuesta y la evaluación del organizador (~1 % en el simulador de A1). Lo mide el test de carga de A1.
