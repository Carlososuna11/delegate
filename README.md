# delegate

Delega trabajo de código acotado desde una sesión de Claude Code hacia modelos más baratos (DeepSeek, Qwen), sin que el modelo delegado pueda alcanzar tus credenciales ni tus datos de producción.

`delegate` corre el harness de Claude Code en modo headless contra el endpoint compatible con Anthropic de cada proveedor. El endpoint y la key se fijan solo en ese proceso, así que tu sesión principal no cambia. El environment del proceso se construye desde una allowlist, no se hereda. Por defecto la corrida es de solo lectura, sin hooks, sin tus settings de usuario y sin ningún servidor MCP. Cualquier permiso extra se pide por nombre en la línea de comandos.

```bash
delegate deepseek "revisa src/auth.ts y lista los error paths"
delegate deepseek --edit --bash "arregla el test roto de parser.test.ts y córrelo"
delegate qwen --timeout 2400 "segunda opinión sobre este diff: git diff main...feat"
```

## Para qué sirve

- **Repartir el trabajo por costo.** El modelo principal planifica y verifica. El trabajo mecánico (tests, refactors acotados, boilerplate, leer muchos archivos y resumirlos) lo hace un modelo más barato.
- **Delegar sin prestar tu máquina.** El modelo delegado no ve `GITHUB_TOKEN`, `AWS_*` ni ninguna variable que no esté en la allowlist. No carga tus MCP, tus hooks ni tus settings, y no escribe ni corre shell salvo que lo pidas.
- **Correr en paralelo y aislado** con `delegate-run`: cada tarea edita en su propio worktree, dentro de bubblewrap, y entrega el resultado como una rama para que la revises.

## Requisitos

- Linux. `delegate` usa `/proc` para verificar descriptores y procesos.
- Bash y coreutils.
- [Claude Code](https://docs.anthropic.com/claude-code) (`claude`) en el `PATH`.
- `git`.
- Python 3, solo para `delegate-run`. Usa únicamente la biblioteca estándar.
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), opcional y recomendado: `delegate-run` lo usa para aislar al modelo y a los checks.
- Una API key de cada proveedor que vayas a usar.

No hay dependencias de runtime que instalar. Probado en Ubuntu 24.04 con Bash 5.2, Python 3.12, git 2.43 y bubblewrap 0.9.

## Instalación

```bash
git clone https://github.com/Carlososuna11/delegate.git
cd delegate
mkdir -p ~/.local/bin
ln -s "$PWD/bin/delegate" "$PWD/bin/delegate-run" ~/.local/bin/
ln -s "$PWD/bin/delegate" ~/.local/bin/dlg   # abreviación opcional
```

`delegate` resuelve su propio directorio a través del symlink, así que el repo puede vivir donde quieras.

Agrega la key de cada proveedor. Se pide sin eco y se guarda en un archivo con modo 600:

```bash
delegate account deepseek add personal
delegate account qwen add personal
delegate providers        # proveedores configurados, su modelo y sus permisos
delegate account deepseek list
```

## Uso

```text
delegate <proveedor> [opciones] "tarea"       agente con herramientas
delegate <proveedor> chat [opciones] "texto"  una pregunta, sin herramientas
delegate account <proveedor> <acción>         cuentas (API keys)
delegate mcp list                             servidores MCP declarados
delegate providers                            proveedores configurados
delegate help
```

### Permisos

Pide siempre el acceso más angosto que alcance para la tarea:

| Flag | Qué agrega |
|---|---|
| (ninguna) | Solo lectura: leer archivos y un shell de lectura (`grep`, `git log`, `git diff`, …) |
| `--edit` | Escritura de archivos |
| `--bash` | Shell sin restricción |
| `--mcp <id>` | Las herramientas de lectura de ese servidor MCP |
| `--mcp-write <id>` | También las de escritura de ese servidor |

`--mcp` y `--mcp-write` se pueden repetir. Sin ellos, la corrida no alcanza ningún MCP.

### Otras opciones

| Opción | Uso |
|---|---|
| `--account <nombre>` | Usa la key de esa cuenta sin tocar la activa. Úsala en pipelines: la cuenta activa es global y otra sesión puede cambiarla |
| `--dir <ruta>` | Directorio de trabajo (por defecto, el actual) |
| `--model <id>` | Cambia el modelo del proveedor para esta corrida |
| `--timeout <s>` | Límite en segundos (por defecto, el del proveedor) |
| `--json` | Salida JSON con el resultado, el costo y la sesión |

### Usarlo desde Claude Code

Lo más cómodo es un subagente que envuelva `delegate`. Por ejemplo, `~/.claude/agents/deepseek.md`:

```markdown
---
name: deepseek
description: Hand a well-scoped coding or reading task to DeepSeek through delegate. Not for secrets, customer data or production.
tools: Bash
---
Run the task with `delegate deepseek [--edit] [--bash] "<task>"` from the repo, using the
narrowest flags that fit. Return its result verbatim, then one line with what you verified.
```

## Proveedores

Cada proveedor es un archivo `clave<TAB>valor` en `providers.d/`. El repo trae `deepseek` y `qwen`:

```text
endpoint	https://api.deepseek.com/anthropic
auth_env	ANTHROPIC_API_KEY
key_dir	~/.config/deepseek
model	deepseek-flash[1m]
small_model	deepseek-flash
timeout	1800
caps	read,edit,bash,mcp,mcp-write
```

- **Claves:** `endpoint`, `auth_env`, `key_dir`, `key_pattern`, `default_account`, `model`, `small_model`, `context_tokens`, `timeout`, `caps` y `residency`.
- **Validación:** el parser rechaza claves desconocidas o duplicadas.
- **`auth_env`:** solo acepta `ANTHROPIC_API_KEY` (se manda como `x-api-key`) o `ANTHROPIC_AUTH_TOKEN` (como `Authorization: Bearer`).
- **Otra carpeta:** con `DELEGATE_PROVIDERS_DIR` usas una carpeta de proveedores distinta.

