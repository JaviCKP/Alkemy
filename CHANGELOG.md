# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Este proyecto seguirá [SemVer](https://semver.org/lang/es/) a partir de la
primera release (mientras la versión sea 0.x, la API se considera inestable).

## [Unreleased]

### Added

- T1.8 — `cli.py`: nuevo subcomando `synthdb analyze RUTA.sql [--dialect]
  [--json]` (Typer + Rich) que ejecuta el pipeline estructural completo
  (`parse_ddl` → `interpret_checks` → `analyze_structure` → `resolve_cycles`)
  y lo presenta sin generar datos: por cada tabla, sus columnas (tipo canónico,
  nulabilidad, default), PK, FKs (con `cardinality_hint` y `deferrable`),
  uniques, checks con sus cotas si `interpret_checks` los reconoció (y marcando
  los que no), `kind` y comentario; luego las fases de generación en orden con
  su tipo (Insert/InsertLeveled/Update/Deferred) y sus tablas; y al final todos
  los avisos acumulados (parser + checks + grafo) agrupados por origen. `--json`
  vuelca `{schema, phases, warnings}` con la serialización propia de cada modelo
  (`model_dump`, nunca dicts a mano) y claves ordenadas, determinista byte a
  byte. Códigos de salida sin traceback (CLAUDE.md): `0` correcto (con o sin
  avisos), `1` `ParseError` (mensaje del parser tal cual), `2` `UnbreakableCycle`
  (su diagnóstico accionable), `3` archivo inexistente o ilegible. Un callback
  de la app mantiene `analyze` como subcomando con nombre propio en vez de
  colapsarse en la raíz. En Windows fuerza UTF-8 en stdout/stderr para que Rich
  no aborte con `UnicodeEncodeError` al emitir `→`/`⇒`/bordes de tabla sobre una
  consola cp1252. El entry point `synthdb = "synthdb.cli:app"` ya estaba
  declarado en `pyproject.toml`. Tests con `typer.testing.CliRunner`
  (`tests/unit/cli/test_analyze.py`): los 10 fixtures con su código de salida,
  avisos surgidos y agrupados, diagnóstico del ciclo irrompible, error de
  sintaxis y ruta inexistente sin traceback, y `--json` parseable, con `kind`
  por fase y determinista.
- T1.9 — documentación de cierre del Hito 1. `docs/limitations.md` (estrena el
  archivo), redactado desde el comportamiento real del código: DDL soportado y
  construcciones que solo generan aviso, subconjunto de `CHECK` interpretado con
  el porqué del recorte de `LIKE` (pospuesto entero) y `OR` (unión de rangos, no
  una cota simple), y las tres salidas de ciclo más el caso irrompible.
  `docs/adr/003-ir-congelada.md` congela `ir/schema.py` (todo cambio posterior
  exige un ADR con campo, motivo e impacto en hash/snapshots) y deja explícito
  que `ir/plans.py` NO se congela hasta que el motor del H2 lo consuma, con el
  inventario de campos derivados excluidos del hash como punto de partida.
  README: sección "Estado actual" con el ejemplo de `analyze` sobre
  `inmobiliaria.sql` y enlace a `limitations.md`.
