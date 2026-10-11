# delegate — contexto

CLI para delegar trabajo de código acotado desde una sesión principal de Claude Code
hacia modelos más baratos (DeepSeek, Qwen), sin que el delegado pueda alcanzar datos de
producción.

Corre el harness de Claude Code en headless contra el endpoint Anthropic-compatible de
cada proveedor, fijándolo **solo en ese proceso**: la sesión principal no se toca.

## Invariantes

Son la razón de existir de la herramienta. Un cambio que debilite cualquiera de estos
está mal, aunque simplifique el código. Si uno estorba, se discute antes de tocarlo.

1. **La key nunca va como argumento ni en literal de shell.** Un argumento queda visible
   en `ps` para cualquier usuario de la máquina. Se lee de un archivo modo 600 y se pasa
   solo por el environment del proceso hijo. Ni siquiera como argumento de `env`:
   `env ANTHROPIC_API_KEY=... cmd` pone la key en el argv de `env`, y
   `/proc/<pid>/cmdline` es legible por cualquiera. Viaja por un pipe (fd 3, NUL
   separado, escrito por el `printf` builtin) hacia un trampolín `/bin/bash -p`
   arrancado con `env -i`, que la exporta para el hijo y lo supervisa. Corolarios, cada
   uno un camino de fuga que existió:
   - El shebang es `#!/bin/bash -p`: ruta fija y modo privilegiado. Lo que el shell que
     invoca controla actuaba **antes** de la primera línea del script: `BASH_ENV` se
     sourceaba, una función exportada llamada `set` corría dentro de la propia limpieza,
     y un `SHELLOPTS=xtrace` con un `PS4` que expandía `$ANTHROPIC_API_KEY` imprimía la
     key del padre. El modo privilegiado no lee `BASH_ENV` ni `ENV`, no importa
     funciones e ignora `SHELLOPTS`, `BASHOPTS`, `CDPATH` y `GLOBIGNORE` del environ.
     La primera línea útil es `set +a +x +v`, para `bash -a` y `bash -x`: `allexport`
     volvía a exportar la key a pesar del `local +x`, y `xtrace` trazaría `key=<la key>`
     al stderr que recoge `--json 2>&1`. El límite: invocado como `bash bin/delegate`
     el shebang no aplica y nada de esto vale; instalado y llamado por nombre, sí.
   - `local +x key`: un `local` que tapa una variable exportada del mismo nombre
     **conserva** el atributo de export, así que con `export key=...` en el shell que
     invoca, la key entraba al environment de todo helper posterior.
   - El environment del hijo se **construye** desde una allowlist. `env -u A -u B` deja
     pasar todo lo demás, así que `GITHUB_TOKEN`, `AWS_*` y `NODE_OPTIONS` llegaban a una
     corrida sin flags. Y se construye con `env -i`, no solo con el wipe: Bash no importa
     una entrada del environ cuyo nombre no es un identificador (`SECRET-TOKEN`, `A.B`,
     un `BASH_FUNC_x%%` que no importó), `compgen -e` no la ve, y Bash se la pasa intacta
     a cada hijo.
   - Las **funciones** exportadas: el modo privilegiado no las importa, y el script igual
     borra las que hubiera, por si lo corren como `bash bin/delegate`. Viven en el environ como `BASH_FUNC_<nombre>%%`, que `compgen -e` no lista y
     `unset -v` no toca, y una función importada **gana** sobre el binario del mismo
     nombre: con `export -f grep` en el shell padre, el piso de solo lectura —donde
     `Bash(grep:*)` está permitido— pasaba a ser ejecución arbitraria, y este script
     mismo ya estaba corriendo código ajeno antes de llegar al exec.
   - El wipe corre en una función **sin** los locals de `cmd_run` vivos. Dentro de una
     función, `unset -v key` borra el binding **local** y lo deja tapando el del
     llamador en vez de destaparlo (`export -n` se comporta igual), así que un
     `export key=...`, `export prompt=...` o `export model=...` del padre sobrevivía al
     wipe y llegaba al hijo. Por eso `cmd_run` publica en `_DG_X_*` y **retorna**, y el
     exec vive en `dg_exec_child`.
2. **La key se lee una sola vez**, justo después de validar argumentos y config. Una vez
   leída, un cambio de cuenta activa hecho por otro proceso no puede desviar la corrida
   en curso.
3. **Aislamiento: ningún MCP por defecto.** Sin hooks, sin plugins, sin settings de
   usuario, sin memoria y, sobre todo, sin servidores MCP: `--strict-mcp-config` sin
   `--mcp-config` deja al hijo sin alcanzar ninguno. Cada servidor se habilita por
   invocación y por nombre (`--mcp <id>`), con su propio allowlist de herramientas, y la
   escritura es un permiso aparte (`--mcp-write <id>`). Que un servidor esté declarado en
   la máquina no lo pone al alcance de nada. El delegado no alcanza bases de datos,
   Datadog, Sentry, Retool, Linear ni APIs de GitHub, salvo que alguien declare una
   entrada para eso y la pida explícitamente en esa corrida.
4. **Permisos graduados, y solo hacia arriba.** Solo lectura por defecto; `--edit` agrega
   escritura de archivos; `--bash` agrega shell sin restricción; `--mcp` y `--mcp-write`
   agregan las herramientas de un servidor. El techo de capacidades del proveedor
   (`caps`) gana sobre los flags: si no incluye `bash`, `--bash` se **rechaza**, no se
   ignora en silencio, y un rechazo no deja ningún global seteado, para que una corrida
   negada no pueda leerse como una más angosta.
