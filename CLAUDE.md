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
   arrancado con `env -i`, que la exporta y hace `exec`. Corolarios, cada uno un
   camino de fuga que existió:
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
5. **Otras CLIs de IA bloqueadas en el PATH del hijo.** Un delegado no invoca `codex`,
   `claude` ni `delegate` para gastar cuota cara ni para re-delegar.
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
     `EDITOR` y compañía. Como el environment del hijo se
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
  review, y git corre programas que nombra su config: `diff.external`, el
  `command` o el `textconv` de un driver, `core.fsmonitor`, `core.pager`, un
  filtro `clean`. Quien pudiera escribir `.git/config` —un árbol hostil, o la
  misma corrida con `--edit`— convertía el piso en ejecución de código. Por eso,
  en el piso, el `git` que alcanza el modelo es un wrapper primero en el `PATH`:
  ignora la config de sistema y la global, anula `core.fsmonitor`, `core.pager`,
  `core.hooksPath` y `diff.external`, fuerza `--no-ext-diff`/`--no-textconv`, y
  rechaza `--ext-diff`, `--textconv` y `--output`. Es defensa en profundidad, no
  un límite: un filtro `clean` en el `.git/config` del repo con su
  `.gitattributes` sigue corriendo, y el wrapper se alcanza por nombre, no por
  ruta absoluta. El límite real sigue siendo **no delegar sobre árboles
  hostiles**. Con `--bash` el `git` es el de la máquina: el shell sin
  restricción no se angosta.
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

Ejecutable `delegate`, con `dg` como abreviación (el mismo script, no una copia).
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
  permisos 600.
- Borrar la cuenta activa deja el setup **sin** cuenta activa, a propósito y con aviso
  ruidoso. Cambiar en silencio qué key se factura es peor que quedarse sin activa.
- Ningún comando imprime el contenido de una key. Lo más que muestra es una huella
  (prefijo de sha256).

## Lenguaje

Bash, sin dependencias de runtime. Esto es construir un environment y hacer `exec`.

Python convendría si creciera hacia clientes HTTP portables, transformación de salida
JSON estructurada o manejo atómico de cuentas multiplataforma. Hoy un rewrite agregaría
riesgo de migración sin fortalecer el manejo de la key, que es lo que importa.

## Tests

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
  binario: `claude`, `timeout` y el `grep` del filtro se resuelven a ruta absoluta
  antes del `cd`, y el `PATH` del hijo es stubs más las entradas absolutas que
  quedan.
- El runner cuenta como fallo un test que retorna distinto de 0 aunque no haya
  llamado a `fail`: un test de seguridad tiene que poder ponerse rojo muriendo.
- Una tarea que parece opción (`-- --help`) llega al hijo como prompt, detrás de
  un `--`, y `--append-system-prompt` sigue siendo la última opción.
- En el piso, el `git` del hijo no corre ningún helper que nombre la config del
  repo o la global (`diff.external`, `textconv`, `core.fsmonitor`, `core.pager`,
  un filtro `clean`) y rechaza `--ext-diff`, `--textconv` y `--output`; con
  `--bash`, el `git` es el de la máquina.
- Un `env_passthrough` con cualquier nombre `GIT_*` se rechaza con 2.
- Un rechazo de `dg_perm_resolve` o de `dg_mcp_resolve` justo después de un grant
  no deja ningún global del grant, probado en el **mismo** shell: inspeccionar
  los globals del padre después de correr el resolver en un subshell no prueba
  nada.
- `DEEPSEEK_ACCOUNT` definida rechaza la corrida con 2, nombrando `DELEGATE_ACCOUNT`,
  antes de leer la key.
- Una entrada del environ con un nombre que Bash no puede nombrar (`SECRET-TOKEN`,
  `A.B`) no llega al hijo, y la key sigue sin aparecer en ningún argv.
- Invocado por nombre, nada del shell que llama corre antes de la limpieza: un
  `BASH_ENV`, una función exportada `set`, un `SHELLOPTS=xtrace` con un `PS4` que
  expande la key, y `allexport` (por `SHELLOPTS` o por `bash -a`), cada uno con su
  sentinela. Los tests de aislamiento ejecutan el script, no `bash bin/delegate`.
- El `grep` que filtra stderr no lleva el environment del padre ni la key: un
  `GITHUB_TOKEN` del shell que invoca no aparece en su `/proc/<pid>/environ`.

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
