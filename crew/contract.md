# Contrato de cada agente

Este contrato va con cada encargo. Cúmplelo completo. Nunca copies secretos,
credenciales ni contenido de `.env` en tus preguntas ni en tu entrega: nombra la
variable, nunca el valor.

## 1. Preguntas primero

Antes de escribir código, lee lo necesario. Manda en una sola tanda todas las
preguntas que cambian lo que vas a construir: numeradas y cada una con tu
recomendación. No preguntes lo que ya está en el repo. Junto con las preguntas,
propón qué significa "terminado": qué va a poder hacer Carlos y cómo lo va a
probar. Luego termina tu turno y espera. La última línea de tu mensaje final debe
ser exactamente `CREW: PREGUNTAS`.

## 2. Después, hasta el final

Con las respuestas, trabaja sin volver a preguntar: implementa, prueba y revisa.
Si surge una duda que no bloquea, toma la suposición que recomendaste, anótala y
lístala en la entrega. Solo un bloqueo real te detiene de nuevo: si no puedes
seguir sin una decisión o un acceso que no tienes, explica qué necesitas y
termina tu turno con la última línea de tu mensaje final exactamente
`CREW: BLOQUEADO`. No uses esa marca para dudas menores ni para pedir permiso.

## 3. Más de lo pedido, en el cómo y no en el cuánto

No te salgas del feature: no toques otras secciones ni "aproveches" para cambiar
lo que no se pidió. Sí reutiliza lo que ya existe, piensa en que escale y sigue
buenas prácticas. No escribas código que la tarea no necesita: primero
reutilizar, después la plataforma, al final lo mínimo nuevo. Lo que mejoraría
fuera del alcance lo propones en la entrega, sin hacerlo.

## 4. Probar antes de entregar

Corre los tests que tocan tu cambio, no la suite entera. Si hay UI, recórrela en
el navegador con `playwright-cli` contra lo que acordaron como "terminado". Pasa
Thermos (calidad y bugs) y anti-slop si hay UI.

## 5. Entrega

Empuja una rama, sin PR, para no disparar el CI: Carlos decide cuándo abrir el PR
o juntarla con otras. Escribe un resumen corto con:

- qué hiciste;
- qué probaste y cómo;
- qué debe revisar Carlos y dónde;
- las suposiciones que tomaste;
- las propuestas fuera de alcance.

Termina tu turno con la última línea de tu mensaje final exactamente
`CREW: ENTREGA`.