5. **Otras CLIs de IA bloqueadas por nombre en el PATH del hijo.** `codex`, `claude`,
   `delegate`, `dlg`, `dg`, `deepseek-agent`, `qwen-agent`, `opencode` y `gemini` son
   stubs que salen con 126, para que un delegado no gaste cuota cara ni re-delegue.
   Lo que esto promete es exactamente eso: bloquea la invocación **por nombre**. Una
   corrida con `--bash` puede llamar un binario por su ruta absoluta o reescribir su
   `PATH`, y eso no lo impide nada aquí.

   Los stubs viven en un directorio privado por corrida (`mktemp -d`, 700, en
   `XDG_RUNTIME_DIR` o `TMPDIR`), que se borra al terminar. El directorio compartido
   de antes se reescribía con `>` en cada corrida, así que seguía un symlink
   precreado, y un ejecutable dejado ahí (`node`, `git`) corría antes que el real. El
   padre tiene que ser del usuario o de root, y solo escribible por su dueño o con
   sticky bit: quien puede escribir el padre puede renombrar el directorio de la
   corrida y poner el suyo, aunque este sea 700.

   Alguien tiene que esperar al hijo para borrar el directorio, y no puede ser el CLI:
   un proceso vivo conserva en `/proc/<pid>/environ` el environment con el que
   arrancó, aunque haga `unset`, y el CLI arrancó con las credenciales del padre. Por
   eso el CLI hace `exec` a un supervisor `/bin/bash -p` arrancado con `env -i` (mismo
   pid, environ vacío). El supervisor:
   - instala sus trampas antes de lanzar al hijo;
   - le reenvía TERM, INT y HUP, cada una como sí misma;
   - cierra todo descriptor por encima de 2;
   - borra el directorio y sale con el código del hijo, aunque el borrado falle.

   **Límite:** un SIGKILL al proceso de la corrida no se puede atrapar. El hijo
   (`timeout` y el harness) sigue vivo hasta su timeout, y el directorio de stubs
   queda en disco. Bajo `delegate-run` ese caso queda cubierto mientras el runner
   viva: es subreaper y limpia el árbol completo (ver "delegate-run").
6. **La config se parsea, nunca se `source`a.** Vale igual para los archivos de proveedor
   y para las entradas de MCP: no pueden inyectar fragmentos de shell, ni valores de
   variables de entorno, ni reescribir lo que la corrida fija. Una entrada declara
   **nombres** de variables a reenviar, nunca valores. Dos familias de nombres se
   **rechazan**:
   - Los que la corrida define (`ANTHROPIC_*`, `CLAUDE_CODE_*`). Hoy los `export` de la
     corrida se aplican después del passthrough, así que uno de estos perdería en vez de
     ganar; el rechazo no es por el orden sino porque un servidor que pide un nombre que
     la corrida posee está pidiendo algo que no puede tener.
   - Los que se interpretan como **código** dentro del hijo: `LD_*`, `NODE_OPTIONS`,
     `BASH_ENV`, `ENV`, `PATH`, `HOME`, `PYTHONPATH`, todo `GIT_*` (con
     `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` una entrada http
     configuraba `diff.external` y el `git diff` del piso lo corría), `PAGER`,
     `EDITOR`, `GCONV_PATH` y compañía. También los que desvían o debilitan el TLS del
     hijo, que lleva la key del proveedor: `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` (y
     en minúsculas), `NODE_EXTRA_CA_CERTS` y `NODE_TLS_REJECT_UNAUTHORIZED`. Como el environment del hijo se
     construye desde una allowlist, el passthrough es el **único** canal por el que algo
     de este shell cruza, y un nombre de estos lo convierte en ejecución de código.
     Las credenciales **no** están en esa lista a propósito: reenviar un token es para lo
     que existe la función, así que ahí la respuesta es visibilidad, no rechazo — cada
     corrida imprime en stderr los nombres que reenvía (nombres, nunca valores), y
     `delegate mcp list` los muestra junto con el `rules` completo de la entrada.

## Lo que el piso de solo lectura NO promete

Dicho explícito porque dos reviews independientes lo encontraron y porque un
invariante que se cree más fuerte de lo que es, es peor que no tenerlo.

La allowlist de Claude Code calza el **nombre** de un comando, nunca sus opciones.
Sí considera la **redirección**: `ls /tmp > archivo` y `head -c 4 x > archivo` fueron
denegados los dos contra el harness real, con el comando de la izquierda permitido, así
que un `>` no es una vía de escritura. De ahí salen tres consecuencias:

- `find` y `rg` quedaron **fuera** del piso: `-delete`/`-exec` escriben, y
  `rg --pre=/usr/bin/rm` hace que ripgrep lance ese programa. `grep` cubre la
  necesidad de búsqueda y no tiene equivalente.
- `git` se queda, porque `log`/`diff`/`show` es casi todo lo que hace una tarea de
  review, y git corre programas que nombra su config: `diff.external`, el `command` o
  el `textconv` de un driver, `core.fsmonitor`, `core.pager`, un filtro `clean` o
  `process`, y el `gpg.program` que verifica una firma. Quien pudiera escribir
  `.git/config` —un árbol hostil, o la misma corrida con `--edit`— convertía el piso
  en ejecución de código. Por eso, en el piso, el `git` que alcanza el modelo es un
  wrapper primero en el `PATH`, que:
  - ignora la config de sistema y la global;
  - anula `core.fsmonitor`, `core.pager`, `core.hooksPath` y `diff.external`;
  - anula cada filtro `clean`/`smudge`/`process` y cada driver de diff
    (`command`, `textconv`) que nombre la config del repo: los lista con
    `git config` y los vacía con `-c`;
  - apaga `log.showSignature` y apunta los tres `gpg.*.program` a `/bin/false`;
  - pone `--no-ext-diff`, `--no-textconv` y `--ignore-submodules=all` **justo
    después** del subcomando, antes de todo lo del llamador. Al final quedaban como
    valor de una opción que lo lleva aparte: `git diff --line-prefix` (o
    `--src-prefix`, `--dst-prefix`, `--word-diff-regex`) se tragaba el
    `--no-ext-diff` y corría el driver del repo;
  - rechaza toda forma y abreviatura de lo que las deshace (`--ext*`, `--textc*`,
    `--ignore-su*`, `--sub*`), porque `git diff --no-index` acepta prefijos, y
    `--output` en cualquier prefijo. Los submódulos se ignoran: su contenido lo lee
    un git con la config del submódulo, que nada de esto anula.

  Es defensa en profundidad, no un límite: es una lista de mecanismos **conocidos** de
  git 2.43, y una versión de git que agregue otro helper configurable lo deja pasar. El
  wrapper se alcanza por nombre, no por ruta absoluta. El límite real sigue siendo **no
  delegar sobre árboles hostiles**. Con `--bash` el `git` es el de la máquina: el shell
  sin restricción no se angosta.