- T1.6 — `graph/dependency.py`: `analyze_structure()` construye el grafo de
  dependencias (nodo por tabla, arista hijo→padre por cada FK cuya
  `ref_table` sea otra tabla del esquema; las autorreferencias no crean
  arista, se anotan en `StructuralPlan.self_refs`), calcula sus componentes
  fuertemente conexos vía `networkx` (Tarjan) y los organiza en fases con
  `phase_layers()`: cada componente ocupa la fase más temprana que sus
  dependencias permiten, fusionando en la misma fase las tablas
  independientes entre sí (`tables_by_phase`, ordenado alfabéticamente
  dentro de cada fase para determinismo total, CLAUDE.md). Una FK cuya
  `ref_table` no existe en el esquema no añade arista: se registra como
  aviso en `StructuralPlan.warnings` en vez de fallar. De paso rellena,
  mutando `spec` in situ, los dos campos derivados que le corresponden
  (`TableSpec.kind` y `RelationshipSpec.cardinality_hint`, ya excluidos del
  hash canónico desde T1.5): `kind="bridge"` si la PK o un UNIQUE está
  formada íntegramente por columnas de 2+ FK distintas y quedan ≤ 2 columnas
  propias; `kind="lookup"` si la tabla no tiene FK salientes, tiene ≤ 3
  columnas y alguien la referencia; `cardinality_hint="one_to_one"` si un
  UNIQUE (o la PK) cae exactamente sobre las columnas de la FK,
  `"self_reference"` si la FK apunta a la propia tabla, `"many_to_one"` en
  el resto. `ir/plans.py` estrena el archivo con `StructuralPlan` y `FkRef`
  (identifica una FK sin ambigüedad por tabla propietaria + columnas +
  tabla referenciada).
- T1.7 — `graph/strategies.py`: `resolve_cycles()` expande cada fase de un
  `StructuralPlan` en la secuencia final de `Phase` (`ir/plans.py`:
  `InsertPhase`, `InsertLeveledPhase`, `UpdatePhase`, `DeferredPhase`).
  Autorreferencia (especificacion.md §6.3): FK anulable →
  `InsertLeveledPhase` (niveles); FK `NOT NULL` diferible → `DeferredPhase`;
  `NOT NULL` no diferible → `InsertLeveledPhase(roots_point_to_self=True)`
  con aviso en `StructuralPlan.warnings` (única salida sin tocar el DDL: las
  raíces se referencian a sí mismas). Ciclo real de 2+ tablas
  (especificacion.md §6.2): la primera FK anulable del ciclo (desempate
  alfabético por `(tabla, primera columna)`) se inserta a `NULL`
  (`InsertPhase.null_fks`, con el resto del ciclo ya en orden de dependencia
  vía `phase_layers()` sobre el subgrafo sin esa arista) seguida de una
  `UpdatePhase` que asigna el valor real; si ninguna es anulable pero alguna
  es diferible, `DeferredPhase`; si tampoco, `UnbreakableCycle` con
  diagnóstico accionable (tablas y FK implicadas, las tres salidas
  posibles: anulable, diferible o `--allow-ddl`, desaconsejado). Dentro de
  cada fase estructural, las tablas que no son ni ciclo ni autorreferencia
  se fusionan en un único `InsertPhase`; todas las unidades de una fase se
  emiten ordenadas alfabéticamente por su tabla más temprana. `pyproject.toml`:
  override de mypy (`ignore_missing_imports`) para `networkx`, que no
  distribuye stubs de tipos.

- Preparación del repositorio (Semana 0 del plan de ejecución del MVP):
  licencia Apache-2.0 (ADR-001), `pyproject.toml` con dependencias núcleo y
  extras `[db]`/`[dev]`, tooling de `ruff`/`mypy`/`pre-commit`, workflow de
  CI, árbol de módulos de `src/synthdb/` y los 10 esquemas fixture en
  `tests/schemas/` (7 dominios, con variantes en `rrhh_autoref` y `ciclos`).
- Milestones, labels y plantillas de issue en GitHub para los 5 hitos y la
  release 0.1.0; issues del Hito 0 creadas.
- Hito 0 — experimento de validación LLM completo (`experiments/00_llm_plan/`):
  extractor de IR, contrato v0, prompt v0, runner (90 llamadas: 3 modelos ×
  10 fixtures × 3 repeticiones), etiquetado y métricas. **Decisión: Go**
  (ADR-002) — `qwen2.5:7b-instruct` como modelo por defecto. Hallazgo
  relevante para T3.7: la confianza autodeclarada por el modelo no está
  calibrada en columnas sin contexto (0% de calibración correcta sobre
  `opaco.sql`), hace falta una señal estructural adicional en el fusor.
