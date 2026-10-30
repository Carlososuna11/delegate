# Crew: dirigir un equipo de agentes desde el chat

Fecha: 2026-10-29. Estado: diseño de la fase 1, pendiente de aprobación.

`delegate` manda una tarea a **un** trabajador barato y aislado. Crew dirige al **equipo**: convierte lo que Carlos dicta en el chat en encargos para agentes que corren en paralelo, casi siempre en caracas, y deja el chat libre.

## Por qué

Carlos avanza más cuando dicta en un chat y la sesión delega el trabajo a agentes en caracas (Paseo). Hoy eso depende de que la sesión entienda cada vez qué hacer. Falla de tres maneras:

- **Hace el trabajo ella misma.** A veces la sesión resuelve la tarea en el chat y lo bloquea.
- **Se olvida de lo lanzado.** Tras una compactación, la sesión pierde la cuenta de los agentes que lanzó.
- **Los agentes trabajan sin las reglas que importan.** Cada brief se escribe distinto: unos preguntan y otros no, unos se salen del pedido, otros entregan sin probar.

Carlos quiere dictar órdenes una tras otra ("ordena esto", "mejora el UI/UX de esta sección porque…", "implementa botón de pedido prioritario") y que cada una la lleve un agente hasta el final, mientras él sigue dictando o navegando el sitio.

## Enfoque: lo mínimo primero

La mayor parte del valor está en **cómo trabaja cada agente** y en que **la sesión siempre delegue**. Las dos cosas son texto (skill, contrato, una regla), no infraestructura. Por eso la fase 1 construye solo tres piezas, se usa una semana en Datago y después se decide qué más hace falta con datos reales. El resto queda abajo como hoja de ruta, sin compromiso.

## Fase 1: qué se construye

### 1. El contrato de cada agente

Es un texto que va en todos los briefs. Es la pieza más importante.

1. **Preguntas primero.**
   - Antes de escribir código, el agente lee lo necesario.
   - Manda en una sola tanda todas las preguntas que cambian lo que va a construir, numeradas y cada una con su recomendación. Nada de preguntas cuya respuesta está en el repo.
   - Junto con las preguntas propone qué significa "terminado": qué va a poder hacer Carlos y cómo lo va a probar.
   - Luego termina su turno y espera.
2. **Después, hasta el final.**
   - Con las respuestas, trabaja sin volver a preguntar: implementa, prueba y revisa.
   - Si surge una duda que no bloquea, toma la suposición recomendada, la anota y la lista en su entrega.
   - Solo un bloqueo real lo detiene de nuevo. Esto importa porque Paseo no entrega mensajes a un agente a mitad de turno.
3. **Más de lo pedido, en el cómo y no en el cuánto.**
   - No se sale del feature: no toca otras secciones ni "aprovecha" para cambiar lo que no se pidió.
   - Sí reutiliza lo que ya existe, piensa en que escale y sigue buenas prácticas.
   - No escribe código que la tarea no necesita. Sigue el criterio de Ponytail: primero reutilizar, después la plataforma, al final lo mínimo nuevo.
   - Lo que mejoraría fuera del alcance lo propone en la entrega, sin hacerlo.
4. **Probar antes de entregar.**
   - Corre los tests que tocan su cambio, no la suite entera.
   - Si hay UI, la recorre en el navegador con `playwright-cli` contra lo que acordó como "terminado".
   - Pasa Thermos (calidad y bugs) y anti-slop si hay UI.
5. **Entrega.**
   - Una rama empujada, sin PR, para no disparar el CI. Carlos decide cuándo abrir el PR o juntarla con otras.
   - Un resumen corto con:
     - qué hizo;
     - qué probó y cómo;
     - qué debe revisar Carlos y dónde;
     - las suposiciones que tomó;
     - las propuestas fuera de alcance.

### 2. El skill `crew` y la regla que lo dispara

- **Una regla en el `CLAUDE.md` global.**
  - Dice que "lanza un agente", "en paralelo", "en background", "delégalo a caracas", "mándalo a caracas", "que lo haga un agente/un deepseek" y "encárgalo" significan delegar.
  - Esas frases nunca significan "hazlo tú en este chat".
- **El skill `crew`:**
  1. Identifica proyecto, repo y rama base. Se apoya en el skill `caracas`: misma tabla de destinos, mismo manejo de lo no empujado.
  2. Separa el dictado en encargos, uno por orden. Los encargos dictados juntos sobre un mismo tema comparten prefijo de título (`<frente> · <encargo>`), para reconocerlos como lote.
  3. Arma cada brief: las palabras de Carlos citadas, las decisiones ya tomadas en el chat, las reglas del proyecto y el contrato. Nunca lleva secretos ni contenido de `.env`.
  4. Elige destino:
     - **caracas vía Paseo**, por defecto;
     - **DeepSeek** cuando Carlos lo pide. Va por `delegate-run --remote caracas --detach` tal cual, sin pasar por la fase de preguntas, porque es para trabajo mecánico;
     - **local en background** (subagente) cuando Carlos dice "aquí" o "local".
  5. Lanza y responde en una línea por encargo: título, destino y que el chat queda libre.
- **Comandos de consulta:**
  - **"¿Cómo van?"** muestra el estado de los encargos del proyecto en Paseo y las preguntas abiertas.
  - **"Qué pregunta el 2"** muestra las preguntas de ese encargo.
  - **"2: 1 sí, 2 la b"** se manda al agente con `paseo send`.
  - **"Cancela el 2"** lo detiene.

### 3. El hook que no deja olvidar

- **Cuándo corre:** un hook `UserPromptSubmit` (y `SessionStart`).
- **Qué hace:**
  - Mira los encargos de este proyecto en Paseo.
  - Si alguno terminó o espera respuesta desde el último mensaje, inyecta una línea. Por ejemplo: "#1 terminó · #2 espera tu respuesta hace 40 min".
  - Si no hay novedades, no dice nada.
