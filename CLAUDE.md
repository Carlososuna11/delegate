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
   solo por el environment del proceso hijo.
2. **La key se lee una sola vez**, justo después de validar argumentos y config. Una vez
   leída, un cambio de cuenta activa hecho por otro proceso no puede desviar la corrida
   en curso.
3. **Aislamiento.** Sin servidores MCP (excepción: Figma, solo con `--figma` /
   `--figma-write`), sin hooks, sin plugins, sin settings de usuario, sin memoria. El
   delegado no alcanza bases de datos, Datadog, Sentry, Retool, Linear ni APIs de GitHub.
4. **Permisos graduados, y solo hacia arriba.** Solo lectura por defecto; `--edit` agrega
   escritura de archivos; `--bash` agrega shell sin restricción. El techo de capacidades
   del proveedor (`caps`) gana sobre los flags: si no incluye `bash`, `--bash` se
   **rechaza**, no se ignora en silencio.
5. **Otras CLIs de IA bloqueadas en el PATH del hijo.** Un delegado no invoca `codex` ni
   `claude` para gastar cuota cara.
6. **La config se parsea, nunca se `source`a.** No puede inyectar MCPs arbitrarios,
   fragmentos de shell ni variables de entorno.

## Superficie

Ejecutable `delegate`, con `dg` como abreviación (el mismo script, no una copia).
Proveedor como subcomando; modo agente por defecto.

```
delegate deepseek "revisa src/auth.ts y lista los error paths"
delegate deepseek --edit --bash --account kontigo "arregla y corre los tests"
delegate qwen --timeout 2400 "segunda opinión de este PR"
delegate deepseek chat "explica este regex"      # sin herramientas

delegate account deepseek list|add|use
delegate providers
delegate help
```

## Proveedor = data

Un archivo por proveedor en `providers.d/`, formato `clave<TAB>valor`.

**En config:** `endpoint`, `auth_env`, `key_dir`, `key_pattern`, `default_account`,
`model`, `small_model`, `timeout`, `caps`, `residency`.

**En el script, no configurable:** validación de argumentos y config, el lattice de
permisos, los allowlists exactos de herramientas y MCP, el enforcement de `caps`,
`--bare`, la restricción de setting-sources, la limpieza del environment, timeout y
stdin, el bloqueo de otras CLIs, el chequeo de permisos de la key y su inyección por
environment, y las integraciones ya vetadas (Figma).

El parser rechaza claves desconocidas, claves duplicadas, tabs o newlines en los valores,
IDs de proveedor inválidos y variables de auth no soportadas. Sin `source`, sin `eval`,
sin JSON/YAML/TOML: sumarían parser y dependencia para un esquema plano de diez líneas.

## Cuentas

`<key_dir>/api_key.<cuenta>` (modo 600) es la fuente de verdad. `--account <nombre>` y
`DEEPSEEK_ACCOUNT` resuelven directo a uno de esos archivos. La cuenta por defecto vive
en un archivo de metadata no secreto, para no depender de estado global mutable.

`api_key` sin sufijo se mantiene **solo como proyección de compatibilidad** para
`cc-proveedor`, que es Python y lee esa ruta fija.

**Reglas duras:**

- El symlink de la cuenta activa se reemplaza **atómicamente**: symlink temporal más
  `rename(2)` (`mv -T`). **Nunca `ln -sfn`**: con `-f` desvincula el destino antes de
  crear el nuevo, así que deja una ventana en la que la ruta no existe y un lector
  concurrente falla.
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
- Sin `--edit` no hay escritura; sin `--bash` no hay shell.
- `caps` del proveedor gana sobre los flags pedidos.

## Lo que NO se construye

Nombrado para que no reaparezca: daemon, locking de cuentas, keyring o Vault, base de
datos de costos, DSL de políticas, sistema de plugins, registry generalizado de MCPs,
fallback o enrutamiento automático entre proveedores (manda código a la jurisdicción o a
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
