# ADR-009 — Frontera entre propuesta semántica y artefacto de plan

- **Estado**: aceptada
- **Fecha**: 2026-07-23
- **Referencias**: issue #51, ADR-002, ADR-003, ADR-005,
  `src/synthdb/semantic/llm/contract.py`,
  `src/synthdb/semantic/plan_artifact.py`

## Contexto

La especificación §8 describía una sola respuesta que mezclaba inferencias del
modelo con campos cercanos a la ejecución (`params` abiertos, reglas,
`depends_on` y `null_ratio`). Esa forma no expresa una frontera suficiente: una
respuesta válida según Pydantic seguiría pareciendo un plan autorizado.

El Hito 0 también dejó dos poblaciones distintas. El test heurístico evaluaba
cinco ficheros e incluía solo la variante nullable de RR. HH.; `RESULTS.md`
incluía las dos variantes, pero recorría las columnas devueltas por cada
respuesta. Por eso los denominadores de generador eran 252, 255 o 315. Las
labels fueron redactadas por Claude y su segundo repaso humano continúa
pendiente.

## Decisión

Se separan dos contratos que no son intercambiables:

1. **`SemanticProposal` es entrada no confiable.** Todos sus modelos usan
   `extra="forbid"` y tipos estrictos. Los generadores forman una unión
   discriminada con parámetros tipados. Solo puede proponer `faker`, `choice`,
   `numeric_range`, `datetime_range`, `template`, `sequence` o `uuid`; no puede
   proponer estructura, relaciones, claves concretas, `null_ratio`,
   `depends_on`, `text_pool`, `llm_text` ni `llm_group`. Cada string escalar
   queda acotado a 4096 caracteres y la respuesta completa a 1 000 000 bytes;
   `validate_semantic_proposal_json` aplica el techo antes de parsear la
   respuesta del proveedor.
2. **`ResolvedPlanArtifact` es un artefacto distinto.** Sus parámetros usan los
   mismos modelos Pydantic que el catálogo ejecutable actual. Solo se crea y se
   convierte a `TablePlans` después de comprobar contra `SchemaSpec` el hash,
   las tablas, las columnas, su orden y la compatibilidad de cada decisión con
   tipo, dominio, nulabilidad, unicidad, PK/FK y gestión por la base de datos.
   `generator=None` solo es válido para `autoincrement` o `GENERATED`. La IR no
   se copia ni se amplía: sigue siendo la única autoridad estructural.

Las reglas y estrategias FK del modelo son `ProposedRule` y
`ProposedRelationshipHint`. Una regla debe compilar en el mini-DSL seguro, pero
eso solo prueba su forma. Un hint referencia una FK existente mediante
`fk:<sha256>` calculado desde la IR; no describe una relación nueva. Ninguno
entra en `ResolvedPlanArtifact` ni en `TablePlans` de forma automática.

Toda identidad de tabla contractual es el par `(schema_name, table_name)`.
También forma parte de evidencias, reglas, hints, columnas e IDs de relación.
Así `a.items` y `b.items` pueden coexistir sin colisión. El `TablePlans` del H2
solo conserva el nombre simple: el puente `to_table_plans` rechaza de forma
explícita ese caso ambiguo en vez de entregar al motor un plan incorrecto. La
eliminación de esa limitación del consumidor no se adelanta a H3-R2.

## Contratos y fronteras de confianza

`validate_proposal_against_schema` comprueba el hash, la identidad cualificada,
la compatibilidad tipo-generador y los dominios cerrados. Una evidencia de
comentario o constraint solo es válida si ese contenido existe realmente en la
IR. El AST de cada regla se recorre completo: una referencia
`parent(<fk>).<columna>` debe resolver una única FK local y una columna real de
la tabla padre. Devuelve la misma propuesta: no resuelve ni autoriza nada.

`ResolvedPlanArtifact.create(schema=...)` es la frontera de datos confiables.
`to_table_plans(schema)` repite la comprobación estructural antes de entregar el
plan al consumidor existente. Esta segunda comprobación protege también los
artefactos recargados de disco.

Disposición de campos:

- versión, `schema_hash` e identificadores de propuesta: selección del contrato
  y validación contra la IR;
- entidad, rol, generador, confianza, evidencia e incertidumbres: entrada
  auditable para la política de fusión de H3-R2; no se ejecutan en H3-R1;