- **Una corrida de solo lectura puede leer cualquier archivo que pueda leer quien
  la invoca**, y mandarlo al proveedor. `--add-dir` agrega acceso, no confina;
  `--restricted` confina las herramientas de archivo pero no el Bash. Por eso el
  `--dir` de una corrida delegada nunca apunta a un árbol con secretos, y por eso
  "qué no delegar" no es un consejo, es parte del contrato.

Lo que el piso sí da: que escribir, correr shell sin restricción o alcanzar un MCP
sea una **decisión explícita** de quien invoca, visible en la línea de comandos, y
que el modelo sepa exactamente qué tiene. El aislamiento real contra producción lo
dan otras tres cosas: el environment se construye desde una allowlist en vez de
heredarse, no hay ningún MCP salvo el que se pida por nombre, y no se leen settings
de usuario.

## Superficie

Ejecutable `delegate`, con `dlg` como abreviación (el mismo script por symlink, no una
copia). No `dg`: en esta máquina ese nombre es el CLI de Deepgram.
Proveedor como subcomando; modo agente por defecto.

```
delegate deepseek "revisa src/auth.ts y lista los error paths"
delegate deepseek --edit --bash --account kontigo "arregla y corre los tests"
delegate deepseek --mcp context7 "cómo se usa el hook nuevo de esta librería"
delegate deepseek --edit --mcp-write figma "aplica estos cambios al frame Login"
delegate qwen --timeout 2400 "segunda opinión de este PR"
delegate deepseek chat "explica este regex"      # sin herramientas

delegate account deepseek list|add|use
delegate mcp list
delegate providers
delegate help
```

`--mcp` y `--mcp-write` son repetibles y se acumulan; `--mcp-write <id>` implica las
herramientas de lectura de ese mismo servidor. En modo `chat` los dos son un error, igual
que `--edit` y `--bash`.

## Proveedor = data

Un archivo por proveedor en `providers.d/`, formato `clave<TAB>valor`.

**En config:** `endpoint`, `auth_env`, `key_dir`, `key_pattern`, `default_account`,
`model`, `small_model`, `timeout`, `caps`, `residency`.

**En el script, no configurable:** validación de argumentos y config, el lattice de
permisos locales, el enforcement de `caps`, `--bare`, la restricción de setting-sources,
la limpieza del environment, timeout y stdin, el bloqueo de otras CLIs, el chequeo de
permisos de la key y su inyección por environment, y el guardián de nombres reservados
del passthrough.

`auth_env` es una allowlist de dos nombres, no un patrón: `ANTHROPIC_API_KEY`, que el
harness manda como `x-api-key`, o `ANTHROPIC_AUTH_TOKEN`, que manda como
`Authorization: Bearer`. Cuál acepta el endpoint no se deduce: DeepSeek toma el primero
y Qwen Cloud el segundo. Los dos, y no uno solo, porque fijarlo en `ANTHROPIC_API_KEY`
dejó a qwen inservible con un mensaje que culpaba al archivo de config. Sigue siendo
allowlist para que un archivo de config no pueda apuntar la key a una variable
arbitraria.

El parser rechaza claves desconocidas, claves duplicadas, tabs o newlines en los valores,
IDs de proveedor inválidos y variables de auth no soportadas. Sin `source`, sin `eval`,
sin JSON/YAML/TOML: sumarían parser y dependencia para un esquema plano de diez líneas.

## MCP = data

Un archivo por servidor en `$DELEGATE_MCP_DIR` (por defecto `~/.config/delegate/mcp.d`),
mismo formato `clave<TAB>valor` que un proveedor. El dueño de la máquina declara qué
existe; la invocación decide qué alcanza. Ejemplos en `examples/mcp.d/`.

**Claves:** `transport` (`http`, `sse` o `stdio`), `tools_read` (siempre obligatoria: un
servidor sin herramienta de lectura no tiene nada que ofrecer), `url` (solo `https`, solo
para http y sse), `command` más `trust local-exec` (solo stdio: es un binario corriendo
con los privilegios del hijo, y eso merece una línea explícita), `args`, `tools_write`,
`env_passthrough` (**nombres**, nunca valores), `description`, `rules` y `requires_file`.

`rules` es la guía propia del servidor que se le agrega al system prompt de la corrida
**solo cuando esa entrada está habilitada**. Ahí va lo que el modelo necesita saber para
usarlo bien sin quemar turnos descubriéndolo: de dónde salen los identificadores, qué no
debe tocar, qué tiene que reportar al terminar. Es el único valor de una entrada que
cambia lo que se le **dice** al modelo, y se anexa después de las reglas de la corrida,
que es la posición más fuerte del prompt: por eso `delegate mcp list` lo imprime
completo, no un conteo. Que se pueda leer es toda la defensa que hay contra una entrada
que diga "ignora las reglas anteriores".

`requires_file` son rutas que tienen que **existir** para que la entrada sea usable
(hasta 4, sin `..`). Una ruta relativa se lee desde el directorio de trabajo de la
corrida (`--dir`), que es donde el hijo también la va a buscar; una absoluta es
precondición de la máquina. Si falta, la corrida se rechaza con el nombre del archivo y
del servidor que lo necesita, **antes** de leer la key. Existe porque `rules` puede
decir "lee X primero", y una corrida delegada a la que le falta X no se detiene:
improvisa, y pagarle a un proveedor para ver a un modelo razonar alrededor de un archivo
de instrucciones ausente es la falla cara.

El límite que hay que conocer: la precondición es por **entrada**, no por permiso. No hay
forma de decir "solo si escribe", y por eso el ejemplo de figma **no** la usa: su archivo
de skill es precondición de escribir, y leer un diseño sin él es una corrida
perfectamente buena. El ejemplo donde sí calza es `examples/mcp.d/repo-notes`.

El JSON de `--mcp-config` se arma por concatenación, sin paso de escape, y eso es seguro
**solo** porque cada charset excluye la comilla, el backslash y los caracteres de
control. La validación es lo que hace sólido el armado: ampliar un charset obliga a
revisar el armado.

