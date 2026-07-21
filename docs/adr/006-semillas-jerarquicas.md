# ADR-006 — Semillas jerárquicas: una `Random` independiente por fila

- **Estado**: aceptada
- **Fecha**: 2026-07-21
- **Referencias**: especificación §13, `src/synthdb/generation/seeding.py` (T2.1),
  CLAUDE.md (principio «determinismo y reproducibilidad»),
  [ADR-003](003-ir-congelada.md) (hash canónico del que depende la reproducibilidad)

## Contexto

La promesa central del proyecto es la reproducibilidad byte a byte: misma semilla
global + mismo plan ⇒ mismos bytes (verificado en CI por el test de reproducibilidad
del Hito 2). Para sostenerla, la aleatoriedad no puede depender de nada que varíe
entre ejecuciones o plataformas: ni `random` global, ni `datetime.now()`, ni el
orden de iteración de un `set`/`dict`, ni el tamaño de lote con el que se genere.

Un RNG **secuencial por tabla** (avanzar un único `Random` fila tras fila) no
cumpliría esto: cambiar `output.batch_size`, paralelizar o reordenar la generación
cambiaría qué valores caen en qué fila. El código ya resuelve esto en
`generation/seeding.py` (T2.1), pero el **esquema de derivación** no estaba
registrado como decisión.

## Decisión

Toda la aleatoriedad del motor cuelga de dos funciones puras y deterministas, en
dos niveles:

1. **`seed_for_table(seed_global, tabla)`** deriva la semilla de una tabla
   mezclando la semilla global del usuario con el nombre canónico de la tabla vía
   **BLAKE2b** (con framing de longitud, libre de colisiones, y personalización de
   dominio). Estable entre ejecuciones y plataformas.
2. **`rng_for_row(seed_tabla, indice)`** construye un `random.Random`
   **independiente por cada fila**, sembrado desde `(seed_tabla, indice)` con
   BLAKE2b (dominio separado del de tabla).

La consecuencia clave: el valor de la fila *i* depende **solo** de
`(seed_tabla, i)`, nunca del tamaño de lote, del orden de generación ni de una
futura paralelización. Queda **prohibido** en las rutas de generación: `random`
global, `datetime.now()`, la iteración sobre `set`/`dict` sin orden definido y
cualquier fuente de entropía del sistema (CLAUDE.md).

## Consecuencias

- **Independencia del tamaño de lote**: el test del motor genera el mismo dataset
  con `batch_size` 5 y 5000 y obtiene bytes idénticos; el emisor produce el mismo
  `seed.sql` y los mismos CSV.
- **Base para el Hito 4**: el emisor de base de datos regenera desde cero con las
  mismas semillas tras un lote piloto revertido, garantizado por diseño.
- **Base para el Hito 3B**: el muestreo de vocabularios de IA y el `ContentStore`
  se apoyan en el RNG de fila para ser reproducibles.
- El `ContentStore` y la caché de planes se apoyan además en el hash canónico
  ([ADR-003](003-ir-congelada.md)); ambos deben permanecer estables por la misma
  razón. Cambiar el esquema de derivación invalidaría toda salida ya generada:
  exige un ADR nuevo.
