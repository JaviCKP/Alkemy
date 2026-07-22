# ADR-007 — Generación de autorreferencias compuestas multi-tenant

- **Estado**: aceptada
- **Fecha**: 2026-07-22
- **Referencias**: issue #44, ADR-004

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
- `InsertLeveledPhase` reutiliza esa selección para las FKs externas, fija el
  padre del nivel anterior antes de generar la fila y, en raíces
  `roots_point_to_self`, asigna la autorreferencia después de generar la PK y
  los valores compartidos.

No se modifica la IR, el parser ni `ir/plans.py`.

## Consecuencias

La salida sigue siendo determinista por fila e independiente de
`batch_size`; `Dataset.levels`, `KeyStore` y el cierre de integridad reflejan
solo las filas aceptadas. Los casos obligatorios sin combinación de padres
válida dejan de producir `KeyError` o filas inválidas silenciosas y requieren
corregir la cardinalidad/configuración del esquema.
