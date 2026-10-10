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
  "provider": "deepseek", "model": "…", "account_fingerprint": "ab12cd",
  "base": "<sha>", "branch": "delegate/<run_id>", "worktree": "…",
  "grants": ["edit", "bash"],
  "changed_files": [{"path": "src/a.ts", "change": "M"}],
  "scope_violation": [],
  "checks": [{"cmd": ["npm", "test"], "exit": 0, "log": "checks/0.log", "seconds": 41}],
  "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
  "cost_usd_estimate": null,
  "report": "texto final del delegado",
  "artifacts": "~/.local/state/delegate/runs/<run_id>"
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
- Fase B: tests de Python con `unittest` y un `claude` falso (como `test/isolation.test.sh`) que simula éxito, fallo, timeout que ignora SIGTERM, JSON roto, edición fuera de scope y archivos no rastreados.
- Una suite chica contra el harness real, opcional (`DELEGATE_REAL_HARNESS=1`), que prueba que el piso de solo lectura deniega escribir y que `--` corta el parseo.

## Review

Al cerrar cada fase, review adversarial con Codex `gpt-6-astra` xhigh sobre el diff de
la fase.
