# ADR-007 — Generación de autorreferencias compuestas multi-tenant

- **Estado**: aceptada
- **Fecha**: 2026-07-22
- **Referencias**: issue #44, ADR-004, revisión adversarial del PR #45

## Contexto

Una autorreferencia compuesta como `(tenant_id, previous_id) ->
(tenant_id, id)` puede tener un discriminador `NOT NULL` y una columna
jerárquica nullable. `RelationshipSpec.nullable` describe la nulabilidad de la
FK completa, por lo que no basta para decidir si una raíz puede romperla bajo
`MATCH SIMPLE`. Además, una fase `InsertLeveledPhase` debe generar primero las
FKs externas que fijan el tenant; de lo contrario una PK UUID o el propio
discriminador aún no existe cuando se construye la jerarquía.

## Decisión

- `graph/strategies.py` decide que una autorreferencia es rompible con
  `nullable_columns` no vacío bajo `MATCH SIMPLE`, y exige que todas las
  columnas sean anulables bajo `MATCH FULL`.
- `generation/engine.py` mantiene un estado de selección por FK y por tabla.
  Cuando varias FKs comparten columnas, procesa primero las obligatorias y
  filtra los padres por los valores locales ya fijados y descarta de antemano
  candidatos que no tienen soporte en las FKs obligatorias restantes. Una FK
  parcialmente nullable puede anular solo su subconjunto nullable; una FK
  obligatoria sin padre compatible produce `GenerationError` con tabla,
  columnas y valores. Si el padre está completamente en cuarentena y el modo
  es `on_error=quarantine`, la FK queda sin resolver para que la fila hija
  también se aparte y el cierre RI pueda continuar.
- La factibilidad de un candidato depende solo de los valores compartidos que
  fija, no de qué fila lo pide, así que el filtro por FKs obligatorias se
  memoiza por el conjunto de valores locales ya fijados (`filtered_candidates_cache`).
  Las proyecciones por columnas compartidas se construyen una vez por tabla y
  el descarte deja de recorrer todos los padres por cada fila: el coste es
  lineal, no filas × padres. La caché se omite cuando una FK obligatoria
  restante usa `unique_subset` compartido, cuyo soporte se agota fila a fila.
- La **cuota** es un contrato de tabla. Sobre FKs compartidas se reparte solo
  entre padres *utilizables* (con combinación compatible en las demás FKs
  obligatorias); si `min > 0` y algún padre no lo es, o si los utilizables no
  alojan las filas, se rechaza con `GenerationError` que nombra la FK, la cuota
  y la incompatibilidad. Nunca se sustituye una asignación de cuota incompatible
  por un padre aleatorio (eso incumpliría `min/max` en silencio).
- **Varias cuotas compartidas se coordinan** por su discriminador común: cada
  grupo (valor compartido) recibe un número de hijos factible *a la vez* para
  todas las cuotas —la intersección de `[|padres_g|·min, |padres_g|·max]`— y
  luego cada cuota reparte esos hijos entre sus padres del grupo. Prepararlas por
  separado y solo priorizar una hacía que la segunda chocara con el tenant que la
  primera fijaba; la coordinación resuelve toda asignación conjunta factible y,
  cuando no existe, falla con un error que nombra tabla, relaciones, cuotas y el
  grupo en conflicto.
- La **tabla puente** deduplica reconsiderando el par completo (o cualquier
  izquierda con cualquier derecha si no comparten columnas), pero consumiendo una
  enumeración perezosa de combinaciones válidas con un cursor incremental: cada
  combinación se inspecciona a lo sumo una vez en toda la tabla, sin reconstruir
  el producto cartesiano por colisión ni materializarlo cuando se piden pocas
  filas. Si se agotan las combinaciones válidas produce `GenerationError` con la
  tabla, la cardinalidad solicitada y las combinaciones disponibles.
- `InsertLeveledPhase` reutiliza esa selección para las FKs externas, fija el
  padre del nivel anterior antes de generar la fila y, en raíces
  `roots_point_to_self`, asigna la autorreferencia después de generar la PK y
  los valores compartidos.

No se modifica la IR, el parser ni `ir/plans.py`.

## Consecuencias

La salida sigue siendo determinista por fila e independiente de
`batch_size`; `Dataset.levels`, `KeyStore` y el cierre de integridad reflejan
solo las filas aceptadas. Los casos obligatorios sin combinación de padres
válida —o una cuota o un puente sin combinaciones compatibles— dejan de
producir `KeyError`, filas inválidas silenciosas o cuotas incumplidas y
requieren corregir la cardinalidad/configuración del esquema. Tanto el filtro de
FKs obligatorias como la deduplicación del puente escalan de forma lineal en
filas y padres. La medición de ese trabajo es **instrumentación privada de los
tests** (`engine._SELECTION_WORK`, no una API pública): las regresiones de
complejidad verifican una cota estructural sin comprometer una API ni depender de
un umbral temporal frágil.
