# Limitaciones conocidas

Este documento describe, **desde el comportamiento real del código** (no desde
lo que la especificación aspira a cubrir), qué entiende hoy SynthDB y qué
queda fuera. Los avisos que emite el pipeline (`SchemaSpec.warnings`,
`StructuralPlan.warnings`, visibles al final de `synthdb analyze`) remiten
aquí: si te topas con un caso no soportado, la respuesta correcta es un aviso
y una entrada en este archivo, nunca un soporte a medias silencioso
(CLAUDE.md).

Alcance del MVP: SynthDB solo promete resultados correctos para **PostgreSQL**.
La CLI acepta `--dialect`, pero cualquier dialecto distinto de `postgres` no
está validado y puede producir una IR incorrecta sin avisar.

Estado a cierre del **Hito 1** (núcleo estructural: parseo, tipos, hash,
interpretación de `CHECK`, grafo de dependencias y estrategias de ciclo). La
generación e inserción de datos llega en hitos posteriores.

---

## DDL soportado

### Sentencias reconocidas y procesadas

- `CREATE TABLE` con columnas, tipos y restricciones (inline y de tabla).
- `CREATE TYPE ... AS ENUM`.
- `COMMENT ON TABLE` y `COMMENT ON COLUMN`.
- `ALTER TABLE ... ADD CONSTRAINT` (`FOREIGN KEY`, `UNIQUE`, `CHECK`,
  `PRIMARY KEY`).

El orden de las sentencias en el archivo no importa: el parser hace dos
pasadas, de modo que un `ALTER TABLE` o un `COMMENT ON` pueden preceder al
`CREATE TABLE` que modifican, y una columna puede usar un enum declarado más
abajo.

### Restricciones soportadas

- **`PRIMARY KEY`**: inline, de tabla, compuesta y añadida por `ALTER TABLE`.
  Sus columnas quedan siempre `NOT NULL`, aparezca o no el `NOT NULL` explícito
  (PostgreSQL lo implica).
- **`NOT NULL`**.
- **`FOREIGN KEY`**: inline (`REFERENCES`) y de tabla, simples y compuestas;
  `ON DELETE` / `ON UPDATE` (`CASCADE`, `RESTRICT`, `SET NULL`, `SET DEFAULT`,
  `NO ACTION`); la variante de PostgreSQL 15+ que acota `SET NULL`/`SET DEFAULT`
  a columnas concretas, `ON DELETE SET NULL (columna, …)`, se conserva en
  `RelationshipSpec.on_delete_set_columns` (ADR-004; el dialecto base de sqlglot
  no la parsea, la añade `parsing/dialect.py`); `MATCH FULL` (frente al
  `MATCH SIMPLE` por defecto); `DEFERRABLE [INITIALLY DEFERRED]`.
- **`UNIQUE`**: inline y de tabla. Una `UNIQUE` cuyas columnas coinciden
  exactamente con la `PRIMARY KEY` se descarta por redundante.
