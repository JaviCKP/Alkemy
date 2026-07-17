# ADR-004 — Lecciones del primer esquema real: descongelación puntual de la IR

- **Estado**: aceptada
- **Fecha**: 2026-07-18
- **Referencias**: [ADR-003](003-ir-congelada.md) (procedimiento de
  descongelación), CLAUDE.md (primer y segundo principios innegociables),
  especificación §5, `ir/hashing.py`, `docs/validaciones/` (validación del
  `schema.sql` de inmobiliaria, 2026-07-17)

## Contexto

La primera validación de SynthDB contra un esquema **real** de producción —una
inmobiliaria multi-tenant de 20 tablas sobre **PostgreSQL 15**
(`docs/validaciones/`)— destapó tres construcciones que el núcleo del Hito 1 no
representa y que hoy impiden analizar el esquema tal cual:

1. **`ON DELETE SET NULL (columna)`** — sintaxis de PostgreSQL 15+ que restringe
   la acción `SET NULL`/`SET DEFAULT` a columnas concretas de una FK compuesta.
   Aparece 21 veces en el esquema real; sqlglot 30.12.0 (la última publicada)
   rechaza la lista de columnas con `Expecting )` y aborta el análisis completo.
2. **Nulabilidad dirigida de FK compuestas.** El cálculo actual usa `all(...)`
   sobre las columnas locales de la FK: una FK es "anulable" solo si **todas**
   sus columnas lo son. En el esquema real la clave típica es
   `(inmobiliaria_id NOT NULL, entidad_id NULL)`: la FK entera no es anulable,
   pero la columna que cierra el ciclo (`entidad_id`) sí. El planificador la
   declaraba parte de un ciclo irrompible cuando en realidad se rompe anulando
   solo esa columna. La semántica correcta depende de `MATCH SIMPLE` (defecto)
   frente a `MATCH FULL`.
3. **Arrays (`text[]`, `numeric(7,2)[]`).** Se degradaban a su tipo escalar
   silenciosamente respecto a su naturaleza de array (`clientes.roles`,
   `busquedas.tipos`, `busquedas.zonas`, `faq.etiquetas`).

La IR (`src/synthdb/ir/schema.py`) quedó **congelada** al cierre del Hito 1
(ADR-003): todo cambio de campo exige un ADR nuevo que registre el campo, el
motivo y el impacto en hash y snapshots. Este ADR es ese registro.

## Decisión

Se añaden cuatro campos a la IR y se ajusta la semántica de uno existente.

### Cambios estructurales (SÍ entran en el hash canónico)

- **`TypeSpec.is_array: bool = False`.** El `kind` sigue siendo el del
  **elemento** (`text[]` ⇒ `kind="text"`, `is_array=True`; `numeric(7,2)[]`
  conserva `precision`/`scale`). Se detecta desde el AST de sqlglot
  (`DataType` con `this == ARRAY`), **nunca** desde el texto. Arrays
  multidimensionales (`text[][]`) se tratan como **una** dimensión y emiten un
  aviso (PostgreSQL tampoco distingue el número de dimensiones en la práctica).
  La **generación** de arrays es del Hito 2; aquí solo hay representación.
- **`RelationshipSpec.on_delete_set_columns: list[str] = []`.** Las columnas de
  la lista de `ON DELETE SET NULL/SET DEFAULT (…)`, con el plegado de
  identificadores de PostgreSQL aplicado. Debe ser un subconjunto de `columns`;
  si no lo es, se emite un aviso. Solo tiene sentido con
  `on_delete ∈ {set_null, set_default}` (invariante garantizada por
  construcción: la lista solo puede aparecer tras esas acciones).
- **`RelationshipSpec.match_full: bool = False`.** `True` cuando el DDL declara
  `MATCH FULL` en la FK. `MATCH SIMPLE` (defecto) y `MATCH PARTIAL` dejan el
  campo en `False`. Cambia la política de rotura de ciclos por NULL.

### Campo derivado (EXCLUIDO del hash)

