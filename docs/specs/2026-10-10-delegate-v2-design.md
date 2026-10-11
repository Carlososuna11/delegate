# delegate v2 — diseño

Fecha: 2026-10-10. Rama: `v2`.

## Objetivo

Que `delegate` sea la columna de la delegación en paralelo, en la laptop y en una
segunda máquina por SSH, sin aflojar ninguna invariante de `CLAUDE.md`. Hoy un exit 0
no prueba que la tarea se hizo, dos corridas con `--edit` sobre el mismo árbol se pisan,
y una review adversarial (Codex `gpt-6-astra`, xhigh) encontró 12 defectos, varios
reproducidos.

## Fuera de alcance

Todo lo de "Lo que NO se construye" sigue igual: sin daemon, sin locking de cuentas,
sin base de datos de costos, sin DSL de políticas, sin enrutamiento automático entre
proveedores. Los slots de concurrencia son de **corridas**, no de cuentas, y el costo se
agrega leyendo archivos de resultado, no una base.

## Fase A — defectos

Orden por riesgo práctico. Cada uno entra con un test que falla contra el código
actual.

| # | Defecto | Arreglo |
|---|---|---|
| A1 | Git ejecuta programas desde el piso de solo lectura (`--ext-diff`, `--textconv`, `diff.external`, `core.fsmonitor`, `core.pager` de la config del repo) | En el piso, `git` corre con `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` y `-c` que anulan `diff.external`, `core.fsmonitor`, `core.pager`, `core.hooksPath`, y deniega `--ext-diff`, `--textconv` y `--output` en todas sus formas. Se documenta que es defensa en profundidad: el límite real sigue siendo "no delegar sobre árboles hostiles". |
| A2 | `GIT_CONFIG_*`, `GIT_CONFIG_PARAMETERS` y familia pasan el guardián de passthrough MCP | Se agregan a los nombres rechazados (`GIT_*` completo salvo una allowlist vacía por ahora). |
| A3 | `PATH` heredado: entradas vacías o relativas permiten que el repo revisado gane sobre `grep`, `git`, `timeout` y `claude` | `claude_bin`, `timeout` y `grep` se resuelven a ruta absoluta **antes** de cualquier `cd`. El `PATH` del hijo se construye: stubs + directorios absolutos del `PATH` del padre, descartando vacíos, relativos y cualquiera dentro de `--dir`. |
| A4 | El texto de la tarea puede ser una opción de `claude` (`-- --help` sale 0 sin hacer nada) | `--` antes del prompt posicional. `--append-system-prompt` sigue siendo la última opción. |
| A5 | Un resolver de permisos o MCP que rechaza conserva grants de una llamada anterior | Ambos resolvers vacían sus globals al entrar. |
| A6 | El runner de tests ignora el exit code de cada test; los tests de rechazo inspeccionan globals de un subshell | El runner cuenta como fallo un exit distinto de 0. Los tests de rechazo hacen éxito-luego-rechazo en el **mismo** shell. |
| A7 | `DEEPSEEK_ACCOUNT` está documentado y se ignora | Se rechaza con error explícito que nombra `DELEGATE_ACCOUNT`. Cambiar en silencio qué key se factura es peor que fallar. |
| A8 | Arranque: `BASH_ENV`, función `set` exportada, `allexport`/`SHELLOPTS` heredados actúan antes de la limpieza | El shebang pasa a `#!/bin/bash -p` (privileged: no importa funciones ni lee `BASH_ENV`/`ENV`; ruta fija, no `env`). Primera línea útil: `set +a +x +v`. Test por cada vector, con sentinelas. |
| A9 | `compgen -e` no ve entradas del environ con nombres no válidos para Bash (`SECRET-TOKEN`) | El hijo se lanza con `env -i` y solo lo nombrado; un test con `SECRET-TOKEN` y `A.B`. |
| A10 | El `grep` que filtra stderr arranca antes del wipe y hereda todo el environment | El filtro se lanza después del wipe, o con `env -i PATH=…` propio. |
| A11 | Carreras al validar y crear keys | `account add` crea con `set -C` (noclobber) sobre un temporal privado en `key_dir` y lo publica con `mv -T` sin reemplazo; `key_dir` se valida propio y 700 en cada uso. La lectura valida y lee el mismo descriptor (`exec {fd}<`, y se valida `/proc/self/fd/$fd`). |
| A12 | Los stubs se reescriben en un directorio compartido y siguen symlinks | Directorio de stubs privado por corrida (`mktemp -d`), borrado al salir. Se agregan `deepseek-agent`, `qwen-agent`, `opencode`, `gemini` y `dlg`. La invariante 5 se reescribe honesta: bloquea la invocación por nombre, no un binario por ruta absoluta con `--bash`. |
| A13 | `key_pattern` y `default_account` pueden chocar con los nombres de metadata | El validador rechaza una ruta de cuenta que resuelva a `api_key` o al archivo de metadata. |

