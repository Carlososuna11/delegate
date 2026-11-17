Cuando escribes código en esta corrida:
ANTES de escribir
1. @REPO_RULES@
2. Abre un archivo vecino del que vas a crear o cambiar (mismo directorio, mismo tipo) y copia su forma: imports, nombres, manejo de errores, estructura de los tests.
3. Por cada helper, constante, tipo o componente que necesites, búscalo antes de crearlo: grep -rn por el nombre probable y por el valor literal. Si existe, impórtalo. Si casi sirve, extiéndelo donde está.
MIENTRAS escribes
4. Un valor (número, texto, ruta, regex, clave) que usan dos archivos se define una vez, en el módulo de constantes o config que el repo ya tiene, y se importa. Nunca repitas el literal.
5. La lógica va en la capa donde el repo ya pone esa clase de lógica. No copies bloques entre archivos: extrae un helper compartido.
6. Sin dependencias nuevas, sin refactors fuera de la tarea, sin envoltorios de una línea, sin abstracciones de un solo uso, sin código muerto ni prints de depuración.
ANTES de terminar
7. Corre git status y git diff. Por cada constante y función nueva, grep de su valor y de su nombre en el resto del repo; si ya existía, unifica.
8. Termina con una sección "Reutilización": qué reusaste (archivo y símbolo), qué creaste nuevo y con qué grep comprobaste que no existía, y qué no pudiste verificar.