- T1.1 — `ir/schema.py`: modelos Pydantic v2 completos de la IR canónica
  (`SchemaSpec`, `TableSpec`, `ColumnSpec`, `RelationshipSpec`, `CheckSpec`,
  `TypeSpec`, `DefaultSpec`, `GeneratorSpec`, especificacion.md §5), todos
  con `extra="forbid"`. `canonical_json()` serializa con claves ordenadas
  recursivamente para bytes estables entre ejecuciones (base de la que
  partirá el hash canónico, T1.5). `TableSpec.schema` usa el alias `schema`
  sobre el campo `schema_` para no ensombrecer el `schema` heredado de
  `pydantic.BaseModel`.
- T1.2 — `parsing/types.py`: catálogo canónico de tipos y tabla de mapeo
  desde PostgreSQL (`serial*`→`integer` con `autoincrement`, familia entera,
  `numeric(p,s)`, `varchar(n)`/`char(n)`, `timestamp`/`timestamptz`, `date`,
  `boolean`, `uuid`, `text`, `json`/`jsonb`, `bytea`, tipos `enum` vía
  `is_enum`). Independiente de `sqlglot`: el parser DDL (T1.3) le pasa el
  nombre y los parámetros ya extraídos. Un tipo no reconocido nunca lanza
  excepción: degrada a `text` con un aviso registrado.
- T1.5 — `ir/hashing.py`: `schema_hash()` calcula el SHA-256 de la forma
  canónica de la IR, nunca del texto SQL. Ordena las `tables` por `name`
  (mismas tablas en distinto orden ⇒ mismo hash); preserva el orden de las
  `columns` dentro de cada tabla (identidad del esquema, afecta a los
  `INSERT` posicionales ⇒ reordenarlas cambia el hash). Excluye del hash los
  campos derivados que no proceden del DDL (`SchemaSpec.hash`,
  `SchemaSpec.warnings`, `TableSpec.kind`,
  `RelationshipSpec.cardinality_hint`, `CheckSpec.ast_supported`,
  `CheckSpec.bounds_derived`) mediante un mapa de exclusión centralizado
  (formato `include`/`exclude` avanzado de Pydantic v2), no casos sueltos.
- T1.3 (entrega 1 de 3) — `parsing/ddl.py`: `parse_ddl()` convierte
  sentencias `CREATE TABLE` de PostgreSQL (AST de sqlglot, dialecto
  explícito) en `TableSpec`: nombre de tabla y schema/namespace, columnas en
  su orden original, tipos vía `map_postgres_type` (sus avisos se propagan a
  `SchemaSpec.warnings`), `NOT NULL` y `PRIMARY KEY` tanto inline como a
  nivel de tabla (simple y compuesta). Una columna en la PK queda siempre
  `nullable=False`, aparezca o no un `NOT NULL` explícito en el DDL (PK lo
  implica en PostgreSQL). FK, `UNIQUE`, `CHECK`, `DEFAULT`, enums y
  comentarios quedan para las entregas 2 y 3: toda construcción que el
  parser reconoce pero todavía no maneja (incluidas sentencias que no son
  `CREATE TABLE`, p. ej. `ALTER TABLE`/`CREATE INDEX`) se registra como
  aviso con tabla y columna, nunca en silencio. Un error de sintaxis se
  traduce a un `ParseError` propio con línea, columna y sentencia
  aproximada — nunca se propaga el `sqlglot.errors.ParseError` original.
  Primer snapshot golden de la IR (syrupy,
  `tests/unit/parsing/__snapshots__/test_ddl.ambr`) parseando
  `tests/schemas/inmobiliaria.sql` completo.
