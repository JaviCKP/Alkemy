# ADR-008 — Dimensiones propias de una UNIQUE compuesta

- **Estado**: aceptada
- **Fecha**: 2026-07-22
- **Referencias**: Issue #47, PR #49, `generation/_table_assignment.py`

## Contexto

Una `PRIMARY KEY` o `UNIQUE` compuesta puede cubrir columnas escritas por varias
FKs compuestas. La capacidad real no es el producto de las filas físicas de los
padres cuando varias versiones proyectan los mismos valores en la restricción.
Además, una tercera FK puede compartir el discriminador (`tenant_id`) sin
aportar ninguna columna propia a la UNIQUE. Contarla como dimensión reduce
incorrectamente la capacidad y mezcla su cuota con el solver de unicidad.

## Decisión

`CompoundUniqueConstraint` conserva dos conjuntos deterministas:

1. `relations`: todas las FKs que intersecan la restricción y deben seguir siendo
   compatibles con la fila.
2. `dimensions`: las FKs que aportan al menos una columna de la UNIQUE que no
   aparece en otra FK participante. Solo estas FKs multiplican la capacidad.

La ruta del contrato compuesto tiene prioridad sobre la optimización de tablas
bridge. Deduplica cada dimensión por la proyección exacta de sus columnas de la
UNIQUE, calcula capacidad y asigna pares o tuplas sin materializar el producto
cartesiano. Después, `TableAssigner` completa las demás relaciones con sus
propias cuotas, `unique_subset`, nulabilidad y compatibilidad con los valores ya
fijados. La optimización bridge se mantiene únicamente cuando no hay contrato
compuesto que deba gobernar la UNIQUE.

## Consecuencias

- Las versiones físicas que comparten una proyección UNIQUE ya no inflan la
  capacidad disponible.
- Una FK que solo aporta el discriminador no convierte su padre en una dimensión,
  pero su RI y sus restricciones de asignación se conservan.
- Los errores de capacidad se producen antes de generar filas y son estables entre
  semillas y tamaños de lote.
- La IR, `TableSpec.kind`, las capas de parsing/constraints/graph y la API pública
  no cambian.