## Fase B — `delegate-run`

Un ejecutable nuevo, en Python 3 sin dependencias, que **envuelve** a `delegate` y no lo
reemplaza. `delegate` sigue siendo el primitivo de `exec` en Bash; el JSON estructurado
es justo el caso en el que `CLAUDE.md` dice que Python conviene.

### Petición

JSON por stdin, nunca por argv (sirve igual local y por `ssh -T`):

```json
{
  "v": 1,
  "provider": "deepseek",
  "account": null,
  "repo": "/ruta/al/repo",
  "base": "HEAD",
  "grants": ["edit", "bash"],
  "mcp": [],
  "timeout": 1800,
  "task": "texto de la tarea",
  "checks": [["npm", "test", "--", "src/auth"]],
  "scope": ["src/auth/**"]
}
```

`base` se resuelve a un commit al recibir la petición. Un árbol sucio no se usa en
silencio: el runner lo rechaza salvo `"snapshot": true`, que hace commit del estado
actual en una ref temporal y la usa de base.

### Ciclo

1. Asigna `run_id` (`YYYYMMDD-HHMMSS-<6 hex>`) y crea `~/.local/state/delegate/runs/<run_id>/` con umask 077. Escribe `request.json`.
2. Toma un slot (`flock` sobre `slots/N`, `DELEGATE_SLOTS` por defecto 2). Sin slot libre, espera; `"wait": false` lo hace fallar con `busy`.
3. `git worktree add --detach worktrees/<run_id> <base>`.
4. Lanza `delegate <provider> --json [grants] --dir <worktree> --timeout … -- <task>` en su propio grupo de procesos. Guarda stdout y stderr crudos.
5. Timeout o `delegate-run cancel <run_id>`: SIGTERM al grupo, 15 s de gracia, SIGKILL.
6. Calcula **él mismo** los cambios: `git diff --binary <base>` más los no rastreados, y los compara con `scope`. Archivos fuera del scope marcan `scope_violation`.
7. Corre cada check de la petición en el worktree, con timeout, y guarda su log. Los checks vienen de la petición, nunca del árbol que editó el delegado.
8. Hace commit de los cambios en `delegate/<run_id>` (autor `delegate <provider>`); el delegado no hace commits.
9. Escribe `result.json` de forma atómica y lo imprime en stdout.

### Resultado

```json
{
  "v": 1,
  "run_id": "…",
  "status": "completed",
  "child_exit": 0,
  "provider": "deepseek", "model": "…", "account": null,
  "base": "<sha>", "branch": "delegate/<run_id>", "worktree": "…",
  "grants": ["edit", "bash"],
  "changed_files": [{"path": "src/a.ts", "change": "M"}],
  "scope_violation": [],
  "checks": [{"cmd": ["npm", "test"], "exit": 0, "log": "checks/0.log", "seconds": 41}],
  "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
  "cost_usd_estimate": null,
  "report": "texto final del delegado",
  "artifacts": "~/.local/state/delegate/runs/<run_id>",
  "error": null, "started_at": 0.0, "finished_at": 0.0
}
```