Figma dejó de estar cableada y es una entrada de ejemplo más, sin ningún privilegio que
otra no tenga. Su login OAuth lo guarda Claude Code indexado por nombre y config del
servidor, así que una entrada `figma` apuntando a `https://mcp.figma.com/mcp` reusa ese
login y **ningún token pasa por acá** — por eso su `env_passthrough` está vacío.

## Cuentas

`<key_dir>/api_key.<cuenta>` (modo 600) es la fuente de verdad. `--account <nombre>` y
`DELEGATE_ACCOUNT` resuelven directo a uno de esos archivos. `DEEPSEEK_ACCOUNT`, el
nombre viejo, se **rechaza** con 2: estuvo documentado y se ignoraba, así que quien lo
usaba pagaba con otra cuenta sin enterarse. La cuenta por defecto vive
en un archivo de metadata no secreto, para no depender de estado global mutable.

`api_key` sin sufijo se mantiene **solo como proyección de compatibilidad** para
`cc-proveedor`, que es Python y lee esa ruta fija.

**Reglas duras:**

- El symlink de la cuenta activa se reemplaza **atómicamente**: symlink temporal más
  `rename(2)` (`mv -T`). **Nunca `ln -sfn`**, por portabilidad: el coreutils de esta
  máquina (9.4) resultó usar temporal más `renameat` por dentro, verificado con `strace`,
  pero eso es detalle de implementación y no contrato. El patrón que sí está roto es
  desvincular y después crear: dejó 28.382 observaciones de ruta inexistente contra 0 de
  `mv -T`.
- Antes de usar una key se valida que el target esté **dentro** de `key_dir` y tenga
  permisos 600. La corrida valida y lee **el mismo descriptor**: abre el archivo una vez,
  repite los chequeos sobre `/proc/$BASHPID/fd/<fd>` y lee de ese fd. Validar un nombre y
  leerlo después dejaba cambiar el archivo entre las dos cosas.
- `key_dir` tiene que ser del usuario y no tener bits de grupo ni de otros (700). Se
  comprueba en **cada** uso (validar, leer, `add`), no solo cuando `add` lo crea.
- `account add` nunca escribe sobre la ruta final: crea un temporal con punto, con
  noclobber y `umask 077`, y lo publica con `link(2)` (`ln -T`), que falla con EEXIST en
  vez de reemplazar o seguir un symlink. Lo que aparezca en esa ruta mientras `add`
  espera la key se deja intacto y `add` falla.

**Lo que esto NO promete:**

- No hay locking de cuentas. Todo lo anterior protege contra **otros** usuarios, que con
  `key_dir` en 700 no pueden entrar; el mismo usuario puede seguir cambiando archivos en
  su `key_dir` mientras corre `delegate`.
- La primera verificación es por ruta, y lo que la sigue es un `open(2)` normal. Si entre
  ambas el archivo se cambia por un FIFO, la apertura se bloquea, y el `--timeout` de la
  corrida no la cubre porque solo limita al hijo. Desde Bash no hay `O_NONBLOCK` ni
  `O_NOFOLLOW`. Una vez abierto, lo leído es lo verificado.
- Solo se revisa `key_dir` resuelto, no sus ancestros. Si un ancestro es escribible por
  otros, pueden renombrar el directorio completo.
- La ruta de una cuenta nunca puede ser `api_key`, `default_account` ni un archivo con
  punto: `%s` es un `key_pattern` válido y `api_key` un nombre de cuenta válido, y
  juntos hacían que la key **fuera** el symlink de compatibilidad.
- Borrar la cuenta activa deja el setup **sin** cuenta activa, a propósito y con aviso
  ruidoso. Cambiar en silencio qué key se factura es peor que quedarse sin activa.
- Ningún comando imprime el contenido de una key. Lo más que muestra es una huella
  (prefijo de sha256).

## delegate-run (Fase B)

`bin/delegate-run` (Python 3 de la biblioteca estándar, `lib/delegate_run/`) envuelve a
`delegate`, no lo reemplaza: corre una tarea que **edita** en un worktree aislado,
mide él mismo qué cambió, corre los checks que pidió quien llama y deja el resultado en
una rama. Diseño y decisiones: `docs/specs/2026-10-10-delegate-v2-design.md`, "Fase B".

```
delegate-run < peticion.json          # resultado JSON por stdout
delegate-run status <run_id>          # el result.json tal como se ve ahora
delegate-run list [--limit N]
delegate-run cancel <run_id>          # espera a que la corrida escriba su status final
delegate-run cost [--days N]
delegate-run stats [--days N] [--provider P] [--json]   # tabla legible; JSON con --json
delegate-run gc [--days N] [--dry-run]
```

`stats` da, por proveedor/modelo y por proveedor, corridas, estados, éxito al primer
intento, duración p50/p90, turnos, tokens, costo conocido (y cuántas corridas no lo
tienen), archivos por corrida y corridas fuera de scope. Qué mide cada columna y
cómo compararlo con el gasto del modelo principal (`ccusage`):
`docs/medir-delegacion.md`.

Petición (claves desconocidas o repetidas se rechazan): `v` (1), `provider`, `repo`
(ruta absoluta), `task`, y opcionales `base` (`HEAD`), `account`, `grants` (`edit`,
`bash`), `mcp` (solo lectura), `timeout` (60–14400, 1800), `checks` (lista de argv,
nunca una cadena de shell), `scope` (globs relativos a la raíz: `*` no cruza `/`,
`**` cruza segmentos, una ruta sin comodín cubre todo lo que tiene debajo),
`snapshot` (`false`), `wait` (`true`).

Salida: exit 0 solo con `completed`; 1 con cualquier otro status (el JSON dice cuál);
2 si la petición o los argumentos son inválidos, y entonces no hay corrida ni stdout.
`status`: `completed`, `checks_failed`, `task_failed` (exit distinto de 0, o
`is_error` del harness), `rejected` (delegate salió con 2 —`child_exit` 2— o el runner
rechazó repo, base o árbol sucio —`child_exit` null—), `timed_out` (el reloj del
runner o el `--timeout` de delegate, exit 124), `cancelled`, `busy`, `infra_failed`,
`invalid_result`. `status`/`list` derivan además `abandoned` (dice `running` y nadie
tiene el lock) y `unreadable`.

