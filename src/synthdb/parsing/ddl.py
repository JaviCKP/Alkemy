"""`sqlglot` AST → `SchemaSpec` (T1.3, especificacion.md §5 y §4.1).

Entrega 1 de 3 (plan-ejecucion-mvp.md, fila T1.3): de sentencias `CREATE
TABLE` se extrae nombre de tabla y schema/namespace, columnas en su orden
original, tipos vía `parsing.types.map_postgres_type` y `PRIMARY KEY` (inline
o a nivel de tabla, simple o compuesta). FK, `UNIQUE`, `CHECK`, `DEFAULT`,
enums y comentarios llegan en las entregas 2 y 3.

Toda construcción del DDL que el parser reconozca pero no maneje todavía
(FK, `UNIQUE`, `CHECK`, `DEFAULT`, `COMMENT`, `GENERATED`, triggers,
sentencias que no sean `CREATE TABLE`...) no aborta el parseo de lo que sí
se soporta: se registra como aviso en `SchemaSpec.warnings` con la tabla (y
columna, si aplica) afectada, nunca en silencio (CLAUDE.md).

La IR guarda la **identidad efectiva** de PostgreSQL para nombres de tabla,
schema/namespace, columnas e identificadores de `PRIMARY KEY`, no la grafía
literal del DDL: PostgreSQL pliega a minúsculas todo identificador sin
comillas (`CREATE TABLE Clientes` y `CREATE TABLE clientes` son la misma
tabla) y conserva tal cual uno entrecomillado (`"MiTabla"` sigue siendo
`MiTabla`). Sin este plegado, dos DDL equivalentes para PostgreSQL
producirían IRs y hashes distintos, y en la entrega 2 la resolución de FK
por nombre fallaría ante una diferencia que la base de datos ni ve. Ver
`_identifier_name`.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as _SqlglotParseError

from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec
from synthdb.parsing.types import map_postgres_type

_UNSUPPORTED_CONSTRAINT_LABELS: dict[type[exp.Expression], str] = {
    exp.CheckColumnConstraint: "CHECK",
    exp.DefaultColumnConstraint: "DEFAULT",
    exp.UniqueColumnConstraint: "UNIQUE",
    exp.Reference: "FOREIGN KEY (REFERENCES)",
    exp.ForeignKey: "FOREIGN KEY",
    exp.ComputedColumnConstraint: "GENERATED ALWAYS AS",
    exp.GeneratedAsIdentityColumnConstraint: "GENERATED ... AS IDENTITY",
    exp.CommentColumnConstraint: "COMMENT",
    exp.CollateColumnConstraint: "COLLATE",
}
"""Etiqueta legible para construcciones reconocidas pero aún no soportadas.