`status`: `completed` (exit 0 y todos los checks en 0), `checks_failed`, `task_failed`
(exit distinto de 0), `rejected` (`delegate` salió con 2: flags o config), `timed_out`,
`cancelled`, `busy`, `infra_failed` (git, disco, binario), `invalid_result` (la salida
JSON del hijo no se pudo leer). Exit code del runner: 0 solo con `completed`.

`usage` sale del JSON del harness; si falta, `null`, nunca 0. El costo se estima con
`prices.tsv` (proveedor, modelo, precio por millón de entrada, salida y caché, fecha de
la tarifa) y se marca como estimación. `delegate-run cost [--days N]` suma los
`result.json`.

### Otros subcomandos

`delegate-run status <run_id>`, `delegate-run list`, `delegate-run cancel <run_id>`,
`delegate-run gc --days N` (borra worktrees y ramas de corridas terminadas; nunca de
una corrida viva).

### Decisiones de implementación

Lo que cambió respecto de lo de arriba al implementarlo, y por qué. El código y
`CLAUDE.md` ("delegate-run") son la referencia de uso.

- **Sin `account_fingerprint`.** Calcularla obliga a abrir la key, y el runner no la
  abre nunca. El resultado trae `account`: el nombre pedido, o `null` si la eligió
  `delegate` (cuenta por defecto o activa).
- **Árbol sucio.** Cuenta como sucio también un árbol con solo archivos nuevos (con
  `--untracked-files=all`, sin importar `status.showUntrackedFiles`). Solo importa
  cuando la base es el commit actual: con otra base el árbol no se lee. El snapshot no
  es `git stash create` (ignora los no rastreados): usa un índice temporal, incluye los
  nuevos, respeta `.gitignore` y queda en `refs/delegate/snapshot/<run_id>`. Con otra
  base que no sea `HEAD`, `snapshot` se rechaza.
- **`rejected`** cubre también lo que el runner rechaza antes de lanzar al hijo (repo
  que no es un árbol de git, base que no resuelve, árbol sucio); ahí `child_exit` es
  `null`. Una petición mal formada no crea corrida: exit 2 y stdout vacío.
- **`timed_out`** también cuando `delegate` sale con 124 (su propio `--timeout`);
  **`task_failed`** también cuando el JSON del harness dice `is_error: true` con exit 0.
- **Orden del ciclo.** Los cambios se miden y se congelan en un árbol (`write-tree`)
  **antes** de los checks, y ese árbol es el que se commitea: lo que escriba un check
  no llega a la rama. El commit es plumbing (`commit-tree` y un `update-ref` que solo
  crea), sin hooks ni firma, y sin cambios no hay rama.
- **Scope.** Globs por segmentos, no `fnmatch` (donde `src/*.ts` calzaba
  `src/x/y.ts`): `*` y `?` no cruzan `/`, `**` sí, y una ruta sin comodín cubre lo que
  tiene debajo. El diff es `-z` y `--no-renames`, así que los dos extremos de un rename
  pasan por el scope y un nombre con salto de línea es una sola ruta.
- **Procesos.** El grupo no alcanza: `delegate` corre el harness bajo `timeout`, que se
  muda a otro grupo, y un nieto puede hacer `setsid`. El runner se hace subreaper
  (`prctl`), deja al líder como zombie (`waitid` con `WNOWAIT`) para que su pid no se
  recicle mientras limpia, y manda TERM, gracia y KILL al árbol completo más la sesión.
  También limpia lo que quede después de una salida limpia.
- **Cancelación** por un archivo marcador en el directorio de la corrida, que el runner
  sondea; no se manda ninguna señal entre procesos por un pid guardado. TERM e INT al
  runner cuentan como cancelación y HUP se ignora (Fase C: un corte de ssh no mata la
  corrida).