**Quien llama acepta un resultado solo si** `status` es `completed`, `scope_violation`
está vacío, lee el diff de `delegate/<run_id>` él mismo y vuelve a correr los checks
antes de integrar. `report` es lo que dice el modelo: evidencia, no autorización.
`scope_violation` no cambia el `status`; hay que mirar los dos.

Reglas que el código cumple y los tests prueban:

- Un árbol sucio, **incluidos los archivos nuevos**, se rechaza cuando la base es el
  commit actual. `snapshot: true` lo congela con un índice temporal en
  `refs/delegate/snapshot/<run_id>` sin tocar el índice ni el árbol del usuario. Con
  otra base el árbol no se lee, así que no importa si está sucio. Se rechaza también
  un índice con archivos `assume-unchanged` o `skip-worktree` presentes (git no ve sus
  cambios) y un submódulo con cambios adentro (ni el snapshot ni el worktree los
  llevan). El chequeo corre con `GIT_OPTIONAL_LOCKS=0`: no reescribe el índice.
- Los cambios se miden contra la base con `-z` y sin detección de renombres: los dos
  extremos de un rename pasan por el scope. Lo que el delegado haya commiteado da igual,
  y las refs de reemplazo (`refs/replace/*`) se ignoran en toda llamada. Un nombre que
  no es UTF-8 se reporta con `surrogateescape` (el JSON sale en ASCII): `b"\xff.txt"`
  y el literal `\xff.txt` nunca se confunden en el scope.
- Scope: `*` y `?` no cruzan `/`, `**` sí, un patrón sin comodines es un directorio, y
  las clases `[...]` siguen al shell y a git (`!` o `^` niegan, `]` inicial es literal).
  Un patrón que no compila se rechaza en la petición.
- La rama es plumbing (`commit-tree` + `update-ref` que solo crea): sin hooks, sin firma,
  autor `delegate <proveedor>`, padre la base. Lo que escriban los checks no llega a la
  rama. Sin cambios no hay rama.
- El git del runner nunca lee lo que el delegado puede escribir: el gitdir del worktree
  se fija antes de lanzar al hijo y toda llamada lleva `--git-dir`/`--work-tree` (el
  archivo `.git` del worktree reapuntado a un repo con un `filter.*.clean` corría ese
  programa con el environment del runner). `git` y `delegate` se resuelven a ruta
  absoluta contra las entradas absolutas del `PATH`, antes de entrar al worktree.
- Después del hijo, la config del repo es del delegado (el worktree comparte
  `.git/config`). Todo git de ahí en adelante, y el de `gc`, corre endurecido: sin
  config de sistema ni global, environment construido (sin tokens del que llama),
  hooks, `core.fsmonitor`, `diff.external` y firmas apagados, y cada filtro
  (`clean`/`smudge`/`process`/`required`) y driver de diff (`command`/`textconv`) que
  nombre el repo anulado vía `GIT_CONFIG_KEY_n`. Antes del hijo el repo es del usuario
  y su config se respeta (un smudge de LFS funciona al crear el worktree).
- Los checks corren con un environment construido (sin tokens del que llama) y un `PATH`
  sin entradas relativas ni dentro del worktree. Ejecutan código que escribió el modelo
  con los permisos del usuario: el environment se limpia, los archivos no se confinan.
- Procesos: el hijo corre en su propia sesión, el runner se hace subreaper y al terminar
  (bien, por timeout o por cancelación) manda TERM a todo el árbol y a la sesión, espera
  la gracia y manda KILL. Recuerda la sesión y el grupo de cada proceso visto, zombis
  incluidos, y los mata con `killpg`: una cadena de fork-y-salir que un escaneo de
  `/proc` no alcanza a ver viva cae igual. TERM o INT al runner cuentan como
  cancelación, también si llegan después del hijo (mientras se mide o se commitea):
  la corrida queda `cancelled`, nunca `completed`; HUP se ignora.
- Un lock por corrida (`flock`, lo suelta el kernel) dice si el runner vive. `gc` lo toma
  exclusivo antes de decidir y nunca borra algo distinto de `worktrees/<run_id>`,
  `delegate/<run_id>` y la ref del snapshot: borra refs con `--no-deref` (un symref
  plantado no arrastra a `main`), no sigue un symlink en `worktrees/<run_id>` y no
  corre `git worktree prune` (barre registros ajenos).
- El costo sale de `prices.tsv` (el de `~/.local/state/delegate/` gana sobre el del
  repo). `total_cost_usd` del harness se ignora: pone precios de Anthropic a modelos
  que no lo son. Sin precio o sin uso, `null`, nunca 0. `cost` y `stats` recalculan
  una corrida sin costo con la misma regla, desde el `modelUsage` de su
  `stdout.log` y a la hora en que arrancó: si un modelo no tiene precio, sigue
  `null`.
- `prices.tsv` lleva solo precios copiados de la página oficial, con su URL
  (`source`) y la fecha en que se copiaron (`as_of`). Los cuatro precios son los de
  hora pico; `peak_utc` (`mon-fri 01-04,06-10`) y `offpeak_factor` describen la
  tarifa por franja (DeepSeek cobra la mitad fuera de pico). Sin hora de inicio se
  usa el precio pico. `model` es la clave de `modelUsage` tal cual, con `[1m]`.
  Qwen (Token Plan) no tiene fila: cobra créditos de una suscripción sin tasa por
  token publicada. **Al cambiar una tarifa:** abre la página de `source`, corrige
  los números y `as_of` en la fila (y el comentario de arriba), y corre
  `test/py.test.sh`. Un modelo nuevo en `providers.d/` necesita su fila, o sus
  corridas quedan sin costo. Las corridas ya terminadas guardan el costo con el que
  se cerraron; solo las que quedaron en `null` se recalculan con la tabla nueva.
- El runner nunca abre una key. Por eso el resultado trae `account` (la cuenta pedida, o
  null si decidió delegate), no una huella.