- **Cómo consulta Paseo sin frenar el chat:** si caracas no responde, el hook no bloquea ni falla. Lee una copia que se refresca en segundo plano.
- **Qué cubre:** solo el proyecto de la carpeta actual.
- **Dónde funciona:** los hooks corren igual en la extensión de VSCode que en la terminal.

## Dónde vive

- Crew vive en este repo (`delegate`), como herramienta aparte, con su sección en el `CLAUDE.md`. Ahí van el skill, el contrato, el hook y lo que necesiten (por ejemplo `bin/crew`). `claude-config` los enlaza al instalar.
- Lo personal va en la capa local `~/.config/claude-config/local/`, igual que el skill `caracas` hoy: rutas de caracas, cwd de cada organización y cuentas.
- El orquestador de Innovares en caracas (`innovares-agent`) recibe el contrato en sus reglas, con PR en español, para que lo cumpla aunque reparta el trabajo a otros agentes.
- **Las invariantes de `delegate` no se tocan.**
  - Siguen valiendo para `delegate` y `delegate-run`: environment armado desde cero, sin MCP, sin hooks, sin daemon y sin elegir proveedor automáticamente.
  - Crew es un cliente de esos binarios y los usa como están.
  - Lo que Crew necesita y `delegate` prohíbe (elegir destino, lanzar agentes de Claude en Paseo) vive solo en el código de Crew.
  - Los tests de las invariantes siguen corriendo intactos.

## Restricciones

- Lo pesado corre en caracas, nunca en la laptop.
- Mandan las reglas existentes:
  - idioma: español neutro con "tú"; código y comentarios en inglés; Kontigo en inglés para PRs, commits y Linear;
  - una cuenta de `gh` por organización;
  - `git add` selectivo y nunca `--no-verify`;
  - en Kontigo, nunca se mergea;
  - DeepSeek nunca recibe secretos, PII ni datos de producción;
  - nada del entorno personal en textos para el equipo.
- Crew no cambia el estado de caracas (archivar, systemd) salvo que Carlos lo pida en el encargo.
- Los textos que Crew muestra no nombran el runtime ni el modelo ("Escribe en este chat", no "Responder a Codex"), igual que en Caracas Hub.
- Kontigo queda con el camino listo (`--cwd ~/kontigo/review-config`), pero sin ajuste fino en esta fase.

## Cómo se mide la semana de prueba en Datago

Al terminar la semana se revisan, con los chats y Paseo como evidencia:

1. Cuántas veces un agente se quedó esperando una respuesta, y cuánto tiempo.
2. Cuántas veces la sesión hizo el trabajo en vez de delegarlo.
3. Cuánto retrabajo hubo en las entregas: qué hubo que corregir y por qué.
4. Si las preguntas del inicio fueron útiles o ruido.

Con eso se decide qué piezas de la hoja de ruta construir.

## Hoja de ruta (sin compromiso; se decide tras la prueba)

- Recordatorios por correo si una pregunta lleva más de 30 minutos sin respuesta.
- Formato de Caracas Hub (estados, `asks:` y `hub-ask`), para que el Hub tenga datos cuando exista.
- Vistas nuevas en el Figma del Hub: Nuevo encargo, Frente de trabajo, Qué revisar y Variantes. Se diseñan primero en Figma y se construyen después.
- Modo variantes: N agentes con el mismo pedido y un evaluador que compara capturas. Inspirado en el Arena Mode de Grok Build.
- Para Datago:
  - rama de integración por frente;
  - CI pesado solo al salir de borrador;
  - tests afectados con `pytest-testmon` y `vitest --changed`;
  - un Datago de prueba en caracas con una copia reducida y enmascarada de producción;
  - la lista de "qué revisar" probada antes en el navegador.
- Ponytail instalado (laptop y caracas) tras revisar sus hooks y scripts.
- Ajuste fino de Kontigo.

## No metas de la fase 1

- Tablero propio o panel de VSCode: el tablero será Caracas Hub.
- Registro propio de encargos: el estado real está en Paseo.
- Merges automáticos.
- Cambios a `delegate` o `delegate-run`.

## Decidido por Carlos

- Disparo por frases; caracas como destino principal; local cuando él lo pide.
- Preguntas en el chat y en Paseo; que él no sea el cuello de botella.
- Límites:
  - el agente se queda dentro del feature;
  - "más de lo pedido" es reutilizable, escalable y buenas prácticas;
  - el resto se aprende.
- Seguimiento por proyecto, no global.
- La entrega es una rama empujada, sin PR.
- El Hub será el tablero y Crew la entrada.
- No adoptar Grok ni herramientas cerradas.
- Nombre: Crew. Vive en el repo de `delegate`.
- Empezar por lo mínimo y probarlo una semana en Datago.

## Decisiones mías (revisables)

- Los encargos a DeepSeek no tienen fase de preguntas: son para trabajo mecánico.
- La rama de trabajo sale de `main`. `v2` no tiene commits propios.
- El contrato pide acordar "terminado" junto con las preguntas, siguiendo el patrón de Anthropic de planificador, constructor y evaluador.

## Queda al constructor

- Cómo el hook guarda y refresca la copia del estado de Paseo, y cómo identifica los encargos del proyecto (por `cwd`, etiqueta o prefijo de título).
- Si el skill `crew` reemplaza al skill `caracas` o lo usa por debajo.
- El texto exacto del contrato, siempre que cubra los cinco puntos.
- Detalle de Paseo: el skill `caracas` documenta la 0.11.2 (`-d`), y caracas corre una versión más nueva (`--no-wait`). Verificar en caracas antes de escribir los comandos.