Un tipo ausente de este diccionario no se pierde en silencio: cae al
`type(...).__name__` crudo de sqlglot como aviso (peor presentación, pero
igualmente registrado), ver `_unsupported_construct_label`.
"""


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
    """Parsea DDL `CREATE TABLE` a la IR canónica (`SchemaSpec`).

    Args:
        sql: Texto DDL completo; puede tener una o varias sentencias.
        dialect: Dialecto SQL que usa sqlglot para tokenizar/parsear. El MVP
            solo promete resultados correctos para `"postgres"`
            (especificacion.md §2); el parámetro existe para no acoplar la
            firma a esa promesa concreta.

    Returns:
        La IR con las tablas reconocidas, en el orden en que aparecen en
        `sql`. `hash` queda `None`: lo calcula `ir/hashing.py` en un paso
        posterior del pipeline, no este parser.

    Raises:
        ParseError: si `sql` contiene un error de sintaxis para `dialect`.
    """
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except _SqlglotParseError as exc:
        raise _translate_parse_error(exc) from exc

    tables: list[TableSpec] = []
    warnings: list[str] = []

    for statement in statements:
        if statement is None:
            continue
        if isinstance(statement, exp.Create) and statement.kind == "TABLE":
            tables.append(_parse_create_table(statement, dialect, warnings))
        else:
            approx = statement.sql(dialect=dialect)
            warnings.append(
                f"sentencia no soportada todavía por el parser DDL: {approx!r} "
                "(ver docs/limitations.md)"
            )

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


def _parse_create_table(statement: exp.Create, dialect: str, warnings: list[str]) -> TableSpec:
    """Construye un `TableSpec` a partir de un nodo `CREATE TABLE`."""
    schema_node = statement.this
    table_node = schema_node.this if isinstance(schema_node, exp.Schema) else schema_node
    table_name = _identifier_name(table_node.this)
    namespace = table_node.args.get("db")
    schema_name = _identifier_name(namespace) if isinstance(namespace, exp.Identifier) else None

    entries = schema_node.expressions if isinstance(schema_node, exp.Schema) else []

    columns: list[ColumnSpec] = []
    inline_primary_key: list[str] = []
    table_primary_key: list[str] | None = None

    for entry in entries:
        if isinstance(entry, exp.ColumnDef):
            column, is_pk = _parse_column(entry, table_name, dialect, warnings)
            columns.append(column)
            if is_pk:
                inline_primary_key.append(column.name)
            continue

        for item in _unwrap_table_constraint(entry):
            if isinstance(item, exp.PrimaryKey):
                table_primary_key = [
                    _identifier_name(identifier) for identifier in item.expressions
                ]
            else:
                label = _unsupported_construct_label(item)
                warnings.append(
                    f"tabla {table_name}: restricción de tabla {label} no soportada "
                    "todavía; se ignora (ver docs/limitations.md)"
                )

    primary_key = table_primary_key if table_primary_key is not None else inline_primary_key
    if primary_key:
        pk_columns = set(primary_key)
        columns = [
            column.model_copy(update={"nullable": False}) if column.name in pk_columns else column
            for column in columns
        ]

    return TableSpec(name=table_name, schema=schema_name, columns=columns, primary_key=primary_key)


def _unwrap_table_constraint(entry: exp.Expression) -> list[exp.Expression]:
    """Desenvuelve un `CONSTRAINT nombre (...)` con nombre a sus nodos internos.

    Una restricción de tabla sin nombre (p. ej. `PRIMARY KEY (a, b)`) aparece
    directamente en `schema.expressions`; una con nombre (`CONSTRAINT pk_t
    PRIMARY KEY (a, b)`) llega envuelta en un nodo `Constraint` cuyo interés
    real está en `.expressions`. Tratarlos igual evita duplicar el resto del
    análisis para cada variante.
    """
    if isinstance(entry, exp.Constraint):
        return list(entry.expressions)
    return [entry]


def _parse_column(
    column_def: exp.ColumnDef, table_name: str, dialect: str, warnings: list[str]
) -> tuple[ColumnSpec, bool]:
    """Construye un `ColumnSpec` y señala si lleva `PRIMARY KEY` inline."""
    column_name = _identifier_name(column_def.this)
    raw_type, precision, scale, length = _type_components(column_def.args["kind"], dialect)
    mapping = map_postgres_type(raw_type, precision=precision, scale=scale, length=length)
    for warning in mapping.warnings:
        warnings.append(f"tabla {table_name}, columna {column_name}: {warning}")

    not_null = False
    is_primary_key = False
    for constraint in column_def.args.get("constraints") or []:
        kind = constraint.kind
        if isinstance(kind, exp.NotNullColumnConstraint):
            not_null = True
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            is_primary_key = True
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
    column = ColumnSpec(name=column_name, type=mapping.type_spec, nullable=not forced_not_null)
    return column, is_primary_key


def _type_components(
    data_type: exp.DataType, dialect: str
) -> tuple[str, int | None, int | None, int | None]:
    """Extrae `(nombre_de_tipo, precision, scale, length)` de un `DataType`.

    `map_postgres_type` espera el nombre de tipo separado de sus parámetros;
    sqlglot los devuelve juntos en la forma renderizada (p. ej.
    `"VARCHAR(50)"`), de ahí la separación manual por el primer paréntesis.
    Para tipos definidos por el usuario (candidatos a enum, entrega 2) no
    hay paréntesis que cortar: el nombre real vive en `DataType.args["kind"]`.
    """
    if data_type.this == exp.DataType.Type.USERDEFINED:
        identifier = data_type.args.get("kind")
        raw_type = (
            identifier.this
            if isinstance(identifier, exp.Identifier)
            else data_type.sql(dialect=dialect)
        )
        return raw_type, None, None, None

    rendered = data_type.sql(dialect=dialect)
    raw_type = rendered.split("(", 1)[0].strip()
    params = [
        int(param.this.this)
        for param in data_type.expressions
        if isinstance(param, exp.DataTypeParam)
    ]
    first = params[0] if len(params) >= 1 else None
    second = params[1] if len(params) >= 2 else None
    return raw_type, first, second, first


def _unsupported_construct_label(node: exp.Expression) -> str:
    """Etiqueta legible de una construcción reconocida pero no soportada aún."""
    return _UNSUPPORTED_CONSTRAINT_LABELS.get(type(node), type(node).__name__)