- T1.3 (entrega 2 de 3) — `parsing/ddl.py`: añade `FOREIGN KEY` (inline vía
  `REFERENCES` y de tabla, simples y compuestas), `UNIQUE` (inline y de
  tabla), `CHECK` (de columna y de tabla) y `DEFAULT`. `RelationshipSpec.
  nullable` se deriva de las columnas locales de la FK ya finalizadas (tras
  aplicar el forzado de NOT NULL de una `PRIMARY KEY` de tabla, que puede
  declararse después de la columna en el DDL): `True` solo si TODAS son
  anulables. `ref_table` incluye el namespace cuando el DDL lo declara
  explícitamente. `REFERENCES tabla` sin columnas (apunta implícitamente a
  la PK del padre) deja `ref_columns=[]` con un aviso — esa resolución es
  del grafo de dependencias (T1.6), no de este parser, que no asume que la
  tabla referenciada ya se ha parseado. `ON DELETE`/`ON UPDATE` mapean a
  `ReferentialAction`; `DEFERRABLE` (con o sin `INITIALLY DEFERRED`) marca
  `deferrable=True`. Una `UNIQUE` cuyas columnas coinciden exactamente con
  la PK no se duplica en `TableSpec.uniques`. `CheckSpec.ast_supported`
  queda siempre en `False` y `bounds_derived` en `None` en esta entrega
  (interpretar el predicado es T1.4); `columns_involved` sale de recorrer
  el AST del predicado, no de parsear el texto. `DefaultSpec` distingue
  literal (número, cadena, booleano, `NULL`, incluidos negativos) —
  tipado en `value` — de expresión (`CURRENT_DATE`, `now()`,
  `nextval(...)`) — solo `sql_text`. Snapshot golden de
  `inmobiliaria.sql` actualizado: los avisos de FK/UNIQUE/CHECK de la
  entrega 1 desaparecen (ese fixture no usa nada de la entrega 3), y las
  relaciones/uniques/checks quedan reflejados en la IR.
- `ir/schema.py`: `RelationshipSpec.cardinality_hint` pasa a
  `CardinalityHint | None` (antes obligatorio) — el parser DDL no lo
  rellena a propósito, lo infiere `graph/dependency.py` (T1.6); ya estaba
  excluido del hash canónico. Corregida además la descripción de
  `RelationshipSpec.nullable`, que decía "alguna columna admite NULL"
  cuando la semántica real (y la que implementa el parser) es "TODAS las
  columnas locales admiten NULL".
- T1.3 (entrega 3 de 3, cierre del hito) — `parsing/ddl.py`: añade
  `CREATE TYPE ... AS ENUM`, `COMMENT ON TABLE`/`COMMENT ON COLUMN` y
  `ALTER TABLE ... ADD CONSTRAINT`. El parseo pasa a recorrer las sentencias
  en dos pasadas para que su orden en el archivo no importe: la primera
  resuelve todos los `CREATE TYPE` y `CREATE TABLE` (una tabla puede usar un
  enum declarado más abajo en el archivo); la segunda aplica `COMMENT ON` y
  `ALTER TABLE` sobre las tablas ya construidas. Un `CREATE TABLE` ya no se
  cierra a `TableSpec` de inmediato: queda como `_PendingTable` mutable
  hasta el final de la segunda pasada, porque un `ALTER TABLE` posterior
  puede seguir añadiendo su PK o sus FK — el forzado de `nullable=False` en
  las columnas de la PK y la resolución de `RelationshipSpec` (que depende
  de ese nullable final) se difieren en bloque a `_finalize_table`.
  `ALTER TABLE ... ADD CONSTRAINT` reutiliza íntegra la lógica de
  restricciones de tabla de la entrega 2 (`_apply_table_constraint`), tanto
  para FK/UNIQUE/CHECK como para una `PRIMARY KEY` que llega después del
  `CREATE TABLE` original. Toda variante no soportada (`CREATE TYPE` que no
  sea `AS ENUM`, `COMMENT ON` de un objeto que no sea tabla/columna,
  `ALTER TABLE ADD COLUMN`/`DROP...`, un `ALTER`/`COMMENT` sobre una tabla o
  columna no declarada en el propio DDL) se registra como aviso, nunca en
  silencio. El nombre de un tipo `enum` se pliega como cualquier
  identificador (de paso, corrige el mismo plegado que le faltaba al tipo
  definido por el usuario en `_type_components`, sin efecto observable hasta
  ahora porque nada lo ejercitaba); sus valores son literales y no se
  pliegan. Snapshots golden de los 9 archivos de fixture restantes
  (`cementerio`, `taller`, `ecommerce`, `rrhh_autoref` ×2, `ciclos` ×3,
  `opaco`), con 0 avisos en los 7: cierra el criterio de aceptación de T1.3
  (plan-ejecucion-mvp.md §3, snapshot de IR correcto para los 7 fixtures).
  Las FK que `ciclos_nullable.sql`/`ciclos_deferrable.sql`/
  `ciclos_unbreakable.sql` declaran vía `ALTER TABLE ... ADD CONSTRAINT`
  aparecen ahora como `RelationshipSpec` en vez de como aviso.
