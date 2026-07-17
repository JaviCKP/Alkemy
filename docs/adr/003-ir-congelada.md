# ADR-003 — La IR (`ir/schema.py`) queda congelada al cierre del Hito 1

- **Estado**: aceptada
- **Fecha**: 2026-07-17
- **Referencias**: CLAUDE.md (primer principio innegociable), especificación §5,
  `ir/hashing.py` (ADR implícito de T1.5), plan de ejecución §3 (T1.9)

## Contexto

El Hito 1 cierra el núcleo estructural: parser DDL (T1.3), catálogo de tipos
(T1.2), hash canónico (T1.5), interpretación de `CHECK` (T1.4) y grafo de
dependencias con estrategias de ciclo (T1.6–T1.7). Por el primer principio de
CLAUDE.md, la IR (`SchemaSpec` y sus modelos anidados en
`src/synthdb/ir/schema.py`) es **la única fuente de verdad estructural**: todo
aguas abajo (H2 generación, H3 semántica, emisión) la consume sin releer SQL ni
reinterpretar el esquema.

Dos garantías ya en vigor dependen de que la forma de la IR sea estable:

- **Los snapshots golden de IR por fixture** (syrupy,
  `tests/unit/parsing/__snapshots__/`), que son la red de seguridad del parser.
- **El hash canónico determinista** (`schema_hash`), del que dependen la caché
  de planes (ADR-002) y la reproducibilidad byte a byte que verifica el CI.

Un cambio no rastreado en `schema.py` —añadir, quitar, renombrar o cambiar la
semántica de un campo— rompería snapshots y/o alteraría el hash de esquemas ya
procesados, invalidando cachés y reproducibilidad sin dejar constancia de por
qué.

## Decisión

**A partir del cierre del Hito 1, `src/synthdb/ir/schema.py` queda congelada.**
Cualquier cambio de campo exige un **ADR nuevo** que registre, como mínimo:

1. el campo y el cambio exacto;
2. el motivo;
3. el impacto en el hash canónico (`ir/hashing.py`) y en los snapshots golden,
   incluida la regeneración justificada de los snapshots afectados (nunca en
   bloque para "poner verde").

**`src/synthdb/ir/plans.py` (`StructuralPlan`, `Phase` y sus variantes)
NO se congela todavía.** Es la salida del planificador estructural que el motor
de generación consumirá en el Hito 2; su forma final depende de necesidades que
solo se conocerán al implementar ese motor. Se congelará —con su propio ADR—
cuando el Hito 2 lo consuma y se cierre. Hasta entonces puede evolucionar
dentro del H2 sin ADR, pero con la disciplina habitual de tests y CHANGELOG.

### Inventario de partida: campos derivados excluidos del hash

Estos campos de la IR no proceden del DDL, sino que SynthDB los infiere a
partir de él; por eso `ir/hashing.py` (`_EXCLUDED_FIELDS`) los excluye del hash
(el hash identifica lo que el usuario escribió, no lo que SynthDB dedujo).
Quedan como inventario de partida de la congelación:

- `SchemaSpec.hash` — el propio hash (sería circular).
- `SchemaSpec.warnings` — ruido de ejecución, no estructura.
- `TableSpec.kind` — rol estructural (`regular`/`bridge`/`lookup`) inferido por
  `graph/dependency.py`.
- `RelationshipSpec.cardinality_hint` — cardinalidad (`many_to_one`/
  `one_to_one`/`self_reference`) inferida por `graph/dependency.py`.
- `CheckSpec.ast_supported` y `CheckSpec.bounds_derived` — cotas derivadas por
  `constraints/check_interp.py` (tanto en checks de tabla como de columna).

Todo campo derivado nuevo que se añada en el futuro debe entrar en
`_EXCLUDED_FIELDS` con su propio registro y —por esta ADR— vía ADR.

## Consecuencias

- La reproducibilidad (misma semilla + mismo plan ⇒ mismos bytes) y la caché de
  planes por hash quedan protegidas frente a erosión accidental de la IR.
- Dar soporte a DDL nuevo que introduzca un campo de IR (p. ej. exclusion
  constraints, `GENERATED`) deja de ser un cambio "de paso": requiere ADR.
- El equipo del Hito 2 tiene libertad sobre `ir/plans.py` hasta cerrarlo; esta
  ADR deja constancia de esa asimetría deliberada para que no se lea como un
  olvido.
- Los snapshots golden y el test de determinismo del hash pasan a ser la red que
  detecta en CI cualquier violación de esta congelación.