- **`RelationshipSpec.nullable_columns: list[str] = []`.** Las columnas locales
  de la FK que admiten `NULL`, derivadas de la nulabilidad de cada columna
  (que ya está en la IR). Es información redundante, por eso queda **excluida
  del hash** (`ir/hashing.py::_EXCLUDED_FIELDS`), igual que el resto de campos
  derivados del inventario de ADR-003. El campo `nullable` existente **se
  mantiene** con su semántica actual ("todas las columnas de la FK admiten NULL"
  ⇒ la FK entera puede quedar sin rellenar) y su docstring se amplía para
  distinguir ambos conceptos: `nullable` es el AND global; `nullable_columns` es
  el detalle por columna que necesita la rotura de ciclos bajo `MATCH SIMPLE`.

### Semántica de rotura de ciclos (`graph/strategies.py`)

La condición de "ciclo rompible por NULL" pasa a ser, por FK:

- **`MATCH SIMPLE` (defecto)** ⇒ rompible si `nullable_columns` no está vacía.
  Se insertan a `NULL` **solo** esas columnas y una `UpdatePhase` posterior las
  rellena. El resto de columnas de la FK (p. ej. `inmobiliaria_id`) reciben su
  valor real en el `INSERT`.
- **`MATCH FULL`** ⇒ rompible solo si **todas** las columnas de la FK admiten
  `NULL` (un `NULL` parcial viola `MATCH FULL`).

`InsertPhase.null_fks` y `UpdatePhase` registran ahora **qué columnas** se
anulan (`FkRef.null_columns`), no solo de qué FK se trata.

## Impacto en hash y snapshots (regeneración en bloque justificada)

Añadir los tres campos **estructurales** (`is_array`, `on_delete_set_columns`,
`match_full`) a los modelos cambia su serialización canónica (`canonical_json`)
aunque tomen su valor por defecto: **todos** los hashes de esquema cambian y
**todos** los snapshots golden de IR (`tests/unit/parsing/__snapshots__/`) se
regeneran **en este mismo PR**. El campo derivado `nullable_columns` cambia
`canonical_json` (aparece en los snapshots) pero **no** el hash (está excluido).

Esta es la **única** regeneración en bloque legítima: la que un ADR ordena
explícitamente (CLAUDE.md prohíbe regenerar snapshots "para poner verde"). Se
revisan a ojo antes de comitear. **No hay cachés en producción que invalidar**
(el proyecto está pre-alpha; la caché de planes de ADR-002 aún no existe), así
que el cambio de hash no rompe nada aguas abajo.

## Inventario actualizado de campos derivados excluidos del hash

Amplía el inventario de partida de [ADR-003](003-ir-congelada.md):

- `SchemaSpec.hash` — el propio hash (sería circular).
- `SchemaSpec.warnings` — ruido de ejecución, no estructura.
- `TableSpec.kind` — rol estructural inferido por `graph/dependency.py`.
- `RelationshipSpec.cardinality_hint` — cardinalidad inferida por
  `graph/dependency.py`.
- `RelationshipSpec.nullable_columns` — **nuevo (ADR-004)**: columnas locales
  anulables de la FK, derivadas de la nulabilidad de cada columna.
- `CheckSpec.ast_supported` y `CheckSpec.bounds_derived` — cotas derivadas por
  `constraints/check_interp.py`.

## Consecuencias

- La IR vuelve a quedar **congelada** tras este PR: el próximo cambio de campo
  exige otro ADR (ADR-003 sigue vigente; este es una descongelación puntual, no
  su derogación).
- El parser de PostgreSQL de sqlglot se extiende mediante **subclase del
  dialecto** (mecanismo oficial), nunca con regex ni preprocesado del texto SQL
  (CLAUDE.md). La extensión vive en `src/synthdb/parsing/dialect.py`.
- El soporte de `MATCH FULL` es estructural (entra en el hash) porque cambia qué
  secuencias de carga son válidas; `MATCH SIMPLE`/`PARTIAL` no se distinguen del
  defecto.
- La **generación** de valores de array queda pendiente del Hito 2. Hasta
  entonces, un array se representa y se analiza, pero su relleno se decidirá con
  el motor de generación.
- El esquema real completo del usuario **no** se incorpora al repositorio (es de
  otro proyecto); el patrón se reproduce en el fixture mínimo
  `tests/schemas/crm_real_minimo.sql` y la verificación contra el esquema real
  se hace en local y se reporta en el PR.