- reglas y hints FK: candidatos auditables; H3-R2 debe aceptarlos o descartarlos
  explícitamente y registrar el motivo;
- tablas, columnas y generadores resueltos: proyección ejecutable sellada por
  `fingerprint`;
- fuente, confianza y rol resueltos: trazabilidad sin efecto en generación,
  excluida de `fingerprint` pero sellada por `audit_fingerprint`;
- fecha, tokens, latencia y mensajes: diagnóstico persistido, excluido por
  contrato de ambas huellas.

No queda un campo aceptado sin consumidor o disposición declarada.

## Versionado, canonicalización y fingerprint

Versiones iniciales:

- `semantic-proposal/1`;
- `resolved-plan/1`;
- `plan-canonicalization/1`;
- `merge-policy/1`;
- `rule-dsl/1`;
- `generator-catalog/1`;
- `seed-derivation/1`.

La canonicalización v1 usa JSON UTF-8, claves ordenadas, sin espacios y con
listas en orden contractual. Las tres últimas versiones fijan la semántica que
no vive en el JSON de una decisión: gramática/intérprete de reglas, catálogo e
implementación de generadores, y derivación jerárquica de semillas. Cualquier
cambio incompatible en esas superficies obliga a incrementar su versión.

`fingerprint` es SHA-256 de todas las versiones, `schema_hash` y la proyección
ejecutable: identidades cualificadas, orden, columnas, generadores, parámetros,
`null_ratio` y unicidad. `source`, `confidence` y `role` no cambian los datos y
por tanto no invalidan cachés ejecutables. `audit_fingerprint` cubre además esos
tres campos para detectar manipulación de la trazabilidad. Diagnósticos y las
propias huellas se excluyen de ambos cálculos.

Al cargar un artefacto se recalculan ambas huellas y una discrepancia se rechaza
como manipulación. Un roundtrip validación → JSON canónico → validación conserva
los bytes. Estas huellas identifican el plan, no una ejecución concreta: la
semilla global sigue en `Config`; `seed-derivation/1` fija cómo se interpreta.

## Baseline común

`tests/unit/semantic/llm/baseline.py` evalúa heurísticas y respuestas H0 sobre
las mismas 85 columnas:

| Fixture | Columnas |
|---|---:|
| inmobiliaria | 20 |
| cementerio | 13 |
| taller | 21 |
| ecommerce | 21 |
| rrhh_autoref_nullable | 5 |
| rrhh_autoref_notnull | 5 |

Cada modelo H0 tiene 255 observaciones (85 × 3 repeticiones). Una predicción
ausente o duplicada cuenta como fallo y no cambia el denominador. No se añaden
métricas: se conservan exactitud de rol y de generador.

| Fuente | Rol | Generador |
|---|---:|---:|
| heurísticas | 78/85 (91,8 %) | 78/85 (91,8 %) |
| llama3.1:8b | 221/255 (86,7 %) | 179/255 (70,2 %) |
| qwen2.5:3b-instruct | 212/255 (83,1 %) | 227/255 (89,0 %) |
| qwen2.5:7b-instruct | 226/255 (88,6 %) | 225/255 (88,2 %) |

`baseline_v1.json` graba el resultado. `labels_review_v1.yaml` fija las seis
fuentes por SHA-256 y deja instrucciones, revisor, fecha y decisión para el
segundo repaso humano. Su estado es `pending_human_second_review`: las labels no
se presentan como ground truth humano definitivo. El test recalcula los seis
SHA-256 desde cada `source`; no se limita a validar la longitud del texto.

## Consecuencias

- Las 90 respuestas H0 y `experiments/` permanecen intactos y se leen como
  evidencia.
- `src/synthdb/ir/schema.py`, el fusor, el motor, la CLI, proveedores, prompt y
  caché no cambian.
- `derived` no se puede sellar en `ResolvedPlanArtifact` v1 porque su regla y
  `refs` viven fuera del artefacto; la frontera lo rechaza explícitamente.
- H3-R2 debe implementar la política que transforma propuestas validadas en
  decisiones resueltas, incluida la disposición explícita de reglas y hints.
- Proveedores, prompt, caché, chunking, integración del fusor y `plan.lock`
  quedan para entregas posteriores.