**Límites:** un SIGKILL al runner deja vivo al hijo hasta el `--timeout` de delegate y la
corrida queda `abandoned`; `gc` la recoge pasadas 5 horas. Los archivos ignorados por
`.gitignore` ni se miden ni se commitean. Con los filtros anulados, un archivo LFS que
el delegado modifique entra a la rama como contenido crudo, no como puntero. **Sin
sandbox** (`sandbox: "none"`): con `bash` el delegado alcanza el `.git` compartido
(refs, config) y cualquier archivo del usuario, y la limpieza de procesos no es
contención del kernel (un proceso que cambie de sesión en cada fork puede escaparse).
Con sandbox esas dos cosas se cierran; lo que queda abierto está en "Sandbox".

## Sandbox (bubblewrap)

`delegate-run` corre al hijo de `delegate` (el harness y todo lo que lance) dentro de
bubblewrap cuando está disponible. `DELEGATE_SANDBOX`: `auto` (default: bwrap si
funciona; si no, sigue sin sandbox, lo avisa por stderr y deja `sandbox: "none"` en
`result.json`), `bwrap` (sin bwrap usable la corrida es `infra_failed`, nunca un
downgrade callado) u `off`. `result.json` trae `sandbox`: `bwrap`, `none` o `null` si
la corrida terminó antes de decidirlo. Por debajo es `delegate --sandbox bwrap
--sandbox-ro <gitdir>`, que también sirve fuera del runner.

**Diseño de la key:** `delegate` corre **afuera** del sandbox, lee la key de su archivo
600 como siempre, y solo su hijo entra. El `key_dir` nunca se monta (ni las keys de
otras cuentas); la key cruza únicamente en el environment del hijo, igual que antes.

Lo que ve el hijo:
- `/usr` (y los symlinks `/bin`, `/lib*`) y `/etc` en solo lectura, `/proc` propio,
  `/dev` mínimo, y del `/run` solo el archivo al que apunta `/etc/resolv.conf`.
- `/tmp` y `HOME` como tmpfs privados: del home real no hay nada salvo lo que se monta
  encima (el directorio del binario de `claude`, en solo lectura).
- El worktree en escritura; el gitdir del repo (común y del worktree) en solo lectura,
  montado **después**: git lee historia, pero ni una ref, ni la config, ni un hook del
  repo del usuario se pueden escribir (un `git commit` adentro falla, y el runner hace
  el suyo afuera). El directorio de stubs, en solo lectura.
- Namespaces propios de usuario, montaje, pid, ipc, uts y cgroup; `--disable-userns`,
  `--new-session` (sin TIOCSTI a la terminal) y `--die-with-parent`. Con el namespace
  de pid, cuando el harness sale muere todo lo que dejó, también una cadena que cambia
  de sesión en cada fork: probado sin el runner, con un control sin sandbox donde la
  misma cadena sobrevive.
- Seccomp: `socket(AF_UNIX)` y `socketpair` de datagramas fallan con `EAFNOSUPPORT`,
  `io_uring_setup`, `_enter` y `_register` con `ENOSYS`, y una arquitectura ajena mata
  al proceso. Quedan permitidos los pares `SOCK_STREAM` y `SOCK_SEQPACKET` (las
  tuberías de stdio de libuv): están conectados entre sí y no pueden conectarse a otra
  dirección. Si el stdout que recibe `delegate` es un socket, el hijo recibe en su
  lugar una tubería que `cat` reenvía desde afuera: el filtro impide crear un socket,
  no usar uno heredado. La red es
  compartida y con ella el espacio de sockets unix **abstractos** del host: sin el
  filtro, el sandbox se conectaba a `@/tmp/.X11-unix/X0`, y un X que admite al usuario
  por uid (`SI:localuser`) lo aceptaba, que es teclear en una terminal de afuera.
  Verificado con `xdpyinfo`. Filtro solo para x86_64 y aarch64; en otra arquitectura
  `--sandbox bwrap` se rechaza.
- Antes de leer la key, cada montaje que va encima del HOME privado se valida
  canónico: ni el directorio de trabajo, ni un `--sandbox-ro`, ni el del binario de
  `claude` pueden cruzarse con el `key_dir`; un `--sandbox-ro` no puede ser el HOME ni
  contenerlo, ni contener el directorio de trabajo. Un `claude` ELF se monta solo; un
  script lleva su directorio, salvo que ese directorio contenga el HOME.
- El runner fija la ruta de su `git` antes de crear el worktree: una entrada
  absoluta del `PATH` dentro del worktree no le cambia el `git` después del hijo.

Lo que **no** cierra:
- **La red no está restringida.** El hijo alcanza internet y cualquier servicio TCP de
  `localhost` (una base local, un servidor de desarrollo). Restringirla al host del
  proveedor exige un proxy y `--unshare-net`: trabajo futuro.
- La key está en el environment del harness, dentro del sandbox: con `bash` el modelo
  la lee de `/proc/self/environ` y, con la red abierta, puede mandarla a donde quiera.
- **Los checks corren afuera**, con los permisos del usuario, y ejecutan código que
  escribió el modelo (un `package.json`, un test). El sandbox confina al delegado, no
  a lo que el runner corre después: trabajo futuro, correrlos con el mismo perfil.
- `/usr` y `/etc` completos son legibles; las herramientas instaladas en el home
  (nvm, cargo, `~/.local/bin`) no existen adentro, así que con `bash` un `npm test`
  puede no correr ahí.
- El worktree y el gitdir completo (todas las ramas y objetos del repo) son legibles.
- Un par `SOCK_STREAM` permitido puede hacer `bind()` a un nombre abstracto del host y
  ocuparlo: un servicio que arranque después con ese nombre falla (negación de
  servicio, no conexión; ese socket no puede escuchar). Negarlo exige negar `bind()`
  entero o aislar la red.
- El solo lectura del gitdir es por montaje: un symlink o un hardlink que ya existiera
  en el gitdir apuntando al directorio de trabajo es un alias escribible. Con los
  worktrees del runner no pasa (el worktree es nuevo y el árbol principal no se
  monta), y un hardlink nuevo entre montajes falla con `EXDEV`; con `delegate
  --sandbox` sobre un repo armado así, sí.
