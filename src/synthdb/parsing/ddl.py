"""`sqlglot` AST → `SchemaSpec` (T1.3, especificacion.md §5 y §4.1).

Entrega 3 de 3 (plan-ejecucion-mvp.md, fila T1.3), cierre del hito: añade
`CREATE TYPE ... AS ENUM`, `COMMENT ON TABLE`/`COMMENT ON COLUMN` y
`ALTER TABLE ... ADD CONSTRAINT` (FOREIGN KEY, UNIQUE, CHECK, PRIMARY KEY) a
lo ya soportado en las entregas 1 (columnas, tipos, PRIMARY KEY) y 2 (FK,
UNIQUE, CHECK, DEFAULT).

El parseo recorre las sentencias en **dos pasadas** para que su orden en el
archivo no importe: la primera resuelve todos los `CREATE TYPE` y
`CREATE TABLE` (una tabla puede usar un enum declarado más abajo en el
archivo); la segunda aplica `COMMENT ON` y `ALTER TABLE` sobre las tablas ya
construidas, incluida una FK o PK que un `ALTER` añada después de su
`CREATE TABLE` (ver `_PendingTable`, `_finalize_table`).

Toda construcción del DDL que el parser reconozca pero no maneje todavía
(tipos de `CREATE TYPE` que no sean enum, `COMMENT ON` de objetos que no
sean tabla/columna, variantes de `ALTER TABLE` distintas de `ADD
CONSTRAINT`, `GENERATED`, triggers, sentencias que no sean `CREATE
TABLE`...) no aborta el parseo de lo que sí se soporta: se registra como
aviso en `SchemaSpec.warnings` con la tabla (y columna, si aplica)
afectada, nunca en silencio (CLAUDE.md).

La IR guarda la **identidad efectiva** de PostgreSQL para nombres de tabla,
schema/namespace, columnas e identificadores de `PRIMARY KEY`/`FOREIGN
KEY`/`UNIQUE`/tipo, no la grafía literal del DDL: PostgreSQL pliega a
minúsculas todo identificador sin comillas (`CREATE TABLE Clientes` y
`CREATE TABLE clientes` son la misma tabla) y conserva tal cual uno
entrecomillado (`"MiTabla"` sigue siendo `MiTabla`). Sin este plegado, dos
DDL equivalentes para PostgreSQL producirían IRs y hashes distintos, y la
resolución de FK por nombre (grafo, T1.6) fallaría ante una diferencia que
la base de datos ni ve. Los valores de un enum son literales, no
identificadores, y por tanto no se pliegan. Ver `_identifier_name`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as _SqlglotParseError

from synthdb.ir.schema import (
    CheckSpec,
    ColumnSpec,
    DefaultSpec,
    ReferentialAction,
    RelationshipSpec,
    SchemaSpec,
    TableSpec,
)
from synthdb.parsing.dialect import (
    POSTGRES_SET_NULL_COLUMNS,
    SET_NULL_COLUMNS_ARG,
    PostgresSetNullColumns,
)
from synthdb.parsing.types import map_postgres_type

_UNSUPPORTED_CONSTRAINT_LABELS: dict[type[exp.Expression], str] = {
    exp.ComputedColumnConstraint: "GENERATED ALWAYS AS",
    exp.GeneratedAsIdentityColumnConstraint: "GENERATED ... AS IDENTITY",
    exp.CommentColumnConstraint: "COMMENT",
    exp.CollateColumnConstraint: "COLLATE",
    exp.ColumnDef: "ADD COLUMN",
    exp.Drop: "DROP ...",
}
"""Etiqueta legible para construcciones reconocidas pero aún no soportadas.

