"""`sqlglot` AST → `SchemaSpec` (T1.3, especificacion.md §5 y §4.1).

Entrega 2 de 3 (plan-ejecucion-mvp.md, fila T1.3): añade FOREIGN KEY (inline
y de tabla, simples y compuestas), UNIQUE (inline y de tabla), CHECK (de
columna y de tabla) y DEFAULT a lo ya soportado en la entrega 1 (columnas,
tipos, PRIMARY KEY). `CREATE TYPE ... AS ENUM`, `COMMENT ON` y
`ALTER TABLE` quedan para la entrega 3.

Toda construcción del DDL que el parser reconozca pero no maneje todavía
(enums, `COMMENT`, `GENERATED`, triggers, sentencias que no sean
`CREATE TABLE`...) no aborta el parseo de lo que sí se soporta: se registra
como aviso en `SchemaSpec.warnings` con la tabla (y columna, si aplica)
afectada, nunca en silencio (CLAUDE.md).

La IR guarda la **identidad efectiva** de PostgreSQL para nombres de tabla,
schema/namespace, columnas e identificadores de `PRIMARY KEY`/`FOREIGN
KEY`/`UNIQUE`, no la grafía literal del DDL: PostgreSQL pliega a minúsculas
todo identificador sin comillas (`CREATE TABLE Clientes` y
`CREATE TABLE clientes` son la misma tabla) y conserva tal cual uno
entrecomillado (`"MiTabla"` sigue siendo `MiTabla`). Sin este plegado, dos
DDL equivalentes para PostgreSQL producirían IRs y hashes distintos, y la
resolución de FK por nombre (grafo, T1.6) fallaría ante una diferencia que
la base de datos ni ve. Ver `_identifier_name`.
"""

from __future__ import annotations

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
from synthdb.parsing.types import map_postgres_type

_UNSUPPORTED_CONSTRAINT_LABELS: dict[type[exp.Expression], str] = {
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


class _ColumnParseResult(NamedTuple):
    """Columna parseada, más las señales para construir PK/UNIQUE/FK de tabla.

    El llamador (`_parse_create_table`) usa `is_primary_key`/`is_unique` para
    acumular `TableSpec.primary_key`/`uniques`, y `reference` para diferir la
    construcción del `RelationshipSpec` (ver `pending_foreign_keys`).
    """

    column: ColumnSpec
    is_primary_key: bool
    is_unique: bool
    reference: exp.Reference | None


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
    uniques: list[list[str]] = []
    table_checks: list[CheckSpec] = []
    # (columnas locales, nodo Reference) de cada FK vista, inline o de tabla.
    # No se resuelve a RelationshipSpec al vuelo: su `nullable` depende del
    # nullable *final* de esas columnas, y una PRIMARY KEY de tabla (que
    # puede aparecer más abajo en el DDL, después de la columna) todavía
    # puede forzarlas a NOT NULL. Se resuelven en un segundo paso, una vez
    # cerrado `columns`.
    pending_foreign_keys: list[tuple[list[str], exp.Reference]] = []

    for entry in entries:
        if isinstance(entry, exp.ColumnDef):
            result = _parse_column(entry, table_name, dialect, warnings)
            columns.append(result.column)
            if result.is_primary_key:
                inline_primary_key.append(result.column.name)
            if result.is_unique:
                uniques.append([result.column.name])
            if result.reference is not None:
                pending_foreign_keys.append(([result.column.name], result.reference))
            continue

        for item in _unwrap_table_constraint(entry):
            if isinstance(item, exp.PrimaryKey):
                table_primary_key = [
                    _identifier_name(identifier) for identifier in item.expressions
                ]
            elif isinstance(item, exp.ForeignKey):
                fk_columns = [_identifier_name(identifier) for identifier in item.expressions]
                reference = item.args.get("reference")
                if isinstance(reference, exp.Reference):
                    pending_foreign_keys.append((fk_columns, reference))
                else:
                    warnings.append(
                        f"tabla {table_name}: FOREIGN KEY ({', '.join(fk_columns)}) sin "
                        "cláusula REFERENCES reconocible; se ignora"
                    )
            elif isinstance(item, exp.UniqueColumnConstraint):
                uniques.append(_unique_constraint_columns(item))
            elif isinstance(item, exp.CheckColumnConstraint):
                table_checks.append(_build_check(item.this, dialect))
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
        # Una UNIQUE (inline o de tabla) sobre exactamente las columnas de la
        # PK no aporta nada nuevo: la PK ya garantiza esa unicidad.
        uniques = [group for group in uniques if set(group) != pk_columns]

    column_nullable = {column.name: column.nullable for column in columns}
    foreign_keys = [
        _build_relationship(fk_columns, reference, column_nullable, table_name, warnings)
        for fk_columns, reference in pending_foreign_keys
    ]

    return TableSpec(
        name=table_name,
        schema=schema_name,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        uniques=uniques,
        checks=table_checks,
    )


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
) -> _ColumnParseResult:
    """Construye un `ColumnSpec` y señala sus restricciones inline (PK/UNIQUE/FK)."""
    column_name = _identifier_name(column_def.this)
    raw_type, precision, scale, length = _type_components(column_def.args["kind"], dialect)
    mapping = map_postgres_type(raw_type, precision=precision, scale=scale, length=length)
    for warning in mapping.warnings:
        warnings.append(f"tabla {table_name}, columna {column_name}: {warning}")

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
            # Resuelta en _parse_create_table (ver pending_foreign_keys):
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

    namespace = table_node.args.get("db")
    ref_schema = _identifier_name(namespace) if isinstance(namespace, exp.Identifier) else None
    ref_table_name = _identifier_name(table_node.this)
    ref_table = f"{ref_schema}.{ref_table_name}" if ref_schema else ref_table_name
    return ref_table, ref_columns


def _build_relationship(
    columns: list[str],
    reference: exp.Reference,
    column_nullable: dict[str, bool],
    table_name: str,
    warnings: list[str],
) -> RelationshipSpec:
    """`RelationshipSpec` de una FK (inline o de tabla) ya resuelta contra `columns`."""
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
    for option in reference.args.get("options") or []:
        # sqlglot conserva la grafía original de DELETE/UPDATE en `option`
        # (p. ej. "ON delete CASCADE" si el DDL los escribió en minúsculas);
        # solo CASCADE/RESTRICT/... llegan ya normalizados a mayúsculas.
        normalized = option.upper()
        if normalized == "DEFERRABLE":
            deferrable = True
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
        deferrable=deferrable,
        nullable=all(column_nullable.get(name, True) for name in columns),
        # Derivado por graph/dependency.py (T1.6), no por este parser; ver
        # ir/schema.py y CLAUDE.md.
        cardinality_hint=None,
    )


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


def _type_components(
    data_type: exp.DataType, dialect: str
) -> tuple[str, int | None, int | None, int | None]:
    """Extrae `(nombre_de_tipo, precision, scale, length)` de un `DataType`.

    `map_postgres_type` espera el nombre de tipo separado de sus parámetros;
    sqlglot los devuelve juntos en la forma renderizada (p. ej.
    `"VARCHAR(50)"`), de ahí la separación manual por el primer paréntesis.
    Para tipos definidos por el usuario (candidatos a enum, entrega 3) no
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
