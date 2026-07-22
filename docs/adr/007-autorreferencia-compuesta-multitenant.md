# ADR-007 — Asignación conjunta de FKs compuestas multi-tenant

- **Estado**: aceptada
- **Fecha**: 2026-07-22
- **Referencias**: issue #44, PR #45, ADR-004, `especificacion.md` §6.4 y §7.4

## Contexto

Una autorreferencia compuesta como `(tenant_id, previous_id) ->
(tenant_id, id)` combina un discriminador `NOT NULL` con una columna
jerárquica nullable. Bajo `MATCH SIMPLE`, una raíz puede anular solo las
columnas nullable; bajo `MATCH FULL`, debe anular la relación completa. La
generación por niveles también debe fijar primero las FKs externas que
determinan el tenant.

El mismo problema aparece en cualquier tabla con FKs compuestas que comparten
columnas locales. La implementación anterior seleccionaba cada FK por separado
y después mutaba índices para reparar conflictos o duplicados. Ese orden de
decisión podía romper RI, hacer coincidir patrones de `NULL` que no tenían que
coincidir, incumplir cuotas o reconstruir el producto cartesiano de un puente.

## Decisión

- `graph/strategies.py` sigue determinando las columnas anulables de una
  autorreferencia según `MATCH SIMPLE`/`MATCH FULL`. La IR, el parser y los
  planes no cambian.
- `generation/_table_assignment.py` contiene un `TableAssigner` privado. El
  motor le entrega todas las relaciones de la tabla, sus padres, estrategias,
  nulabilidad y relaciones diferidas. El asignador construye el grafo de FKs
  por columnas locales compartidas y separa sus componentes conexas; las
  componentes independientes se coordinan independientemente.
- Cada componente obtiene su discriminador común y se valida antes de
  consumir aleatoriedad. Una componente conectada sin discriminador universal
  se rechaza con un error determinista y accionable. La asignación conjunta se
  construye válida desde el principio; no hay una fase posterior de reparación
  o deduplicación que pueda sustituir solo una parte de la clave.
- Las máscaras de `NULL` se deciden por relación, no por componente. Una FK
  anulable bajo `MATCH SIMPLE` anula únicamente sus columnas anulables; un
  discriminador compartido no se anula por el hecho de que otra FK de la fila
  sea `NULL`. Las cuotas cuentan solo sus propias filas no nulas. Con
  `min=0`, incluso cuotas compartidas con patrones de `NULL` o `null_ratio`
  distintos se asignan con capacidades independientes.
- `uniform`, `zipf` y `unique_subset` eligen dentro del grupo compatible de la
  fila. Las cuotas se preparan por grupos compartidos, usando intervalos
  conjuntos de `min/max`, y se rechaza de forma estable cualquier combinación
  infactible; nunca se sustituye en silencio el padre impuesto por una cuota.
- Una tabla puente muestrea, sin reemplazo, índices del espacio de pares
  compatibles mediante un índice plano y `unrank`; no materializa todo el
  producto cartesiano. Si hay cuotas, genera grados en cada lado y construye
  simultáneamente un emparejamiento de pares únicos, conservando las cuotas de
  ambos lados o fallando determinísticamente.
- Todas las rutas usan semillas jerárquicas estables por tabla, relación,
  grupo y puente. La instrumentación de trabajo (`engine._SELECTION_WORK`) es
  privada y solo sirve para comprobar la complejidad estructural en tests.
- `InsertLeveledPhase` reutiliza el asignador para las FKs externas, fija el
  padre del nivel anterior antes de generar la fila y, en raíces con
  `roots_point_to_self`, asigna la autorreferencia después de generar la PK y
  los valores compartidos.

## Consecuencias

La salida es determinista por fila e independiente de `batch_size`. Las
asignaciones que llegan a la validación estructural ya respetan las claves
compuestas, RI, unicidad, `NULL` y cuotas; una contradicción de topología,
cardinalidad o cuota produce un `GenerationError` que nombra la tabla y la
causa. Las regresiones cubren máscaras de `NULL` independientes, componentes
conexas, puentes uniformes y cuotas a ambos lados, además de cotas de trabajo
lineales sin ampliar la API pública.