- `sandbox: "bwrap"` dice que el probe pasó y que a `delegate` se le pidió el sandbox,
  no una confirmación desde adentro: si bwrap falla al montar, la corrida sale
  `task_failed` con su mensaje en `stderr.log`, y un `DELEGATE_BIN` que ignore
  `--sandbox` corre sin confinar.
- Un bug del kernel en lo que sigue expuesto (namespaces, syscalls permitidas).
- En una máquina sin bwrap (`sudo apt install bubblewrap`) y con `auto`, nada de esto
  aplica: `sandbox: "none"` es la señal.

Estado en `~/.local/state/delegate/` (o `$DELEGATE_STATE_DIR`), 700: `runs/<run_id>/`
(`request.json`, `result.json`, `stdout.log`, `stderr.log`, `patch.diff`, `checks/`,
`lock`), `worktrees/<run_id>/` y `slots/` (`DELEGATE_SLOTS`, 2 por defecto).

## Lenguaje

Bash, sin dependencias de runtime. Esto es construir un environment y hacer `exec`.
`delegate-run` es la excepción prevista: supervisar, medir un diff y escribir JSON
atómico es justo el caso de Python, y no toca la key.

Python convendría si creciera hacia clientes HTTP portables, transformación de salida
JSON estructurada o manejo atómico de cuentas multiplataforma. Hoy un rewrite agregaría
riesgo de migración sin fortalecer el manejo de la key, que es lo que importa.

## Tests

`test/run.sh` corre todas las suites, incluida la de Python (`test/py.test.sh`:
`python3 -m unittest discover -s test/py -t .`). `delegate-run` se prueba con un
`delegate` falso (`test/py/fake_delegate.py`); el smoke contra el proveedor real corre
solo con `DELEGATE_REAL_HARNESS=1`.

Los invariantes son testeables y en los scripts originales no estaban testeados. Mínimo:

- La key no aparece nunca en la línea de comandos del hijo (verificar por `/proc`).
- El parser rechaza cada entrada inválida de la lista.
- `--account` gana sobre la cuenta activa.
- Cambiar la cuenta activa a mitad de una corrida no afecta la corrida en curso.
- El reemplazo del symlink nunca deja la ruta inexistente.
- Una key con permisos distintos de 600, o fuera de `key_dir`, es rechazada.
- Sin `--edit` no hay escritura; sin `--bash` no hay shell; sin `--mcp` no llega ningún
  `--mcp-config`, aunque el directorio tenga entradas.
- Un servidor declarado pero no pedido no llega al hijo, ni siquiera su `rules`.
- Habilitar un servidor no habilita sus herramientas de escritura.
- Un `env_passthrough` reenvía el valor del shell padre, nunca uno del archivo, y un
  nombre que la corrida define se rechaza.
- Una entrada con `$(...)`, backticks o `;` en un valor no ejecuta nada.
- Un servidor desconocido, o un `--mcp` por encima del techo, falla **antes** de leer la
  key.
- `caps` del proveedor gana sobre los flags pedidos.
- El inventario de herramientas del system prompt se **deriva** del grant: nunca anuncia
  una herramienta que la corrida no tiene.
- El environment del hijo se construye, no se hereda: `GITHUB_TOKEN`, `AWS_*`,
  `NODE_OPTIONS`, `LD_PRELOAD`, `SSH_AUTH_SOCK` y `PGPASSWORD` no llegan, ni por nombre
  ni por valor. Probar con nombres de secretos **distintos** de los tres que el viejo
  `env -u A -u B -u C` ya quitaba: ese test pasaba contra una implementación que dejaba
  cruzar todo lo demás.
- Una función exportada (`export -f grep`) no cruza al hijo, ni su nombre ni su cuerpo.
- Un `export` del padre que choque con un local del CLI (`key`, `prompt`, `model`,
  `claude_bin`, `path`, `n`, `v`…) ni llega al hijo ni rompe la corrida, y la key sigue
  viajando.
- Un `env_passthrough` con un nombre que se interpreta como código se rechaza con 2, y
  los nombres que sí se reenvían salen en stderr.
- `delegate mcp list` imprime el `rules` completo de una entrada, no un conteo.
- `command` acepta ruta absoluta o nombre pelado, y rechaza `./x`, `bin/x` y
  `a/b/c`: una ruta relativa se resuelve contra el cwd del hijo, que es el árbol bajo
  revisión.
- `dg_account_active` respeta el `key_pattern` del proveedor; con un patrón propio, `list`
  y `active` tienen que coincidir.
- Borrar la única cuenta sale con 0. Ese camino terminaba en un `[[ ]] &&` que era el
  último comando de la función, así que devolvía 1 después de haber borrado la key.
- Un `requires_file` que falta rechaza la corrida con 2, nombrando el archivo y el
  servidor, y **antes** de leer la key; presente, la deja pasar. Se resuelve contra
  `--dir`, no contra el cwd del CLI. Y una entrada declarada pero **no pedida** no bloquea
  nada: la precondición de un servidor que no se alcanza no es precondición de la corrida.
- Una entrada vacía, relativa o dentro de `--dir` en el `PATH` nunca aporta un
  binario, ni al hijo ni al propio CLI. Hasta que `cmd_run` filtra el `PATH` del
  llamador contra `--dir`, el CLI corre todo por nombre con `PATH=/usr/bin:/bin`: el
  `dirname` y el `readlink` del arranque salían de una entrada absoluta dentro del
  repo. `claude`, `timeout` y el `grep` del filtro se resuelven a ruta absoluta antes
  del `cd`, y el `PATH` del hijo es stubs más las entradas absolutas que quedan. El
  test planta también los helpers del arranque (`dirname`, `readlink`, `stat`, `sed`,
  etc.).
- El runner cuenta como fallo un test que retorna distinto de 0 aunque no haya
  llamado a `fail`: un test de seguridad tiene que poder ponerse rojo muriendo.
- Una tarea que parece opción (`-- --help`) llega al hijo como prompt, detrás de
  un `--`, y `--append-system-prompt` sigue siendo la última opción.