Para agregar un proveedor, basta con un archivo nuevo, siempre que tenga un endpoint compatible con la API de Anthropic.

## Servidores MCP

Ningún servidor MCP está disponible salvo que lo declares y lo pidas por nombre en la corrida:

1. **Declararlo.** Cada servidor es un archivo en `~/.config/delegate/mcp.d/` (o en `$DELEGATE_MCP_DIR`), con el mismo formato que los proveedores. En `examples/mcp.d/` hay ejemplos de un servidor `http` (Figma) y uno `stdio` (Context7).
2. **Pedirlo.** En la corrida usas `--mcp <id>` para leer o `--mcp-write <id>` para escribir.

Reglas de la declaración:

- `tools_read` es obligatoria. `tools_write` es opcional.
- Un servidor `http` o `sse` solo acepta URLs `https`.
- Un servidor `stdio` necesita además la línea `trust local-exec`, porque es un binario que corre con los privilegios del proceso delegado.
- `env_passthrough` lista **nombres** de variables, nunca valores.
- `rules` agrega instrucciones al prompt de la corrida solo cuando ese servidor está habilitado. `delegate mcp list` las muestra completas para que las revises.

## delegate-run: corridas aisladas y en paralelo

`delegate-run` envuelve a `delegate` para tareas que **editan**. Recibe una petición JSON por stdin, corre la tarea en un worktree aislado, mide qué cambió y corre los checks que pediste. El resultado queda en la rama `delegate/<run_id>`.

```bash
cat > peticion.json <<'EOF'
{
  "v": 1,
  "provider": "deepseek",
  "repo": "/ruta/absoluta/al/repo",
  "task": "Agrega tests para parse_date en src/dates.py, siguiendo test/test_numbers.py",
  "grants": ["edit", "bash"],
  "scope": ["src/dates.py", "test/**"],
  "checks": [["python3", "-m", "pytest", "test/test_dates.py", "-q"]],
  "timeout": 1800
}
EOF
delegate-run < peticion.json      # imprime result.json; exit 0 solo si quedó "completed"
```

```text
delegate-run status <run_id>
delegate-run list [--limit N]
delegate-run cancel <run_id>
delegate-run cost [--days N]
delegate-run stats [--days N] [--provider P] [--json]
delegate-run gc [--days N] [--dry-run]
```

Un resultado se acepta solo si:

1. `status` es `completed`;
2. `scope_violation` está vacío;
3. leíste tú mismo el diff de `delegate/<run_id>`;
4. volviste a correr los checks.

El `report` del modelo es evidencia, no una autorización.

**Sandbox.** Con `bwrap` disponible, el proceso delegado y los checks corren en bubblewrap:

- `/usr` y `/etc` en solo lectura;
- `HOME` y `/tmp` vacíos;
- el worktree en escritura;
- la historia de git en solo lectura;
- ningún archivo de keys montado.

`DELEGATE_SANDBOX` acepta `auto` (por defecto), `bwrap` (falla si no hay sandbox) u `off`.

**Costo.** `cost` y `stats` calculan el gasto con `lib/delegate_run/prices.tsv`, que tiene precios copiados de la página oficial de cada proveedor, con su fuente y su fecha. `docs/medir-delegacion.md` explica cada columna de `stats`.

### En otro host

```bash
scripts/install-remote.sh <host>                 # instala el commit actual por git bundle
delegate-run --remote <host> < peticion.json     # misma petición, corre allá
delegate-run fetch <run_id> --remote <host>      # recupera una corrida que quedó pendiente
```

El cliente empuja la base a un mirror en el host y le manda la petición por ssh. Luego trae la rama de vuelta: nunca mueve una rama local que ya exista. El host usa sus propias keys de delegación.

## Seguridad

El diseño completo, con cada invariante y el ataque que lo motivó, está en [`CLAUDE.md`](CLAUDE.md). Lo esencial:

- **La key nunca va en argumentos ni en literales de shell.** Se lee de un archivo con modo 600 dentro de un directorio con modo 700. Viaja por un pipe hasta el proceso hijo, así que nunca aparece en `ps` ni en `/proc/<pid>/cmdline`.
- **El environment del hijo se construye desde una allowlist.** No se hereda con exclusiones.
- **Ningún comando imprime una key.** Como mucho muestra una huella (un prefijo del sha256).

### Lo que no promete

- **Una corrida de solo lectura puede leer cualquier archivo que puedas leer tú, y mandarlo al proveedor.** `--dir` no confina. Nunca apuntes una corrida a un árbol con secretos.
- **El `git` limitado del modo solo lectura es una defensa extra, no una barrera.** Anula los mecanismos conocidos con los que un repo hace que git ejecute programas. Aun así, no delegues sobre repos en los que no confías.
- **No hay locking de cuentas.** El mismo usuario puede cambiar sus keys mientras corre una delegación.

### Qué no delegar

Secrets, datos personales de clientes, datos o consultas de producción, ni decisiones de dinero, seguridad o compliance. Al proveedor le llega el prompt y todo archivo que el agente lea en el directorio de trabajo.

## Tests

```bash
test/run.sh              # todas las suites: Bash y Python
test/run.sh config       # una sola suite
test/py.test.sh          # solo delegate-run
```

`delegate-run` se prueba con un `delegate` falso y el modo remoto con un `ssh` falso. Las pruebas contra el proveedor real corren solo con `DELEGATE_REAL_HARNESS=1` y gastan créditos.

## Licencia

[MIT](LICENSE).
