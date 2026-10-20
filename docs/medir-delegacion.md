# Medir si delegar vale la pena

Dos lados que comparar en la misma ventana de días: lo que hicieron los delegados
(`delegate-run stats`) y lo que gastó el modelo principal en Claude Code (`ccusage`).

## Lado delegado: `delegate-run stats`

```
delegate-run stats                     # últimos 7 días, tabla legible
delegate-run stats --days 14 --provider deepseek
delegate-run stats --days 14 --json    # lo mismo, en JSON
```

Por proveedor/modelo, y con una fila `<proveedor>/* (todas)` por proveedor:

- **corridas** y **estados** (`completed`, `checks_failed`, `task_failed`, `timed_out`…).
  Las corridas vivas no cuentan (salen como "en curso"); una que dice `running` sin
  runner vivo cuenta como `abandoned`.
- **éxito 1.er intento**: `completed` / corridas. Una corrida que murió antes de que
  el harness respondiera no trae modelo y cae en `<proveedor>/unknown`: la fila
  `/* (todas)` es la que no la pierde.
- **duración p50 / p90**: `finished_at - started_at` (incluye espera de slot y
  checks), percentil por rango más cercano. `harness_seconds` en `result.json` es
  solo el tiempo del agente.
- **turnos p50**: `num_turns` del harness (o de `stdout.log` en corridas viejas).
- **tokens por corrida**: entrada, salida, caché leída y caché escrita, promediados
  solo sobre las corridas con uso conocido; las demás se cuentan en "sin uso
  conocido".
- **costo estimado**: solo el de las corridas con uso **y** precio. Las demás se
  cuentan aparte y el total pasa a ser una cota inferior. Un desconocido nunca es 0.
- **archivos/corrida** y **fuera de scope** (corridas con `scope_violation`).

## Lado principal: `ccusage`

`ccusage` lee los transcripts locales de Claude Code (`~/.claude/projects`) y
suma tokens por día. Corre con `npx`, sin instalar nada global (queda en la caché
de npm):

```
npx --yes ccusage@20.0.29 claude daily --since 20261001
npx --yes ccusage@20.0.29 claude daily --since 20261001 --json
```

Verificado con la versión 20.0.29. Tres cuidados al leerlo:

- **Los delegados aparecen ahí también.** Las corridas del harness que guardan
  sesión dejan transcripts, así que `ccusage claude` lista `deepseek-flash` junto a
  los modelos de Claude. Para el lado principal, quédate con los `modelName` que
  empiezan por `claude-` en `modelBreakdowns`:

  ```
  npx --yes ccusage@20.0.29 claude daily --since 20261001 --json | python3 -c '
  import json, sys
  for d in json.load(sys.stdin)["daily"]:
      m = [b for b in d["modelBreakdowns"] if b["modelName"].startswith("claude-")]
      print(d["date"], sum(b["inputTokens"] + b["outputTokens"] + b["cacheReadTokens"]
            + b["cacheCreationTokens"] for b in m), round(sum(b["cost"] for b in m), 2))'
  ```

- **El costo de `ccusage` es a precio de lista de la API.** Con una suscripción
  (Max) no es lo que se paga, sino el equivalente; sirve para comparar volumen, no
  facturas. Y el costo que le pone a `deepseek-flash` no sale de `prices.tsv`: el de
  los delegados es el de `delegate-run stats`.
- `/cost` dentro de Claude Code da solo la sesión actual; para días, `ccusage`.

## Cómo compararlos

En la misma ventana (`--days 14` y `--since` catorce días atrás): tokens y costo
que se fueron a delegados contra los del modelo principal, y la tasa de éxito al
primer intento. Una corrida que no termina en `completed` cuesta además el turno
del principal que la revisa o la rehace, y eso solo se ve en `ccusage`.