- **Vida de la corrida.** Un `flock` por corrida, tomado antes de escribir nada y
  soltado después del `result.json` final. `status` y `list` derivan `abandoned`
  (dice `running` y nadie tiene el lock) y `unreadable`; `gc` toma ese lock exclusivo
  antes de decidir, y recoge una abandonada solo pasadas 5 horas (timeout máximo más
  una hora), cuando el `--timeout` de `delegate` ya cortó lo que dejó.
- **Checks** con un environment construido (`HOME`, `PATH` filtrado, locale, `TMPDIR`) y
  un `PATH` sin entradas relativas ni dentro del worktree: corren código del modelo, y
  los tokens de quien llama no tienen por qué verlo.
- **Costo.** `total_cost_usd` del harness se ignora: pone precio de Anthropic a modelos
  que no lo son (el harness mismo marca `costBasis: unknown`). Se estima por modelo con
  `prices.tsv`; un modelo sin fila o un conteo ausente dejan `null`. El
  `prices.tsv` de `~/.local/state/delegate/` gana sobre el del repo, que va sin precios.
- **Perilla de tests.** `DELEGATE_RUN_WALL` reemplaza `timeout + 30 s`;
  `DELEGATE_RUN_GRACE` y `DELEGATE_RUN_SLACK` acortan la gracia y el margen.

Límites conocidos:

- Un SIGKILL al runner no se puede atrapar: el hijo sigue hasta el `--timeout` de
  `delegate` y la corrida queda `abandoned`.
- Con `bash`, el delegado alcanza el `.git` compartido (refs, config) y cualquier
  archivo del usuario; con `edit`, lo que permita el harness. El diff mide el
  worktree, no la máquina. Los archivos ignorados no se miden ni se commitean.
- Con Claude Code 2.1.295 el harness responde `No such tool available: Write` aunque
  `delegate --edit` lo otorga: una corrida con `edit` y sin `bash` puede editar archivos
  existentes pero no crear nuevos. Es un defecto de `delegate` frente a esa versión del
  harness, no de `delegate-run`; el smoke real usa una edición por eso.

### Cómo verifica quien llama

Los subagentes `deepseek` y `qwen` pasan a usar `delegate-run` cuando la tarea edita.
Quien llama acepta un resultado solo si: `status` es `completed`, `scope_violation`
está vacío, revisa el diff de la rama él mismo, y vuelve a correr los checks antes de
integrar. El `report` del modelo es evidencia a investigar, no autorización.

## Fase C — remoto (subproyecto caracas)

Solo la interfaz, para que la fase B no la cierre:

- En caracas, un mirror bare por repo: `~/delegate/repos/<nombre>.git`.
- La laptop empuja la base a `refs/delegate/base/<run_id>`, manda la petición por `ssh -T caracas delegate-run` (stdin) y luego hace `git fetch` de `delegate/<run_id>`.
- Si se cae la conexión, se consulta `delegate-run status <run_id>` antes de reintentar: un corte de transporte no significa que la corrida no ocurrió.
- caracas tiene sus propias keys de delegación, no las de la laptop, y ninguna credencial de producción ni de Claude.

## Tests

- Fase A: un test por fila de la tabla, que falla contra `main`.
- Fase B: tests de Python con `unittest` y un `delegate` falso (`test/py/fake_delegate.py`) que simula éxito, fallo, rechazo, timeout que ignora SIGTERM, un nieto con `setsid` que sobrevive, JSON roto, edición fuera de scope, renombres, nombres con salto de línea y archivos no rastreados. `test/py/test_cli.py` maneja `bin/delegate-run` de punta a punta.
- Una suite chica contra el harness real, opcional (`DELEGATE_REAL_HARNESS=1`), que prueba que el piso de solo lectura deniega escribir y que `--` corta el parseo.

## Review

Al cerrar cada fase, review adversarial con Codex `gpt-6-astra` xhigh sobre el diff de
la fase.
