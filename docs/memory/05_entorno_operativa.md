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

## Repositorios visibles para el usuario

- `aitorboligecubas/Datathon-26` (público, con permiso de push, último push 14-nov 11:18 UTC) ← candidato a repo del equipo. **Confirmar con el usuario.**
- Otros repos personales sin relación.
- No hay carpetas del ordenador del usuario conectadas por Remote Control (lista vacía).

## Acciones pendientes del usuario (bloquean la ejecución)

1. **OK al plan.**
2. **Credenciales**: `TEAM_ID` (p. ej. `team_07`, se puede decir en el chat) y `RAWTREE_API_KEY` (**no pegar en el chat**; se introduce en el formulario seguro para `api.rawtree.com`).
3. **Repositorio del equipo** para el código (¿`aitorboligecubas/Datathon-26` u otro?). Aviso: si es público, otros equipos pueden ver el código durante el evento.
4. **Máquina de producción** que no se duerma (ver `06_plan_ganador.md` §8): VPS recomendado, o portátil enchufado con suspensión desactivada, idealmente con cable o red estable.

## Reloj

- Sesión iniciada 14-nov 11:10 UTC (12:10 CET). La práctica lleva abierta desde las 10:00 CET.
- Final: 15-nov 09:00–11:00 UTC (10:00–12:00 CET). Congelación de código prevista 08:00 UTC.