- **`CHECK`**: de columna y de tabla. Su interpretación a cotas de generación
  es un subconjunto acotado; ver [CHECKs](#checks).
- **`DEFAULT`**: literal (número, cadena, booleano, `NULL`, negativos) con su
  valor Python tipado, o expresión (`CURRENT_DATE`, `now()`, `nextval(...)`)
  conservando solo el texto.

### Tipos soportados

- **Enteros**: `smallint`/`int2`, `integer`/`int`/`int4`, `bigint`/`int8` y sus
  variantes `serial` (`serial`/`serial4`, `bigserial`/`serial8`,
  `smallserial`/`serial2`, marcadas como autoincrement). Todos comparten el
  tipo canónico `integer`; el ancho (16/32/64 bits) se conserva aparte y, sin
  `CHECK`, es la cota implícita del generador.
- **Numéricos**: `numeric`/`decimal(p, s)`.
- **Coma flotante binaria**: `real`/`float4`, `double precision`/`float8`,
  `float`. Mapean a `numeric` sin precisión/escala (el argumento de `float(p)`
  es tamaño de almacenamiento, no precisión decimal).
- **Texto**: `text`, `varchar`/`character varying(n)`, `char`/`character`/
  `bpchar(n)`.
- **Fecha y hora**: `date`, `timestamp`/`timestamp without time zone`,
  `timestamptz`/`timestamp with time zone` (conserva la zona horaria).
- **Otros**: `boolean`/`bool`, `uuid`, `json`/`jsonb`, `bytea`.
- **Enums** declarados con `CREATE TYPE ... AS ENUM`.
- **Arrays** (`text[]`, `numeric(7,2)[]`): se detectan desde el AST (nunca del
  texto) y se marca `TypeSpec.is_array`; el `kind` y los parámetros siguen
  siendo los del elemento (`text[]` ⇒ `kind='text'`; `numeric(7,2)[]` conserva
  `precision`/`scale`). Un array multidimensional (`text[][]`) se representa como
  una sola dimensión con un aviso, igual que hace PostgreSQL en la práctica. La
  **generación** de valores de array es del Hito 2; el Hito 1 solo representa y
  analiza (ADR-004).

Un tipo de PostgreSQL sin mapeo conocido **no aborta el parseo**: degrada a
`text` (el tipo más permisivo) con un aviso. Esto mantiene el proceso en marcha
pero puede producir datos menos realistas; añade el mapeo en
`parsing/types.py` o repórtalo.

### Construcciones que generan un aviso (reconocidas, aún no soportadas)

Ninguna de estas aborta el parseo del resto del esquema; todas se registran
como aviso con la tabla (y columna, si aplica) afectada:

- `GENERATED ALWAYS AS ...` (columnas calculadas) y `GENERATED ... AS IDENTITY`.
- `COLLATE`.
- `CREATE TYPE` que no sea `AS ENUM` (compuesto `AS (...)`, `AS RANGE`, tipo
  shell).
- `COMMENT ON` sobre objetos que no sean tabla o columna (índice, tipo...).
- `ALTER TABLE` que no sea `ADD CONSTRAINT` (`ADD COLUMN`, `DROP ...`).
- Sentencias de nivel superior distintas de `CREATE TABLE`/`CREATE TYPE`,
  `COMMENT ON` y `ALTER TABLE` (`CREATE INDEX`, `CREATE VIEW`, triggers,
  funciones, `INSERT`...).
- `FOREIGN KEY` sin cláusula `REFERENCES` reconocible.
- `REFERENCES tabla` sin columnas explícitas (apunta implícitamente a la
  `PRIMARY KEY` del padre): se avisa; la resolución contra esa PK la hace el
  grafo de dependencias, no el parser.
- Una `FOREIGN KEY` cuya tabla referenciada no existe en el esquema: el grafo
  la ignora para la planificación de fases con un aviso.

---

## CHECKs

`CHECK` se conserva siempre (texto y columnas involucradas). Además, un
subconjunto se **interpreta a cotas** que el generador podrá usar directamente
(`ast_supported=True`, `bounds_derived`). El re-parseo se hace con el parser de
expresiones de sqlglot, nunca con regex sobre el texto.

### Subconjunto interpretado

Siempre restringido a un `CHECK` que involucre **exactamente una** columna:

- Comparaciones `col <op> literal` y su forma invertida `literal <op> col`
  (normalizada): `>`, `>=`, `<`, `<=`, `=`, `<>`/`!=`.
- `BETWEEN a AND b` (cotas inclusivas en ambos extremos).
- `IN (...)` (lista cerrada de valores) y `NOT IN (...)` (valores excluidos).
- `NOT` sobre una comparación o sobre un `IN`.
- `AND` de cualquier combinación de lo anterior, intersecando las cotas.

Un `AND` cuya intersección es vacía (p. ej. `x > 5 AND x < 3`) se marca
igualmente como interpretado —PostgreSQL acepta la restricción— pero **emite un
aviso**: ninguna fila podrá cumplirlo nunca, y conviene saberlo antes de
generar.

Un `CHECK` fuera de este subconjunto simplemente queda sin interpretar
(`ast_supported=False`), **sin aviso**: es su estado normal, no un error.

### Recortes deliberados y su porqué

- **`OR` — incluso de una sola columna** (`x < 3 OR x > 9`): un `OR` describe
  una unión de rangos, no una cota simple de intervalo o de lista. El modelo de
  cotas actual (`min`/`max`/`values`/`excluded_values`) no puede representar
  esa unión sin mentir. Se deja como aserción no interpretada; podrá tratarse
  como regla del mini-DSL en un hito posterior. No genera aviso.
- **`LIKE` — pospuesto entero**, no solo los patrones con comodín al principio.
  Aun un prefijo puro (`col LIKE 'AB%'`) tendría un rango `['AB', 'AC')`
  representable, pero tratar `LIKE` bien exige distinguir el prefijo puro de los
  patrones con `%`/`_` intercalados y de los escapes, y como cota de generación
  aporta poco frente a esa complejidad. Si hace falta forzar un patrón, se
  declarará como regla del mini-DSL vía YAML, no como cota derivada del `CHECK`.
- **Predicados multi-columna** (`fecha_fin >= fecha_inicio`): quedan fuera del
  subconjunto de cotas por columna; son aserciones entre columnas de la misma
  fila, terreno del mini-DSL. Sin aviso.
- **Funciones** (`length(x) > 3`, `upper(x) = 'A'`), **casts** y
  **subconsultas**: no se interpretan. Sin aviso.

---

## Ciclos

El planificador de dependencias detecta ciclos entre tablas y
autorreferencias, y elige una estrategia de carga para cada uno. Solo cuando
ninguna es posible sin modificar el DDL se detiene con error.

### Ciclo entre dos o más tablas

En este orden de preferencia:

1. **Alguna FK del ciclo se puede anular** (desempate alfabético por `(tabla,
   primera columna)`): se insertan a `NULL` solo las columnas anulables de esa
   FK (el resto del ciclo, ya en orden de dependencia) y una fase de `UPDATE`
   posterior les asigna su valor real. Con `MATCH SIMPLE` (defecto) basta con
   que la FK tenga alguna columna anulable —se anula solo esa, aunque el resto
   sea `NOT NULL`, como la clave `(inmobiliaria_id, entidad_id)` del esquema
   real—; con `MATCH FULL` se exige que TODAS sus columnas admitan `NULL`,
   porque un `NULL` parcial la viola (ADR-004). La `InsertPhase`/`UpdatePhase`
   registran qué columnas se anulan, no solo qué FK. *(Fixtures:
   `ciclos_nullable.sql`, `crm_real_minimo.sql`.)*
2. **Ninguna es anulable, pero alguna es `DEFERRABLE`**: todas las tablas del
   ciclo se insertan en una única transacción con las constraints diferidas.
   *(Fixture: `ciclos_deferrable.sql`.)*
3. **Ninguna FK del ciclo es anulable ni diferible**: no existe secuencia de
   `INSERT`/`UPDATE` válida sin tocar el DDL. Ver [el caso irrompible](#el-caso-irrompible).

### Autorreferencia (una FK que apunta a la propia tabla)

- **FK anulable**: generación por niveles — las raíces (nivel 0) a `NULL`, cada
  nivel apunta al anterior. *(Fixture: `rrhh_autoref_nullable.sql`.)*
- **FK `NOT NULL` y `DEFERRABLE`**: carga diferida en una sola transacción.
- **FK `NOT NULL` y no diferible**: generación por niveles con las filas raíz
  referenciándose **a sí mismas** (única salida sin modificar el DDL), siempre
  acompañada de un aviso. *(Fixture: `rrhh_autoref_notnull.sql`.)*

### El caso irrompible

Dos o más tablas con FK mutuas `NOT NULL` y **ninguna** diferible (fixture
`ciclos_unbreakable.sql`): el DDL carga sin problema en PostgreSQL —el conflicto
es de generación de datos, no de esquema—, pero no hay ningún orden de carga
que respete todas las FK. SynthDB se detiene con un diagnóstico accionable
(las tablas y FK implicadas, y las tres salidas posibles) y **código de salida
2** en la CLI, en lugar de inventar datos o desactivar constraints. Las tres
salidas que ofrece el diagnóstico:

1. Marcar alguna de las FK implicadas como anulable.
2. Marcarla `DEFERRABLE INITIALLY DEFERRED`.
3. `--allow-ddl` para desactivar y reactivar la constraint durante la carga
   (desaconsejado; nunca es el comportamiento por defecto).