- T1.4 — `constraints/check_interp.py`: `interpret_checks()` re-parsea
  `sql_text` de cada `CheckSpec` (de columna y de tabla) con el parser de
  expresiones de sqlglot — nunca con regex — y, para el subconjunto que
  reconoce, rellena `ast_supported=True` y `bounds_derived`. Soporta, siempre
  restringido a un `CheckSpec` de una sola columna (`columns_involved`):
  comparaciones `col <op> literal` y su forma invertida `literal <op> col`
  ya normalizada (`>`, `>=`, `<`, `<=`, `=`, `<>`/`!=`), `BETWEEN`, `IN`,
  `NOT IN`, `NOT` de una comparación o de un `IN`, y `AND` de cualquier
  combinación de lo anterior intersecando las cotas. Un `AND` cuya
  intersección resulta vacía (p. ej. `x > 5 AND x < 3`) se queda con
  `ast_supported=True` — PostgreSQL acepta la restricción igual — pero
  añade un aviso a `SchemaSpec.warnings`: ninguna fila podrá cumplirla
  nunca. Fuera de este subconjunto, sin aviso nuevo (estado normal): `OR`,
  predicados multi-columna (quedan para el mini-DSL de T2.9), funciones,
  casts, subconsultas y `LIKE` (pospuesto entero; ver docstring del módulo
  para la justificación). `ir/schema.py`: documentado el formato exacto de
  `CheckSpec.bounds_derived` (claves `min`/`min_exclusive`, `max`/
  `max_exclusive`, `equals`, `values`, `excluded_values`, solo las que
  aplican). Test end-to-end de que `ir/hashing.py` (T1.5) no cambia al
  interpretar los checks de un esquema, confirmando la exclusión ya
  existente de `ast_supported`/`bounds_derived` del hash canónico.

### Fixed

- Revisión de T1.1 (#8) / T1.2 (#9) tras el merge de #19 (#20). Dos hallazgos
  que afectaban a la validez de los INSERT:
  - Familia de coma flotante binaria (`real`, `float4`, `double precision`,
    `float8`, `float`) sin mapeo en `parsing/types.py`: degradaba a `text` con
    aviso, lo que produciría INSERT inválidos en columnas float. Ahora mapea al
    kind canónico `numeric` sin `precision`/`scale` (el argumento de `float(p)`
    selecciona el tamaño de almacenamiento, real vs. double, no una precisión
    decimal, así que no se propaga).
  - `TypeSpec` sin ancho de entero: nuevo campo `bits: Literal[16, 32, 64] |
    None`, poblado en el mapeo (`smallint`/`int2`/`smallserial`/`serial2` → 16;
    `integer`/`int`/`int4`/`serial`/`serial4` → 32;
    `bigint`/`int8`/`bigserial`/`serial8` → 64). Cuando una columna entera no
    lleva CHECK, el ancho del tipo es la cota implícita del generador de enteros
    (H2), igual que `bounds_derived`; hasta ahora un `smallint` sin CHECK podía
    desbordar. Resuelto antes de T1.5 (hash canónico) porque `bits` cambia la
    salida de `canonical_json()`.