Un tipo ausente de este diccionario no se pierde en silencio: cae al
`type(...).__name__` crudo de sqlglot como aviso (peor presentación, pero
igualmente registrado), ver `_unsupported_construct_label`.
"""

_ON_ACTION_MAP: dict[str, ReferentialAction] = {
    "CASCADE": "cascade",
    "RESTRICT": "restrict",
    "SET NULL": "set_null",
    "SET DEFAULT": "set_default",
    "NO ACTION": "no_action",
}
"""Traduce la acción `ON DELETE`/`ON UPDATE` de sqlglot al `ReferentialAction` canónico."""


class ParseError(Exception):
    """Error de sintaxis SQL al parsear un DDL.

    Envuelve el `sqlglot.errors.ParseError` original (accesible vía
    `__cause__`) en un tipo propio: quien llama a `parse_ddl` nunca ve
    directamente un traceback crudo de sqlglot (CLAUDE.md), sino un mensaje
    con línea, columna y la sentencia aproximada donde ocurrió el error.

    Attributes:
        line: Línea (1-indexada) donde sqlglot detectó el error, si la supo.
        col: Columna (1-indexada) donde sqlglot detectó el error, si la supo.
        statement: Fragmento de la sentencia alrededor del error.
    """

    def __init__(self, message: str, *, line: int | None, col: int | None, statement: str) -> None:
        super().__init__(message)
        self.line = line
        self.col = col
        self.statement = statement


def parse_ddl(sql: str, dialect: str = "postgres") -> SchemaSpec:
    """Parsea DDL de PostgreSQL a la IR canónica (`SchemaSpec`).

    Soporta `CREATE TABLE`, `CREATE TYPE ... AS ENUM`, `COMMENT ON
    TABLE`/`COMMENT ON COLUMN` y `ALTER TABLE ... ADD CONSTRAINT`.

    Args:
        sql: Texto DDL completo; puede tener una o varias sentencias.
        dialect: Dialecto SQL que usa sqlglot para tokenizar/parsear. El MVP
            solo promete resultados correctos para `"postgres"`
            (especificacion.md §2); el parámetro existe para no acoplar la
            firma a esa promesa concreta.

    Returns:
        La IR con las tablas reconocidas, en el orden en que aparece su
        `CREATE TABLE` en `sql` (el de `COMMENT ON`/`ALTER TABLE` no
        importa). `hash` queda `None`: lo calcula `ir/hashing.py` en un paso
        posterior del pipeline, no este parser.

    Raises:
        ParseError: si `sql` contiene un error de sintaxis para `dialect`.
    """
    # Para PostgreSQL se usa el dialecto extendido (ADR-004): acepta la lista
    # de columnas de `ON DELETE SET NULL (…)` de PostgreSQL 15 que el dialecto
    # base rechaza. Es un superconjunto del parser base, así que no cambia nada
    # para un DDL que no use esa sintaxis.
    read: str | PostgresSetNullColumns = (
        POSTGRES_SET_NULL_COLUMNS if dialect == "postgres" else dialect
    )
    try:
        parsed = sqlglot.parse(sql, read=read)
    except _SqlglotParseError as exc:
        raise _translate_parse_error(exc) from exc

    statements = [statement for statement in parsed if statement is not None]
    warnings: list[str] = []

    # Primera pasada, en dos sub-pasadas: todos los CREATE TYPE antes que
    # todos los CREATE TABLE, para que una columna pueda usar un enum
    # declarado más abajo en el archivo.
    enum_types: dict[str, list[str]] = {}
    for statement in statements:
        if isinstance(statement, exp.Create) and statement.kind == "TYPE":
            _register_create_type(statement, enum_types, warnings)

    pending_tables: dict[tuple[str | None, str], _PendingTable] = {}
    order: list[tuple[str | None, str]] = []
    for statement in statements:
        if isinstance(statement, exp.Create) and statement.kind == "TABLE":
            pending = _parse_create_table(statement, dialect, enum_types, warnings)
            key = (pending.schema_name, pending.name)
            pending_tables[key] = pending
            order.append(key)

    # Segunda pasada: COMMENT ON y ALTER TABLE sobre las tablas ya
    # construidas; cualquier otra sentencia de nivel superior (incluidos los
    # CREATE TYPE/CREATE TABLE, ya procesados arriba) queda fuera de aquí.
    for statement in statements:
        if isinstance(statement, exp.Create) and statement.kind in ("TYPE", "TABLE"):
            continue
        if isinstance(statement, exp.Comment):
            _apply_comment(statement, pending_tables, warnings)
        elif isinstance(statement, exp.Alter) and statement.kind == "TABLE":
            _apply_alter_table(statement, pending_tables, dialect, warnings)
        else:
            approx = statement.sql(dialect=dialect)
            warnings.append(
                f"sentencia no soportada todavía por el parser DDL: {approx!r} "
                "(ver docs/limitations.md)"
            )

    tables = [_finalize_table(pending_tables[key], warnings) for key in order]
    return SchemaSpec(dialect=dialect, tables=tables, warnings=warnings)


def _identifier_name(identifier: exp.Identifier) -> str:
    """Nombre efectivo de un identificador, con el plegado de PostgreSQL.

    Sin comillas, PostgreSQL pliega el identificador a minúsculas antes de
    usarlo como nombre real; entrecomillado, lo conserva tal cual. sqlglot
    expone esa distinción en `Identifier.quoted`, así que basta con mirarla
    aquí en vez de reinterpretar el texto.
    """
    name = str(identifier.this)
    return name if identifier.quoted else name.lower()


def _schema_and_name(table_node: exp.Table) -> tuple[str | None, str]:
    """`(schema, nombre)` de un nodo `Table`, ambos ya plegados.

    Centraliza la extracción que antes se repetía en cada sitio que lee un
    `Table` (tabla de un `CREATE TABLE`, objetivo de una `REFERENCES`, tabla
    de un `COMMENT ON`/`ALTER TABLE`): mismo criterio de plegado en todos.
    """
    namespace = table_node.args.get("db")
    schema_name = _identifier_name(namespace) if isinstance(namespace, exp.Identifier) else None
    name = _identifier_name(table_node.this)
    return schema_name, name


def _qualified(schema_name: str | None, name: str) -> str:
    """`"schema.nombre"`, o solo `"nombre"` si no hay schema."""
    return f"{schema_name}.{name}" if schema_name else name


def _translate_parse_error(exc: _SqlglotParseError) -> ParseError:
    """Convierte un `sqlglot.errors.ParseError` en nuestro `ParseError`."""
    detail = exc.errors[0] if exc.errors else {}
    line = detail.get("line")
    col = detail.get("col")
    statement = "".join(
        str(detail.get(part) or "") for part in ("start_context", "highlight", "end_context")
    )
    message = (
        f"Error de sintaxis SQL en línea {line}, columna {col}: "
        f"{detail.get('description', str(exc))}. Sentencia aproximada: {statement!r}"
    )
    return ParseError(message, line=line, col=col, statement=statement)


class _ColumnParseResult(NamedTuple):
    """Columna parseada, más las señales para construir PK/UNIQUE/FK de tabla.

    El llamador (`_parse_create_table`) usa `is_primary_key`/`is_unique` para
    acumular `TableSpec.primary_key`/`uniques`, y `reference` para diferir la
    construcción del `RelationshipSpec` (ver `_PendingTable.pending_foreign_keys`).
    """

    column: ColumnSpec
    is_primary_key: bool
    is_unique: bool
    reference: exp.Reference | None


@dataclass
class _PendingTable:
    """Estado mutable de una tabla mientras se aplican CREATE/ALTER/COMMENT.

    `_parse_create_table` la construye a partir de un `CREATE TABLE`;
    `_apply_comment`/`_apply_alter_table` (segunda pasada) siguen
    modificándola; `_finalize_table` la cierra a un `TableSpec` inmutable al
    final, una vez que ningún `ALTER TABLE` posterior puede ya cambiar la
    nulabilidad de sus columnas ni sus FK pendientes.
    """

    name: str
    schema_name: str | None
    columns: list[ColumnSpec]
    inline_primary_key: list[str]
    table_primary_key: list[str] | None = None
    uniques: list[list[str]] = field(default_factory=list)
    checks: list[CheckSpec] = field(default_factory=list)
    comment: str | None = None
    pending_foreign_keys: list[tuple[list[str], exp.Reference]] = field(default_factory=list)


def _parse_create_table(
    statement: exp.Create,
    dialect: str,
    enum_types: dict[str, list[str]],
    warnings: list[str],
) -> _PendingTable:
    """Construye el `_PendingTable` de un nodo `CREATE TABLE`.

    No fuerza todavía el `nullable=False` de la PK ni resuelve las FK
    pendientes a `RelationshipSpec`: ambas cosas dependen del estado *final*
    de la tabla, que un `ALTER TABLE` posterior en el mismo DDL puede seguir
    cambiando. Eso lo hace `_finalize_table`, al cierre de la segunda pasada.
    """
    schema_node = statement.this
    table_node = schema_node.this if isinstance(schema_node, exp.Schema) else schema_node
    schema_name, table_name = _schema_and_name(table_node)

    entries = schema_node.expressions if isinstance(schema_node, exp.Schema) else []

    columns: list[ColumnSpec] = []
    inline_primary_key: list[str] = []
    pending = _PendingTable(
        name=table_name,
        schema_name=schema_name,
        columns=columns,
        inline_primary_key=inline_primary_key,
    )

    for entry in entries:
        if isinstance(entry, exp.ColumnDef):
            result = _parse_column(entry, table_name, dialect, enum_types, warnings)
            columns.append(result.column)
            if result.is_primary_key:
                inline_primary_key.append(result.column.name)
            if result.is_unique:
                pending.uniques.append([result.column.name])
            if result.reference is not None:
                pending.pending_foreign_keys.append(([result.column.name], result.reference))
            continue

        for item in _unwrap_table_constraint(entry):
            _apply_table_constraint(item, pending, dialect, warnings)

    return pending


def _finalize_table(pending: _PendingTable, warnings: list[str]) -> TableSpec:
    """Cierra un `_PendingTable` a `TableSpec`, con todo `ALTER`/`COMMENT` ya aplicado.

    Aquí, no antes, se fuerza `nullable=False` en las columnas de la PK
    (inline, de tabla, o añadida después vía `ALTER TABLE ... ADD CONSTRAINT
    PRIMARY KEY`) y se resuelven las FK pendientes contra el nullable *final*
    de sus columnas locales.
    """
    primary_key = (
        pending.table_primary_key
        if pending.table_primary_key is not None
        else pending.inline_primary_key
    )

    columns = pending.columns
    uniques = pending.uniques
    if primary_key:
        pk_columns = set(primary_key)
        columns = [
            column.model_copy(update={"nullable": False}) if column.name in pk_columns else column
            for column in columns
        ]
        # Una UNIQUE (inline, de tabla o de ALTER) sobre exactamente las
        # columnas de la PK no aporta nada nuevo: la PK ya garantiza esa
        # unicidad.
        uniques = [group for group in uniques if set(group) != pk_columns]

    column_nullable = {column.name: column.nullable for column in columns}
    foreign_keys = [
        _build_relationship(fk_columns, reference, column_nullable, pending.name, warnings)
        for fk_columns, reference in pending.pending_foreign_keys
    ]

    return TableSpec(
        name=pending.name,
        schema=pending.schema_name,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        uniques=uniques,
        checks=pending.checks,
        comment=pending.comment,
    )


def _unwrap_table_constraint(entry: exp.Expression) -> list[exp.Expression]:
    """Desenvuelve un `CONSTRAINT nombre (...)` con nombre a sus nodos internos.

    Una restricción sin nombre (p. ej. `PRIMARY KEY (a, b)`, tanto en un
    `CREATE TABLE` como en un `ALTER TABLE ... ADD`) aparece directamente
    como el nodo a tratar; una con nombre (`CONSTRAINT pk_t PRIMARY KEY (a,
    b)`) llega envuelta en un nodo `Constraint` cuyo interés real está en
    `.expressions`. Tratarlos igual evita duplicar el resto del análisis
    para cada variante.
    """
    if isinstance(entry, exp.Constraint):
        return list(entry.expressions)
    return [entry]


def _apply_table_constraint(
    item: exp.Expression,
    pending: _PendingTable,
    dialect: str,
    warnings: list[str],
) -> None:
    """Aplica una restricción de tabla (PK/FK/UNIQUE/CHECK) ya desenvuelta.

    Común a las restricciones de tabla de un `CREATE TABLE` y a las de un
    `ALTER TABLE ... ADD CONSTRAINT`: ambas llegan como el mismo tipo de nodo
    de sqlglot, así que la entrega 3 reutiliza íntegra la lógica de la
    entrega 2 en vez de duplicarla.
    """
    if isinstance(item, exp.PrimaryKey):
        pending.table_primary_key = [
            _identifier_name(identifier) for identifier in item.expressions
        ]
    elif isinstance(item, exp.ForeignKey):
        fk_columns = [_identifier_name(identifier) for identifier in item.expressions]
        reference = item.args.get("reference")
        if isinstance(reference, exp.Reference):
            pending.pending_foreign_keys.append((fk_columns, reference))
        else:
            warnings.append(
                f"tabla {pending.name}: FOREIGN KEY ({', '.join(fk_columns)}) sin "
                "cláusula REFERENCES reconocible; se ignora"
            )
    elif isinstance(item, exp.UniqueColumnConstraint):
        pending.uniques.append(_unique_constraint_columns(item))
    elif isinstance(item, exp.CheckColumnConstraint):
        pending.checks.append(_build_check(item.this, dialect))
    else:
        label = _unsupported_construct_label(item)
        warnings.append(
            f"tabla {pending.name}: restricción de tabla {label} no soportada "
            "todavía; se ignora (ver docs/limitations.md)"
        )


def _parse_column(
    column_def: exp.ColumnDef,
    table_name: str,
    dialect: str,
    enum_types: dict[str, list[str]],
    warnings: list[str],
) -> _ColumnParseResult:
    """Construye un `ColumnSpec` y señala sus restricciones inline (PK/UNIQUE/FK)."""
    column_name = _identifier_name(column_def.this)
    components = _type_components(column_def.args["kind"], dialect)
    enum_values = enum_types.get(components.raw_type)
    mapping = map_postgres_type(
        components.raw_type,
        precision=components.precision,
        scale=components.scale,
        length=components.length,
        is_enum=enum_values is not None,
        is_array=components.is_array,
    )
    for warning in mapping.warnings:
        warnings.append(f"tabla {table_name}, columna {column_name}: {warning}")
    if components.multidimensional:
        original = column_def.args["kind"].sql(dialect=dialect)
        warnings.append(
            f"tabla {table_name}, columna {column_name}: array multidimensional "
            f"{original!r}; se representa como una sola dimensión (ADR-004; ver "
            "docs/limitations.md)"
        )

    not_null = False
    is_primary_key = False
    is_unique = False
    checks: list[CheckSpec] = []
    default: DefaultSpec | None = None
    reference: exp.Reference | None = None

    for constraint in column_def.args.get("constraints") or []:
        kind = constraint.kind
        if isinstance(kind, exp.NotNullColumnConstraint):
            not_null = True
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            is_primary_key = True
        elif isinstance(kind, exp.UniqueColumnConstraint):
            is_unique = True
        elif isinstance(kind, exp.CheckColumnConstraint):
            checks.append(_build_check(kind.this, dialect))
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = _build_default(kind.this, dialect)
        elif isinstance(kind, exp.Reference):
            # Resuelta en _finalize_table (ver _PendingTable.pending_foreign_keys):
            # aquí solo se conoce el nullable *provisional* de la columna.
            reference = kind
        else:
            label = _unsupported_construct_label(kind)
            warnings.append(
                f"tabla {table_name}, columna {column_name}: restricción {label} no "
                "soportada todavía; se ignora (ver docs/limitations.md)"
            )

    # PRIMARY KEY y serial/bigserial/smallserial (autoincrement) implican NOT
    # NULL en PostgreSQL aunque el DDL no lo declare aparte: sqlglot no añade
    # un NotNullColumnConstraint independiente para ninguno de los dos casos.
    forced_not_null = not_null or is_primary_key or mapping.type_spec.autoincrement
    column = ColumnSpec(
        name=column_name,
        type=mapping.type_spec,
        nullable=not forced_not_null,
        default=default,
        enum_values=enum_values,
        checks=checks,
    )
    return _ColumnParseResult(
        column=column, is_primary_key=is_primary_key, is_unique=is_unique, reference=reference
    )


def _reference_target(reference: exp.Reference) -> tuple[str, list[str]]:
    """`(ref_table, ref_columns)` de un nodo `Reference`, con `ref_table` plegado.

    `ref_table` incluye el namespace (`"ventas.clientes"`) cuando el DDL lo
    declara explícitamente, igual que `TableSpec.name`/`schema`. Sin columnas
    explícitas (`REFERENCES tabla`, apunta implícitamente a su PK),
    `reference.this` es directamente un `Table` en vez de un `Schema` que lo
    envuelve junto a la lista de columnas.
    """
    target = reference.this
    if isinstance(target, exp.Schema):
        table_node = target.this
        ref_columns = [_identifier_name(identifier) for identifier in target.expressions]
    else:
        table_node = target
        ref_columns = []

    schema_name, table_name = _schema_and_name(table_node)
    return _qualified(schema_name, table_name), ref_columns


def _build_relationship(
    columns: list[str],
    reference: exp.Reference,
    column_nullable: dict[str, bool],
    table_name: str,
    warnings: list[str],
) -> RelationshipSpec:
    """`RelationshipSpec` de una FK (inline, de tabla o de ALTER) ya resuelta contra `columns`."""
    ref_table, ref_columns = _reference_target(reference)
    if not ref_columns:
        warnings.append(
            f"tabla {table_name}: FOREIGN KEY ({', '.join(columns)}) referencia a "
            f"{ref_table} sin columnas explícitas (apunta a su clave primaria); la "
            "resolución contra esa PK es del grafo de dependencias (T1.6), no de "
            "este parser, que no asume que la tabla referenciada ya se ha parseado."
        )

    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None
    deferrable = False
    match_full = False
    for option in reference.args.get("options") or []:
        # sqlglot conserva la grafía original de DELETE/UPDATE en `option`
        # (p. ej. "ON delete CASCADE" si el DDL los escribió en minúsculas);
        # solo CASCADE/RESTRICT/... llegan ya normalizados a mayúsculas.
        normalized = option.upper()
        if normalized == "DEFERRABLE":
            deferrable = True
        elif normalized == "MATCH FULL":
            # MATCH SIMPLE (defecto) y MATCH PARTIAL no se distinguen del
            # defecto para la rotura de ciclos; solo MATCH FULL cambia la
            # política (NULL parcial la viola). Ver ADR-004 y graph/strategies.
            match_full = True
        elif normalized.startswith("ON DELETE "):
            on_delete = _ON_ACTION_MAP.get(normalized.removeprefix("ON DELETE "))
        elif normalized.startswith("ON UPDATE "):
            on_update = _ON_ACTION_MAP.get(normalized.removeprefix("ON UPDATE "))

    return RelationshipSpec(
        columns=columns,
        ref_table=ref_table,
        ref_columns=ref_columns,
        on_delete=on_delete,
        on_update=on_update,
        on_delete_set_columns=_set_null_columns(reference, columns, table_name, warnings),
        deferrable=deferrable,
        match_full=match_full,
        nullable=all(column_nullable.get(name, True) for name in columns),
        # Derivado: subconjunto anulable de la FK (ADR-004). Excluido del hash.
        nullable_columns=[name for name in columns if column_nullable.get(name, True)],
        # Derivado por graph/dependency.py (T1.6), no por este parser; ver
        # ir/schema.py y CLAUDE.md.
        cardinality_hint=None,
    )


def _set_null_columns(
    reference: exp.Reference,
    columns: list[str],
    table_name: str,
    warnings: list[str],
) -> list[str]:
    """Columnas de `ON DELETE SET NULL/SET DEFAULT (…)`, plegadas y validadas.

    El dialecto extendido (`parsing/dialect.py`, ADR-004) adjunta los
    identificadores de la lista de PostgreSQL 15 al argumento
    `SET_NULL_COLUMNS_ARG` de la `Reference`. Deben ser un subconjunto de las
    columnas locales de la FK; si el DDL nombra alguna ajena, se conserva pero
    se emite un aviso (CLAUDE.md: nada en silencio).
    """
    nodes = reference.args.get(SET_NULL_COLUMNS_ARG) or []
    set_columns = [_identifier_name(node) for node in nodes if isinstance(node, exp.Identifier)]

    fk_columns = set(columns)
    extraneous = [name for name in set_columns if name not in fk_columns]
    if extraneous:
        warnings.append(
            f"tabla {table_name}: FOREIGN KEY ({', '.join(columns)}) declara "
            f"ON DELETE SET NULL/DEFAULT sobre columnas ajenas a la FK "
            f"({', '.join(extraneous)}); se conservan pero revisa el DDL "
            "(ver docs/limitations.md)"
        )
    return set_columns


def _unique_constraint_columns(item: exp.UniqueColumnConstraint) -> list[str]:
    """Columnas de un `UNIQUE` de tabla (simple o compuesto), plegadas.

    Solo se usa para la variante de tabla: la variante inline no lleva lista
    de columnas propia (`item.this` es `None`), así que esa la resuelve
    directamente `_parse_column` con el nombre de la columna que envuelve.
    """
    schema = item.this
    if isinstance(schema, exp.Schema):
        return [_identifier_name(identifier) for identifier in schema.expressions]
    return []


def _build_check(predicate: exp.Expression, dialect: str) -> CheckSpec:
    """`CheckSpec` de un predicado `CHECK`, de columna o de tabla.

    `ast_supported` y `bounds_derived` quedan siempre en `False`/`None` en
    esta entrega: interpretar el predicado para propagar cotas al generador
    es T1.4 (`constraints/check_interp.py`), no trabajo del parser DDL.
    """
    columns_involved: list[str] = []
    seen: set[str] = set()
    for column_ref in predicate.find_all(exp.Column):
        name = _identifier_name(column_ref.this)
        if name not in seen:
            seen.add(name)
            columns_involved.append(name)

    return CheckSpec(
        sql_text=predicate.sql(dialect=dialect),
        ast_supported=False,
        columns_involved=columns_involved,
        bounds_derived=None,
    )


def _numeric_literal_value(text: str) -> int | float:
    """Valor Python de un literal numérico de sqlglot (`Literal.this`, siempre `str`)."""
    if "." in text or "e" in text.lower():
        return float(text)
    return int(text)


def _build_default(expression: exp.Expression, dialect: str) -> DefaultSpec:
    """`DefaultSpec` de un `DEFAULT`: literal ya tipado, o expresión (solo texto).

    Un literal numérico negativo (`DEFAULT -1`) llega como `Neg` envolviendo
    un `Literal`, no como un `Literal` con el signo incluido — sqlglot no
    colapsa el signo dentro del propio literal — de ahí el caso aparte.
    """
    sql_text = expression.sql(dialect=dialect)

    if isinstance(expression, exp.Null):
        return DefaultSpec(kind="literal", sql_text=sql_text, value=None)
    if isinstance(expression, exp.Boolean):
        return DefaultSpec(kind="literal", sql_text=sql_text, value=bool(expression.this))
    if isinstance(expression, exp.Literal):
        value = expression.this if expression.is_string else _numeric_literal_value(expression.this)
        return DefaultSpec(kind="literal", sql_text=sql_text, value=value)
    if (
        isinstance(expression, exp.Neg)
        and isinstance(expression.this, exp.Literal)
        and not expression.this.is_string
    ):
        negated = -_numeric_literal_value(expression.this.this)
        return DefaultSpec(kind="literal", sql_text=sql_text, value=negated)

    return DefaultSpec(kind="expression", sql_text=sql_text, value=None)


class _TypeComponents(NamedTuple):
    """Componentes de un tipo de columna, ya separada su dimensión de array.

    `raw_type`/`precision`/`scale`/`length` describen el tipo del ELEMENTO
    (`text[]` ⇒ `raw_type="text"`), listos para `map_postgres_type`. `is_array`
    marca que la columna es un array; `multidimensional`, que el DDL declaró más
    de una dimensión (`text[][]`), que se colapsa a una sola con aviso (ADR-004).
    """

    raw_type: str
    precision: int | None
    scale: int | None
    length: int | None
    is_array: bool
    multidimensional: bool


def _type_components(data_type: exp.DataType, dialect: str) -> _TypeComponents:
    """Extrae los componentes de un `DataType`, desenvolviendo su dimensión de array.

    El sufijo de array se detecta SIEMPRE desde el AST de sqlglot (`DataType`
    con `this == ARRAY`), nunca desde el texto (ADR-004, CLAUDE.md); el tipo del
    elemento se procesa igual que un escalar. `map_postgres_type` espera el
    nombre de tipo separado de sus parámetros; sqlglot los devuelve juntos en la
    forma renderizada (`"VARCHAR(50)"`), de ahí la separación por el primer
    paréntesis. Para tipos definidos por el usuario (candidatos a enum) no hay
    paréntesis que cortar: el nombre real vive en `DataType.args["kind"]`, y se
    pliega igual que cualquier otro identificador para buscarlo en `enum_types`.
    """
    is_array = False
    multidimensional = False
    while data_type.this == exp.DataType.Type.ARRAY:
        element = data_type.expressions[0] if data_type.expressions else None
        if not isinstance(element, exp.DataType):
            break
        multidimensional = is_array  # una segunda vuelta ⇒ array de arrays (text[][])
        is_array = True
        data_type = element

    if data_type.this == exp.DataType.Type.USERDEFINED:
        identifier = data_type.args.get("kind")
        raw_type = (
            _identifier_name(identifier)
            if isinstance(identifier, exp.Identifier)
            else data_type.sql(dialect=dialect)
        )
        return _TypeComponents(raw_type, None, None, None, is_array, multidimensional)

    rendered = data_type.sql(dialect=dialect)
    raw_type = rendered.split("(", 1)[0].strip()
    params = [
        int(param.this.this)
        for param in data_type.expressions
        if isinstance(param, exp.DataTypeParam)
    ]
    first = params[0] if len(params) >= 1 else None
    second = params[1] if len(params) >= 2 else None
    return _TypeComponents(raw_type, first, second, first, is_array, multidimensional)


def _register_create_type(
    statement: exp.Create,
    enum_types: dict[str, list[str]],
    warnings: list[str],
) -> None:
    """Registra un `CREATE TYPE ... AS ENUM` en `enum_types` (nombre ya plegado).

    Cualquier otra variante de `CREATE TYPE` (compuesto `AS (...)`, `AS
    RANGE`, tipo shell sin cuerpo...) no está soportada todavía: se registra
    como aviso en vez de asumir un enum vacío o abortar el parseo.
    """
    type_name = _identifier_name(statement.this.this)
    expression = statement.expression
    if isinstance(expression, exp.DataType) and expression.this == exp.DataType.Type.ENUM:
        enum_types[type_name] = [str(literal.this) for literal in expression.expressions]
    else:
        warnings.append(
            f"CREATE TYPE {type_name}: solo se soporta la forma AS ENUM (...); esta "
            "variante se ignora (ver docs/limitations.md)"
        )


def _column_ref(column_node: exp.Column) -> tuple[str | None, str, str]:
    """`(schema, tabla, columna)` de un nodo `Column` de `COMMENT ON COLUMN`, plegados."""
    namespace = column_node.args.get("db")
    schema_name = _identifier_name(namespace) if isinstance(namespace, exp.Identifier) else None
    table_identifier = column_node.args.get("table")
    table_name = (
        _identifier_name(table_identifier) if isinstance(table_identifier, exp.Identifier) else ""
    )
    column_name = _identifier_name(column_node.this)
    return schema_name, table_name, column_name


def _apply_comment(
    statement: exp.Comment,
    tables: dict[tuple[str | None, str], _PendingTable],
    warnings: list[str],
) -> None:
    """Aplica un `COMMENT ON TABLE`/`COMMENT ON COLUMN` sobre una tabla ya parseada.

    Una referencia a una tabla o columna que no aparece en este DDL, o un
    `COMMENT ON` de otro tipo de objeto (índice, tipo...), se registra como
    aviso: el comentario es metadato opcional y nunca bloquea el resto del
    parseo (CLAUDE.md).
    """
    kind = statement.args.get("kind")
    normalized_kind = kind.upper() if kind else ""
    comment_text = statement.expression.this

    if normalized_kind == "TABLE":
        schema_name, table_name = _schema_and_name(statement.this)
        pending = tables.get((schema_name, table_name))
        if pending is None:
            warnings.append(
                f"COMMENT ON TABLE {_qualified(schema_name, table_name)}: la tabla no "
                "está declarada en este DDL; se ignora"
            )
            return
        pending.comment = comment_text
    elif normalized_kind == "COLUMN":
        schema_name, table_name, column_name = _column_ref(statement.this)
        pending = tables.get((schema_name, table_name))
        if pending is None:
            warnings.append(
                f"COMMENT ON COLUMN {_qualified(schema_name, table_name)}.{column_name}: "
                "la tabla no está declarada en este DDL; se ignora"
            )
            return
        for index, column in enumerate(pending.columns):
            if column.name == column_name:
                pending.columns[index] = column.model_copy(update={"comment": comment_text})
                break
        else:
            warnings.append(
                f"COMMENT ON COLUMN {pending.name}.{column_name}: la columna no está "
                "declarada en la tabla; se ignora"
            )
    else:
        warnings.append(
            f"COMMENT ON {kind}: tipo de objeto no soportado todavía (solo TABLE y "
            "COLUMN); se ignora (ver docs/limitations.md)"
        )


def _apply_alter_table(
    statement: exp.Alter,
    tables: dict[tuple[str | None, str], _PendingTable],
    dialect: str,
    warnings: list[str],
) -> None:
    """Aplica un `ALTER TABLE ... ADD CONSTRAINT` sobre una tabla ya parseada.

    Solo `ADD CONSTRAINT` (FOREIGN KEY, UNIQUE, CHECK, PRIMARY KEY) está
    soportado en esta entrega; otras variantes (`ADD COLUMN`, `DROP...`) y
    un `ALTER TABLE` sobre una tabla no declarada en este DDL se registran
    como aviso, nunca en silencio.
    """
    schema_name, table_name = _schema_and_name(statement.this)
    pending = tables.get((schema_name, table_name))
    if pending is None:
        warnings.append(
            f"ALTER TABLE {_qualified(schema_name, table_name)}: la tabla no está "
            "declarada en este DDL; se ignora"
        )
        return

    for action in statement.args.get("actions") or []:
        if isinstance(action, exp.AddConstraint):
            for item in action.expressions:
                for constraint in _unwrap_table_constraint(item):
                    _apply_table_constraint(constraint, pending, dialect, warnings)
        else:
            label = _unsupported_construct_label(action)
            warnings.append(
                f"tabla {pending.name}: ALTER TABLE {label} no soportado todavía; se "
                "ignora (ver docs/limitations.md)"
            )


def _unsupported_construct_label(node: exp.Expression) -> str:
    """Etiqueta legible de una construcción reconocida pero no soportada aún."""
    return _UNSUPPORTED_CONSTRAINT_LABELS.get(type(node), type(node).__name__)
