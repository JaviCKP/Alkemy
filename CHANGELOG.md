# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Este proyecto seguirá [SemVer](https://semver.org/lang/es/) a partir de la
primera release (mientras la versión sea 0.x, la API se considera inestable).

## [Unreleased]

### Added

- H2 Sesión E (T2.11+T2.12+T2.13) — motor determinista en memoria con
  compilación previa de generadores y reglas, ejecución por fases/lotes,
  `KeyStore` y selectores de FK, `RowContext` con padres reales, costura
  `complete_batch`, jerarquías por niveles, ciclos con actualizaciones diferidas,
  puentes sin pares repetidos y arrays de 0–5 elementos. Se añade
  `validation.structural.validate_batch` para tipos, NOT NULL, CHECK, UNIQUE/PK,
  FK y re-evaluación exacta de todas las reglas, con abortado o cuarentena dentro
  de `Dataset`. La configuración de una FK compuesta puede nombrar cualquiera de
  sus columnas; estrategias contradictorias en la misma relación producen un
  `ConfigError` que cita ambas claves.

- T2.9+T2.10 (#36) — **mini-DSL de reglas** (parser + intérprete de lista blanca) y
  **RowContext + orden de columnas intra-fila** (Hito 2, Sesión D, §4 del plan;
  especificacion.md §7.2). Las `rules` del YAML dejan de ser cadenas opacas (Sesión B)
  y pasan a interpretarse; el motor real que las consume por fila es la Sesión E:
  - **`rules/dsl.py` (T2.9).** `parse_rule(text) -> Rule`: reutiliza el parser de
    EXPRESIONES de sqlglot (postgres) y **traduce** su AST a uno propio y tipado
    (`Const`, `Col`, `ParentCol`, `Ref`, `Call`, `Compare`, `Arith`, `BoolOp`, `Not`,
    `Neg`). La traducción es una **lista blanca CERRADA por tipo de nodo**: cualquier
    nodo de sqlglot no contemplado ⇒ `RuleParseError` con el fragmento; jamás se
    guarda ni evalúa un nodo de sqlglot tal cual (CLAUDE.md prohíbe eval/exec, y esta
    sesión es donde esa regla se juega el proyecto). Gramática: literales (número,
    cadena, booleano, NULL) y columnas de la fila; `parent(<fk>).<columna>`;
    `ref('<nombre>')`; comparaciones (`= <> < <= > >=`), aritmética (`+ - * /`),
    `and/or/not`. Se rechazan explícitamente subíndices (`a[0]`), atributos
    encadenados (`a.b.c`), columnas cualificadas (`t.c`), `||`, `^`, `%`, `LIKE`,
    `IN`, `BETWEEN`, `IS NULL`, `CAST`, `CASE`, subconsultas, comentarios (`--`,
    `/* */`), `;`, y toda función fuera de la lista blanca (`upper`, `concat`,
    `system`, `getattr`, `__import__`…). Los **agregados de grupo** (`sum`, `count`…)
    se rechazan con mención expresa a la v1.0 (`sum_over_group`, post-MVP). Se cierra
    también el hueco de sqlglot que aparca argumentos sobrantes de `round`/`len`/
    `date_add` en slots con nombre (`truncate`/`binary`/`unit`): esos slots se
    rechazan en vez de ignorarse en silencio. `clasify_rule(rule) -> RuleKind`:
    `bound` (desigualdad con una columna local despejada frente a una expresión que no
    la referencia — cota del generador), `derivation` (`col = expr` — la calcula el
    generador `derived`), `assertion` (cualquier otra cosa evaluable). Toda regla se
    re-evalúa además como aserción (doble uso de §7.2). Helpers para el motor:
    `as_bound` (columna, lado inferior/superior, exclusividad, expresión de la cota),
    `as_derivation` (columna + expresión) y `rule_dependencies` (dependencia de orden
    implícita: destino + columnas locales leídas).
  - **`rules/eval.py` (T2.9).** `evaluate(rule, ctx)` y `check(rule, ctx) -> bool`
    con un intérprete que despacha por tipo de nodo con operaciones nativas
    escogidas a mano: sin `eval`/`exec`/`compile`/`getattr` dinámico sobre ningún
    nombre del usuario. La **lista blanca de funciones vive en UN solo dict**
    (`FUNCTIONS`): `date(y,m,d)`, `date_add(fecha, días)`, `years_between(a,b)`,
    `noise(sigma)` (multiplicativo `1 + N(0, sigma)`, con el **RNG de la fila** ⇒
    determinista, sin `random` global), `round(x, ndigits=0)`, `len(texto)`; el
    parser consulta ese mismo dict (nombres + aridad) para rechazar en compilación.
    Errores de evaluación (columna inexistente, `ref` desconocida, fila padre
    ausente, división entre cero, tipos incompatibles) ⇒ `RuleEvalError` con la regla
    y la fila. `check` exige que la regla evalúe a booleano.
  - **`generation/context.py` (T2.10).** `RowContext` **extiende** el `GenContext` de
    la Sesión A rellenando el hueco documentado: `row` (valores ya generados),
    `refs` y `parent(<fk>) -> dict | None`; los generadores existentes no cambian de
    firma (un `RowContext` ES un `GenContext`). `parent()` se alimenta de un
    `ParentResolver` inyectado (protocolo; `mapping_resolver` lo respalda con un
    `dict` en tests, y el motor lo poblará desde el KeyStore/selector de FK en la
    Sesión E). `build_column_order(table_plan, rules) -> list[str]`: grafo de
    dependencias IMPLÍCITAS de las reglas `bound`/`derivation` (la columna que
    acota/deriva se genera tras las columnas locales que lee) + topo-sort determinista
    (Kahn con min-heap, **desempate alfabético**); ciclo entre columnas ⇒ `PlanError`
    nombrando el ciclo.
  - **Generador `derived` (T2.10), `generation/generators/derived.py`.** Registrado
    en el catálogo como uno más; su `expression` (lado derecho de la derivación) se
    parsea una vez y se evalúa por fila sobre el `RowContext`. Exige un `RowContext`
    (lo estrecha con `isinstance`); un `GenContext` plano ⇒ `TypeError` accionable.
  - **Alcance no tocado** (task de la sesión): `ir/schema.py`, `parsing/`,
    `constraints/`, `graph/`, `keystore`/`fk`, `merge.py` y `config/` quedan intactos.
    Las `rules` siguen llegando crudas del YAML. **Aviso para la Sesión E**: las
    reglas de ejemplo de `tests/configs/inmobiliaria_ejemplo.yaml` son marcadores de
    la Sesión B (`superficie_from_parent(...)`, `sum_over_group(...) ~= ... +- 0.01`)
    que **no** conforman la gramática real del mini-DSL; habrá que reescribirlas en
    DSL válido (p. ej. `precio = parent(vivienda_id).superficie_m2 * ref('precio_m2_base') * noise(0.2)`)
    cuando el motor las consuma. La derivación con `noise()` no satisface la igualdad
    estricta al re-comprobarse como aserción (el ruido se vuelve a muestrear): es la
    Sesión E quien fija la política de re-validación de derivaciones aleatorias.
  - **Docs.** `docs/dsl.md` estrena la referencia de la gramática, la lista blanca de
    funciones, los tres tipos de regla con ejemplos de §7.2/§16, y los errores
    comunes. Tests nuevos en `tests/unit/rules/` y `tests/unit/generation/test_context.py`
    (parseo de cada construcción y anidamientos, batería de rechazo una por línea,
    clasificación, evaluación determinista, orden de columnas con cadena y ciclo, y el
    ejemplo trabajado de §16 sobre filas de juguete).

- T2.7+T2.8 (#34) — **KeyStore** y **selección de claves foráneas** (Hito 2, Sesión C,
  §4 del plan; especificacion.md §7.4). Construido solo contra la IR congelada y los
  modelos de estrategia FK de la Sesión B; la selección concreta del padre la
  consumirá el motor (Sesión E):
  - **`generation/keystore.py` (T2.7).** `KeyStore` append-only por tabla:
    `add(table, keys)`, `count(table)`, `get(table, index) -> tupla`. Las claves se
    guardan **siempre como tupla** (PK simple = tupla de 1), de modo que la PK
    compuesta no es un caso especial en ningún consumidor y `get` devuelve la tupla
    entera, nunca componentes sueltos (§7.4). Solo memoria (el *spill* a SQLite en
    disco es v1.0, dicho en el docstring). Smoke de rendimiento marcado
    `@pytest.mark.slow`: 10⁶ claves añadidas + 10⁵ accesos aleatorios muy por debajo
    del objetivo (~2 s), con cota holgada para no volverse inestable en CI.
  - **`generation/fk.py` (T2.8).** Protocolo `FkSelector.pick(rng) -> int | None`
    (contrato común, pensado para consumo por lotes: un selector por columna FK,
    llamado una vez por fila hija con el RNG de esa fila) y las estrategias de §7.4:
    `UniformSelector` (equiprobable), `ZipfSelector(s)` (pocos padres concentran
    hijos; el ranking de popularidad se asigna por **índice de inserción** —padre 0 =
    el más popular—, determinista y documentado, nunca por valor de clave),
    `UniqueSubsetSelector` (1:1 sin reemplazo; agotamiento ⇒
    `UniqueSubsetExhaustedError` con tabla, padres disponibles y filas pedidas) y
    `NullRatioSelector` (envoltura que decide NULL **antes** de seleccionar: consume
    el RNG del selector interior solo en las filas no nulas). `quota` va aparte por
    ser un asignador de **lote completo**: `build_quota_assignment(rng, n_parents,
    n_rows, min, max)` da a cada padre su `min`, reparte el excedente y baraja para no
    correlacionar padre con posición; respeta `[min, max]` exactamente e infeasible
    (por abajo o por arriba) ⇒ `QuotaInfeasibleError` con los cuatro números y el
    rango factible. Determinismo total: toda la aleatoriedad entra por el `rng`
    recibido, sin `random` global. Tests con estadística gruesa (semilla fija,
    tolerancias amplias) para zipf/uniform, cotas exactas para quota, y determinismo
    bit a bit en las cinco estrategias.
  - **Cableado del plan (`semantic/merge.py`, toque mínimo).** Las columnas FK dejan
    el generador provisional + aviso de la Sesión B y pasan a `ColumnPlan` con
    `generator.type == "fk"`, `params` = volcado de la estrategia del YAML
    (`tables.<t>.fk.<col>`, cuyos campos `s`/`min`/`max`/`null_ratio` coinciden con
    los selectores) o `uniform` por defecto; `source="user"` si viene del YAML, `"ir"`
    si es el defecto, `confidence=1.0`, sin avisos. La selección real sigue siendo del
    motor. Snapshot golden de `inmobiliaria.sql` regenerado (cambio acotado a las 4
    columnas FK: avisos provisionales fuera, generadores `fk` con su estrategia
    dentro; nada más se movió). `pyproject.toml`: nuevo marcador `slow`.
  - **Limitación señalada** (modelos de config intactos, Sesión B): `tables.<t>.fk`
    indexa la estrategia por nombre de columna, así que en una FK **compuesta** solo
    la columna nombrada recibiría la estrategia y las demás caerían al defecto. No
    afecta a los fixtures del MVP (FK de una sola columna); si se necesita, es una
    ampliación del modelo de config, no de esta sesión.

- T2.5+T2.4+T2.6 (#31) — configuración del usuario, heurísticas deterministas y el
  **fusor** (Hito 2, Sesión B, §4 del plan). Cierra la cadena de prioridad de
  especificacion.md §7.1 (usuario > IR > LLM > heurística > fallback) sobre la IR
  congelada y el catálogo de generadores de la Sesión A; el LLM queda como hueco
  reservado para el H3 (ADR-002):
  - **`config/models.py` + `config/loader.py` (T2.5).** Modelos Pydantic del YAML del
    MVP (§11): `version`, `seed`, `locale`, `dialect`, `llm`, `defaults`, `tables`
    (`rows`, `columns.{generator, params, null_ratio, unique}`, `fk`, `rules`),
    `refs`, `hierarchy`, `output`. `extra="forbid"` en todo ⇒ una clave desconocida
    es error con su **ruta exacta** (`tables.viviendas.columns.foo.generator`). Las
    estrategias de `fk` (uniform/zipf/unique_subset/quota, §7.4) se validan solo en
    su *forma* (el selector es de la Sesión C); las `rules` se guardan como cadenas
    **sin interpretar** (el mini-DSL es de la Sesión D); el bloque `llm` se parsea
    entero pero no tiene efecto hasta el H3 salvo `min_confidence`, que el fusor ya
    usa como umbral. Loader vía ruamel en modo seguro: un error de sintaxis YAML se
    reporta con **línea y columna**, y un `ValidationError` con la ruta de cada campo.
  - **`semantic/heuristics.py` (T2.4).** `infer_column(table, column) ->
    HeuristicResult | None`: 43 patrones es/en ordenados del más específico al más
    genérico (el orden es contrato y se testea: `codigo_postal` gana a `codigo`,
    `fecha_nacimiento` a `fecha`, `usuario` a `nombre`; `id`/`*_id` van al final).
    Cada patrón combina regex de nombre con condiciones de tipo y propone un
    `GeneratorSpec` del catálogo. Confianzas honestas por patrón (0.6–0.95): `email`
    0.95, `descripcion` 0.7 (el generador de relleno es pobre; el modo IA es del
    H3B), `id`/`*_id` 0.6 (por debajo del umbral por defecto ⇒ en el fusor caen al
    fallback, porque el valor real de una FK lo pone el selector de la Sesión C).
    Sin match ⇒ `None` (el fusor decide el fallback). `password`/`hash` producen
    SIEMPRE un marcador inerte (`template`), jamás un valor de Faker (privacidad).
    Métrica contra las labels del H0 (fixtures 1–5), réplica de `compute_metrics.py`:
    **91 % de acierto de rol y 91 % de generador**, muy por encima del umbral ≥ 60 %
    del criterio de T2.4; marcada `@pytest.mark.metric`.
  - **`semantic/merge.py` (T2.6).** `build_plan(spec, config) -> TablePlans`
    (`ir/plans.py` estrena `TablePlan`/`ColumnPlan` con `generator`, `source`,
    `confidence`, `role`, `warnings`; `"llm"` reservado en el `Literal` de `source`,
    documentado como H3). La cadena §7.1 en orden exacto: (1) **usuario**, que manda
    pero se valida contra la IR — un `choice` con valores fuera del enum/CHECK IN, o
    unas cotas de usuario fuera de las del CHECK, se rechazan con `PlanError` que
    nombra tabla.columna y las dos partes en conflicto; (2) **IR** — `enum_values`/
    `CHECK IN` ⇒ `choice`; `autoincrement`/`GENERATED` ⇒ la BD asigna el valor y la
    columna se excluye de los INSERT (`source="ir"`, generador `None`);
    `bounds_derived` interseca las cotas del generador de CUALQUIER fuente (la
    heurística de `anio` [1900, 2100] se recorta al CHECK [1900, 2026]); un UNIQUE/PK
    de una sola columna fuerza `unique=True` aunque el usuario diga lo contrario;
    (3) **heurística** si supera `min_confidence` (0.7 por defecto); (4) **fallback**
    seguro por tipo, siempre con aviso por columna. `null_ratio` sobre una columna
    `NOT NULL` ⇒ `PlanError` (y `defaults.null_ratio` no rompe columnas NOT NULL:
    solo aplica a las anulables). Las columnas que participan en una FK llevan un
    aviso provisional (el selector es de la Sesión C) y `excluded_values` de un
    `<>`/`NOT IN` que un rango no puede evitar también se avisa: nada en silencio.
    Determinista byte a byte (recorre tablas/columnas en el orden de la IR). Plan
    golden de `inmobiliaria.sql` + el YAML de ejemplo de §11
    (`tests/configs/inmobiliaria_ejemplo.yaml`) como snapshot revisado a ojo; sobre
    `opaco.sql`, cero inventos (ninguna columna `heuristic`: SERIAL ⇒ IR, el resto
    fallback). Tests nuevos en `tests/unit/config/` y `tests/unit/semantic/`
    (498 passed en total).
  - **Alcance no tocado** (declarado en los docstrings y con avisos en el plan):
    `ir/schema.py` (congelada), `parsing/`, `constraints/`, `graph/` y `generators/`
    quedan intactos; el fusor no wirea `config.locale` en los params de `faker` (el
    locale por defecto sigue en `FakerParams`), el selector de FK es la Sesión C
    (T2.8) y el intérprete de reglas la Sesión D (T2.9).

- T2.1+T2.2+T2.3 (#29) — cimientos deterministas del motor de generación (Hito 2,
  Sesión A), construidos solo contra la IR congelada (no dependen de `parsing/` ni
  de `graph/`):
  - **`generation/seeding.py` (T2.1).** `seed_for_table(seed_global, tabla)` deriva
    la semilla de tabla con BLAKE2b sobre un mensaje con *framing* de prefijo de
    longitud (ninguna descomposición ambigua de `(semilla, tabla)`) y dominios
    separados por `person=`. `rng_for_row(seed_tabla, indice)` construye un
    `random.Random` **por fila** sembrado desde `(seed_tabla, indice)`: el valor de
    la fila *i* depende solo de su índice, no de un flujo secuencial por tabla, así
    que es independiente del tamaño de lote y del orden de generación (test:
    100 filas en lotes de 10, 100 y 7 producen lo mismo). Sin `random` global ni
    `datetime.now()` (CLAUDE.md).
  - **`generation/generators/base.py` (T2.2).** `GenContext` (dataclass con `rng`,
    `column`, `table`; el hueco para `row`/`parent()` de la sesión D queda
    documentado, no implementado). Protocolo `Generator.generate(ctx) -> Any`.
    Registro por nombre (`register()` + `resolve(GeneratorSpec)`) con los parámetros
    de cada generador validados por un modelo Pydantic propio (`extra="forbid"` ⇒
    error de campo exacto si faltan o sobran). Envoltura de unicidad a nivel de
    `resolve` (`unique=True`): 50 reintentos contra un conjunto de vistos y, al
    agotarse, `UniqueExhaustedError` con tabla, columna y cardinalidad alcanzada vs
    pedida. Los *entry points* de plugins quedan para v1.0; `TypeSpec.is_array` se
    ignora esta sesión (la envoltura de arrays es del motor, sesión E).
  - **Catálogo básico (T2.3), `generation/generators/`.** `faker` (una instancia de
    Faker por locale —`es_ES` por defecto—, resembrada por fila desde el RNG, sin
    `faker.unique`), `numeric_range` (distribución en la forma anidada canónica
    `{family, params}` mediante un `DistributionSpec` reutilizable —familias
    uniform/normal/lognormal/zipf, parámetros validados por familia con error de
    campo exacto, solo `random` estándar—; respeta min/max con exclusividades,
    `round_to`, y para enteros los bits del `TypeSpec` como cota implícita),
    `sequence` (arranque+paso),
    `datetime_range` (date y timestamp, con zona si el tipo la declara; rango por
    defecto una década FIJA, sin `datetime.now()`), `choice` (pesos), `template`
    (`{tabla}_{columna}_{n}`), `uuid` (v4 derivado del RNG, determinista) y
    `fallback` seguro por kind del `TypeSpec` (respeta `varchar(n)`, usa
    `enum_values`). Tests en `tests/unit/generation/`: tipo devuelto, cotas y
    exclusividades, reproducibilidad (misma semilla ⇒ misma secuencia; distinta ⇒
    distinta), unicidad con agotamiento controlado, independencia del tamaño de lote
    y tests estadísticos gruesos de zipf/normal/lognormal con semilla fija.
- ADR-004 — lecciones del primer esquema real (inmobiliaria de 20 tablas,
  PostgreSQL 15; ver `docs/validaciones/`). Descongelación puntual de la IR
  (ADR-003) para tres construcciones que el esquema real necesita:
  - **`ON DELETE SET NULL (columna)`** de PostgreSQL 15+. sqlglot 30.12.0 (la
    última) la rechaza con `Expecting )`; en vez de preprocesar el texto SQL
    (prohibido por CLAUDE.md) se extiende el dialecto por el mecanismo oficial:
    `parsing/dialect.py` subclasea `PostgresParser` y consume la lista de
    columnas desde el AST. Nuevo `RelationshipSpec.on_delete_set_columns`
    (estructural, con plegado de identificadores; subconjunto de `columns` o
    aviso).
  - **Nulabilidad dirigida de FK compuestas.** Nuevo
    `RelationshipSpec.nullable_columns` (derivado, EXCLUIDO del hash) con el
    subconjunto anulable de la FK, y `RelationshipSpec.match_full` (estructural)
    para `MATCH FULL`. `graph/strategies.py` rompe un ciclo bajo `MATCH SIMPLE`
    anulando SOLO las columnas anulables (p. ej. `entidad_id` de
    `(inmobiliaria_id, entidad_id)`), y bajo `MATCH FULL` exige que sean todas.
    `InsertPhase.null_fks`/`UpdatePhase` registran qué columnas se anulan
    (`FkRef.null_columns`), no solo qué FK. El docstring de `nullable` se amplía
    para distinguir el AND global de la nulabilidad por columna.
  - **Arrays** (`text[]`, `numeric(7,2)[]`). Nuevo `TypeSpec.is_array`
    (estructural); el `kind`/parámetros son los del elemento. Detección desde el
    AST de sqlglot (`DataType` ARRAY), nunca del texto; `text[][]` se colapsa a
    una dimensión con aviso. La generación de arrays queda para el Hito 2.
  - `synthdb analyze` refleja las tres: sufijo `[]` en el tipo, `MATCH FULL` y
    `ON DELETE set_null (columnas)` en la FK, y las columnas concretas que se
    anulan en las fases.
  - Nuevo fixture `tests/schemas/crm_real_minimo.sql` (fixture 11): reproduce en
    3 tablas el ciclo de FK compuestas con nulabilidad dirigida, la acción
    `ON DELETE SET NULL (columna)` y una columna `text[]`, sin incluir el
    esquema real del usuario. Tests nuevos en parser, grafo, hash, tipos y CLI.
- ADR-004 (cambio, no adición) — añadir los tres campos estructurales
  (`is_array`, `on_delete_set_columns`, `match_full`) cambia la forma canónica de
  la IR: **todos los hashes de esquema cambian** y **todos los snapshots golden
  de IR se regeneraron en este PR** (regeneración en bloque justificada por el
  ADR, revisada a ojo; no hay cachés en producción que invalidar). El campo
  derivado `nullable_columns` cambia los snapshots pero no el hash.
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

- Tercera revisión de PR #40: `numeric_range` mantiene en `Decimal` las cotas y
  la rejilla de `NUMERIC(p, s)` hasta la cuantización final, evitando el
  subdesbordamiento de `scale_step` en escalas grandes y preservando el contexto
  decimal global. Las referencias FK cualificadas por schema se resuelven al
  `TableSpec` canónico antes de detectar autorreferencias, validar lotes y cerrar
  la integridad referencial. Se añaden regresiones unitarias para ambos casos y
  una prueba PostgreSQL real marcada `integration` para redondeo y overflow.

- Segunda revisión de la sesión E (PR #40): cuatro hallazgos nuevos sobre la
  revisión anterior, en `generation/numeric_bounds.py`,
  `generation/generators/numeric.py`, `generation/engine.py` y
  `validation/structural.py`.
  - **`NUMERIC(p, s)` con la semántica exacta de PostgreSQL.** `quantize_to_scale`
    (y el `_quantize` del generador `numeric_range`) redondeaban medio-a-par
    (`ROUND_HALF_EVEN`); PostgreSQL aleja del cero los empates
    (`ROUND_HALF_UP`): `0.385` a escala 2 pasa a ser `0.39` (no `0.38`,
    simétrico en negativo), y `9.995` en `NUMERIC(3,2)` desborda tras
    redondear (`10.00 > 9.99`). Además, `representable_limit`/
    `quantize_to_scale`/`fits` dependían en silencio de la precisión ambiente
    de `Decimal` (28 dígitos por defecto): `NUMERIC(50, 0)` truncaba su límite
    sin avisar y `NUMERIC(100, 0)` reventaba con `InvalidOperation` al
    cuantizar. Cada función construye ahora su propio `decimal.Context` LOCAL
    (nunca `decimal.getcontext()`), dimensionado por los dígitos ENTEROS
    reales del valor (`.adjusted()`, no `len(digits)`: un valor con exponente
    grande y coeficiente corto como `2E+999` necesita igualmente 1000 dígitos)
    más la escala destino; exacto hasta `NUMERIC(1000, 500)` sin tocar el
    contexto global.
  - **Rangos exclusivos y la rejilla de la escala en compilación.** La
    comprobación de compilación (`_check_numeric_representable`) solo
    intersecaba intervalos *reales*: `min=max=9.99, min_exclusive=true` en
    `NUMERIC(3,2)` "solapaba" con la ventana representable y se aceptaba en
    silencio aunque ningún valor lo cumple; `CHECK (x > 9.99)` en el mismo
    tipo llegaba a generación y reventaba con `ValueError` en vez de
    rechazarse en compilación. Nueva `numeric_bounds.has_quantized_value
    (precision, scale, low, high, min_exclusive, max_exclusive)`: comprueba
    que existe al menos un múltiplo de la escala dentro del rango, respetando
    sus exclusividades y las del límite del tipo (que nunca excluye su propio
    extremo salvo que el usuario lo pida). Ningún rango imposible llega ya al
    bucle de generación ni acaba como cuarentena completa.
  - **Las FK que referencian una UNIQUE distinta de la PK validan
    correctamente.** `validate_batch`/`_foreign_key_errors` y la postcondición
    `_enforce_referential_integrity` comparaban la clave del hijo contra
    `parent.primary_key`, asumiendo que toda FK referencia la PK del padre.
    `RelationshipSpec.ref_columns` puede apuntar a cualquier UNIQUE (o a la PK
    compuesta en otro orden): con esa asunción, una FK así rechazaba en falso
    casi todas sus filas, y el cierre nunca detectaba una referencia
    realmente colgante hacia esa UNIQUE. Ambas capas comparan ahora contra los
    valores reales de `fk.ref_columns`, en su orden exacto; `KeyStore` sigue
    indexando por PK para la SELECCIÓN de FK (sin cambios), pero la
    validación usa índices propios por `(tabla, columnas referenciadas)`
    (`Dataset._ref_value_sets`, con caché perezosa; `_accepted_ref_value_sets`
    en el cierre). `validate_batch` deja de necesitar `KeyStore`.
  - **`Dataset.updates` sigue siendo válido tras la cuarentena del cierre
    referencial.** `_fill_missing_foreign_keys` registra `row_index` como la
    posición en `dataset.tables[tabla]` EN ESE MOMENTO; si
    `_enforce_referential_integrity` cuarentena filas después, esas
    posiciones quedaban desplazadas o pasaban a apuntar a otra fila. Nuevo
    `Dataset._update_origins` (número de fila original, estable, paralelo a
    `updates`) y `_resync_updates`: al terminar el cierre, descarta las
    actualizaciones de filas cuarentenadas y recalcula el `row_index` de las
    supervivientes contra su posición final.
  - Nuevo fixture `tests/schemas/fk_unique_target.sql` (fixture 12): FK simple
    y compuesta hacia una UNIQUE, `ref_columns` en orden distinto al de la PK,
    y un ciclo diferible donde una dirección referencia una UNIQUE, para
    probar la cuarentena de un padre referenciado por UNIQUE con cierre
    transitivo hasta una tercera tabla fuera del ciclo. Tests nuevos en
    `test_numeric_precision.py` (redondeo, precisión grande, rejilla y
    exclusividad) y `test_engine.py` (las reproducciones mínimas del hallazgo
    3 y la regresión de coherencia de `updates` sobre `ciclos_deferrable.sql`,
    hallazgo 4).

- Revisión de la sesión E (T2.11-T2.13): los dos hallazgos publicados en el PR.
  - **`NUMERIC(precision, scale)` se respeta de punta a punta.** La IR conservaba
    `precision`/`scale` pero ni la generación ni `validation.structural` los
    aplicaban: un `numeric_range` podía producir —y el validador aceptar— valores
    fuera del rango representable (p. ej. `100.0` en `NUMERIC(3,2)`, cuyo máximo es
    `9.99`). Nuevo módulo puro `generation/numeric_bounds.py` con la semántica exacta
    (rango representable, cuantización a la escala y test de encaje) en aritmética de
    `Decimal`/entero, **nunca floats para contar decimales** (evita el ruido binario).
    Lo comparten el generador `numeric_range` (recorta el rango al representable y
    redondea a la escala; `_quantize` pasa a ser exacto con `Decimal`), el `fallback`
    numérico (no desborda tipos estrechos), `validation.structural` (rechaza un valor
    que desborda la precisión, nombrando el tipo) y la compilación
    (`_check_numeric_representable`): un rango cuya intersección con el representable
    es vacía ⇒ `PlanError` accionable con tabla, columna, rango y tipo, en vez de un
    `Dataset` inválido en silencio.
  - **La cuarentena ya no puede romper la integridad referencial final.** En
    `DeferredPhase` y en `InsertLeveledPhase` una fila padre podía acabar en
    cuarentena mientras el `KeyStore`, `_key_sets` o el propio lote aún conservaban su
    clave, dejando hijos aceptados que apuntaban a una fila inexistente. Nueva
    postcondición de `generate_dataset` (`_enforce_referential_integrity`): recorre el
    dataset hasta un punto fijo apartando toda fila aceptada con una FK no nula
    colgante; al cuarentenar un padre, sus dependientes caen también,
    **transitivamente** (incluido el ciclo diferible en ambos sentidos). No inventa ni
    reasigna valores (la reparación es H4): solo aísla. `Dataset.levels` pasa a
    alinearse 1:1 con las filas aceptadas por número de fila (antes recortaba el
    prefijo del lote y se desalineaba al cuarentenar una fila intermedia); al terminar,
    el `KeyStore` (nuevo `KeyStore.replace`) y `_key_sets` reflejan únicamente filas
    aceptadas. `complete_batch` sigue siendo la costura pública de lote y
    `on_error=abort` no cambia: aborta en la primera fila estructuralmente inválida,
    antes del cierre.
  - Tests nuevos: `tests/unit/generation/test_numeric_precision.py` (módulo puro,
    generador, `numeric[]`, `PlanError` de rango imposible y cuarentena por
    desbordamiento) y regresiones de integridad referencial en `test_engine.py`
    (cascada transitiva diferida y por niveles, cero FK colgante, niveles alineados,
    `abort` sin cambios, y los casos sin corrupción con cero cuarentena y mismo hash).

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