- En el piso, el `git` del hijo no corre ningún helper que nombre la config del
  repo o la global (`diff.external`, `textconv`, `core.fsmonitor`, `core.pager`,
  un filtro `clean` o `process` o el `command` de un driver en el `.git/config` del
  repo, `gpg.program` con `log.showSignature` o `--show-signature` sobre un commit
  firmado), tampoco con abreviaturas (`--textcon`, `--ext-dif` con `--no-index`) ni
  con una opción que se trague el flag siguiente (`--line-prefix`, `--src-prefix`,
  `--dst-prefix`, `--word-diff-regex`); rechaza `--ext*`, `--textc*`,
  `--ignore-su*`, `--sub*` y todo prefijo de `--output`; con `--bash`, el `git` es
  el de la máquina.
- Un `env_passthrough` con cualquier nombre `GIT_*` se rechaza con 2.
- Un rechazo de `dg_perm_resolve` o de `dg_mcp_resolve` justo después de un grant
  no deja ningún global del grant, probado en el **mismo** shell: inspeccionar
  los globals del padre después de correr el resolver en un subshell no prueba
  nada.
- `DEEPSEEK_ACCOUNT` definida, aunque esté vacía, rechaza la corrida con 2, nombrando
  `DELEGATE_ACCOUNT`, antes de leer la key.
- Una entrada del environ con un nombre que Bash no puede nombrar (`SECRET-TOKEN`,
  `A.B`) no llega al hijo, y la key sigue sin aparecer en ningún argv.
- Invocado por nombre, nada del shell que llama corre antes de la limpieza: un
  `BASH_ENV`, una función exportada `set`, un `SHELLOPTS=xtrace` con un `PS4` que
  expande la key, y `allexport` (por `SHELLOPTS` o por `bash -a`), cada uno con su
  sentinela. Los tests de aislamiento ejecutan el script, no `bash bin/delegate`.
- El `grep` que filtra stderr no lleva el environment del padre ni la key: un
  `GITHUB_TOKEN` del shell que invoca no aparece en su `/proc/<pid>/environ`.
- Los stubs viven en un directorio privado (700) por corrida, distinto en cada una y
  borrado al terminar, también si al CLI le llega un TERM; un symlink o un ejecutable
  dejados en el viejo directorio compartido no tienen efecto, un padre escribible por
  el grupo o por todos sin sticky bit (770, 775, 730, 777) se rechaza y uno en 1770,
  700 o 755 se acepta, y el código de salida del hijo es el de la corrida,
  también cuando falla el borrado.
- El hijo no hereda ningún descriptor aparte de 0, 1 y 2: la copia que bash guardaba
  del pipe de la key (fd 63) le llegaba abierta.
- TERM, INT y HUP enviadas al proceso de la corrida llegan al hijo como esa misma
  señal, no todas como TERM.
- Mientras el hijo vive, ningún proceso de la máquina tiene en su
  `/proc/<pid>/environ` un `GITHUB_TOKEN` del shell que invoca.
- Una cuenta cuyo archivo caería sobre `api_key`, `default_account` o un temporal con
  punto (por ejemplo `key_pattern` `%s` con la cuenta `api_key`) se rechaza con 2, y
  `list` nunca muestra esos archivos como cuentas.
- Un `key_dir` en 750, 705, 770, 777 o 755 se rechaza al validar, al leer y en `add`.
  `add` no escribe a través de un symlink, vivo o colgante, ni sobre un archivo plantado
  en la ruta después de su chequeo. No deja temporales, tampoco si lo interrumpen TERM
  o HUP entre el temporal y su borrado; un SIGKILL sí puede dejar uno con nombre de
  punto en `key_dir`, que es 700. `dg_account_load` rechaza, sin imprimir la key, lo
  mismo que `validate`, y un FIFO sin bloquearse.
- Con bwrap usable, dentro del sandbox el hijo no lee un canario en `HOME`, ni el
  archivo de la key, ni un archivo cualquiera fuera de los montajes; no escribe la
  config, una ref, un hook ni el `HEAD` del gitdir; sí escribe el worktree y eso llega
  a la rama; no crea sockets unix; no hereda descriptores aparte de 0, 1 y 2; y una
  cadena que cambia de sesión en cada fork muere con el sandbox (con su control sin
  sandbox, donde sobrevive). Tampoco crea un hardlink del gitdir (`EXDEV`), ni usa
  io_uring, ni recibe como stdout un socket aunque se lo pasen a `delegate`. Un
  `--sandbox-ro` sobre el HOME (también por symlink), el `key_dir` o la key, o que
  contenga el directorio de trabajo, y un directorio de trabajo dentro del `key_dir`,
  salen con 2 antes de leer la key; un `claude` script en el HOME no lo monta. Un
  `git` plantado en una entrada del `PATH` dentro del worktree no lo corre el runner.
  `auto` sin bwrap queda en `none`; `bwrap` sin bwrap es
  `infra_failed` sin lanzar al hijo; un flag de sandbox inválido sale con 2 antes de
  leer la key.

## Lo que NO se construye

Nombrado para que no reaparezca: daemon, locking de cuentas, keyring o Vault, base de
datos de costos, DSL de políticas, sistema de plugins, descubrimiento o instalación
automática de servidores MCP (el registro es declarativo y lo escribe el dueño de la
máquina; nada se agrega solo), fallback o enrutamiento automático entre proveedores (manda código a la jurisdicción o a
la cuenta equivocada), y enforcement automático a partir de `residency` — eso último es
teatro de seguridad: la nota se muestra y la elección del proveedor queda explícita.

## Migración

Los scripts que esto reemplaza viven en `~/bin`: `deepseek-agent`, `qwen-agent`,
`deepseek` y `deepseek-cuenta`. Orden, sin borrar nada hasta el final:

1. `delegate` funcionando en paralelo a los scripts actuales.
2. Los subagentes `deepseek` y `qwen` de Claude Code apuntan a `delegate`.
3. `cc-proveedor` sigue leyendo `api_key` sin cambios.
4. Con todo lo anterior verde, se borran los cuatro scripts.

No hay urgencia: los scripts actuales funcionan.

## Qué NO delegar

Vale para la herramienta y para quien la use: secrets, PII de clientes, datos o consultas
de producción, y decisiones de dinero, seguridad o compliance. Lo que sale hacia el
proveedor es el prompt y todo archivo que el agente lea en el directorio de trabajo.
