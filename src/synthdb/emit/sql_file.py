"""Emisor de `seed.sql` para PostgreSQL (T2.14, especificacion.md §11).

`synthdb export --format sql` recorre las **fases** del plan del `Dataset` y
produce un script cargable en un PostgreSQL que ya tenga el esquema creado
(`psql -v ON_ERROR_STOP=1 -f seed.sql`). El emisor no crea DDL: solo `INSERT`,
`UPDATE` y el marco transaccional.

Garantías del archivo (criterios del Hito 2 y CLAUDE.md):

- **Todo literal se renderiza con el generador de expresiones de sqlglot**
  (`exp.Literal`, `exp.Array`, `exp.null/true/false`, dialecto `postgres`).
  Jamás se concatena SQL a mano ni se escapa una comilla artesanalmente: esa es
  la barrera anti-inyección del archivo. sqlglot dobla las comillas simples de
  las cadenas y las dobles de los identificadores.
- **Orden de fases estricto.** Cada `Phase` del plan se emite en orden: los
  `INSERT` de una `InsertPhase`/`InsertLeveledPhase`/`DeferredPhase` y, después,
  la `UpdatePhase` que cierra un ciclo (columnas insertadas a `NULL` y luego
  actualizadas a su valor real). Cada fase va envuelta en `BEGIN`/`COMMIT`; la
  fase diferida añade `SET CONSTRAINTS ALL DEFERRED`.
- **`INSERT` multi-fila por lotes** del tamaño de `output.batch_size`.
- **Columnas autoincrementales (`SERIAL`) y `GENERATED` se omiten** del `INSERT`:
  las asigna la base de datos. Para un dataset sin cuarentena, la secuencia de
  PostgreSQL reproduce los mismos ids que usó el motor (1, 2, 3…), así que las
  FKs cuadran; ver `docs/limitations.md` sobre el caso con cuarentena.
- **Nombres cualificados con esquema** cuando la `TableSpec` lo tiene, y
  **identificadores entrecomillados solo cuando el plegado de PostgreSQL lo
  exige** (mayúsculas, caracteres especiales, palabra reservada): un nombre en
  minúsculas simple va sin comillas.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlglot import exp

from synthdb.config.models import Config
from synthdb.generation.engine import Dataset
from synthdb.ir.plans import (
    DeferredPhase,
    InsertLeveledPhase,
    InsertPhase,
    UpdatePhase,
)
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec

_DIALECT = "postgres"

_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*")
"""Identificador que PostgreSQL deja intacto sin comillas: minúsculas ASCII,
dígitos, `_` y `$`, sin empezar por dígito. Cualquier otra forma (mayúsculas,
espacios, acentos, símbolos) cambia al plegarse y por tanto exige comillas."""

_RESERVED_KEYWORDS: frozenset[str] = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "authorization",
        "between",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "collation",
        "column",
        "concurrently",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "system_user",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    }
)
"""Palabras reservadas de PostgreSQL que, como identificador, exigen comillas
aunque tengan forma «segura» (p. ej. `select`, `order`, `user`)."""


def _needs_quote(name: str) -> bool:
    """`True` si `name` debe entrecomillarse para no cambiar al plegarlo."""
    if not name or _SAFE_IDENTIFIER.fullmatch(name) is None:
        return True
    return name in _RESERVED_KEYWORDS


def _ident(name: str) -> str:
    """Renderiza un identificador, entrecomillado solo si el plegado lo exige."""
    return exp.to_identifier(name, quoted=_needs_quote(name)).sql(dialect=_DIALECT)


def _qualified_table(table: TableSpec) -> str:
    """Nombre de tabla cualificado con esquema cuando la `TableSpec` lo tiene."""
    name = _ident(table.name)
    if table.schema_:
        return f"{_ident(table.schema_)}.{name}"
    return name


def _element_type_sql(type_spec: TypeSpec) -> str:
    """Tipo SQL del elemento de una columna (sin la dimensión de array).

    Solo se usa para el `CAST` de un array **vacío** (`CAST(ARRAY[] AS TEXT[])`):
    PostgreSQL no puede inferir el tipo de `ARRAY[]` a secas.
    """
    kind = type_spec.kind
    if kind == "integer":
        return {16: "SMALLINT", 32: "INTEGER", 64: "BIGINT"}.get(type_spec.bits or 32, "INTEGER")
    if kind == "numeric":
        if type_spec.precision is not None and type_spec.scale is not None:
            return f"NUMERIC({type_spec.precision}, {type_spec.scale})"
        if type_spec.precision is not None:
            return f"NUMERIC({type_spec.precision})"
        return "NUMERIC"
    if kind == "varchar":
        return f"VARCHAR({type_spec.length})" if type_spec.length else "VARCHAR"
    if kind == "char":
        return f"CHAR({type_spec.length})" if type_spec.length else "CHAR"
    if kind == "timestamp":
        return "TIMESTAMPTZ" if type_spec.with_timezone else "TIMESTAMP"
    return {
        "text": "TEXT",
        "date": "DATE",
        "boolean": "BOOLEAN",
        "uuid": "UUID",
        "json": "JSON",
        "bytea": "BYTEA",
        "enum": "TEXT",
    }.get(kind, "TEXT")


def _literal(value: Any, column: ColumnSpec) -> exp.Expression:
    """Construye la expresión sqlglot del valor de una celda.

    Todo pasa por `exp.*`; no hay una sola concatenación de SQL a mano. Un array
    vacío se envuelve en un `CAST` a su tipo para que PostgreSQL lo acepte.
    """
    if value is None:
        return exp.null()
    if isinstance(value, bool):  # antes que int: bool es subclase de int
        return exp.true() if value else exp.false()
    if isinstance(value, int):
        return exp.Literal.number(str(value))
    if isinstance(value, float):
        return exp.Literal.number(repr(value))
    if isinstance(value, Decimal):
        return exp.Literal.number(str(value))
    if isinstance(value, datetime.date | datetime.datetime):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, bytes | bytearray):
        return exp.cast(exp.Literal.string("\\x" + bytes(value).hex()), "BYTEA")
    if isinstance(value, dict):
        return exp.Literal.string(json.dumps(value, ensure_ascii=False, sort_keys=True))
    if isinstance(value, list):
        if not value:
            return exp.cast(exp.Array(expressions=[]), f"{_element_type_sql(column.type)}[]")
        return exp.Array(expressions=[_literal(item, column) for item in value])
    if isinstance(value, str):
        return exp.Literal.string(value)
    return exp.Literal.string(str(value))


def _render_value(value: Any, column: ColumnSpec) -> str:
    """Renderiza el valor de una celda como literal SQL de PostgreSQL."""
    return _literal(value, column).sql(dialect=_DIALECT)


def _insert_columns(table: TableSpec) -> list[ColumnSpec]:
    """Columnas que van en el `INSERT`: se omiten autoincrement y generadas."""
    return [
        column for column in table.columns if not column.type.autoincrement and not column.generated
    ]


def _value_tuple(
    row: Mapping[str, Any], columns: Sequence[ColumnSpec], null_columns: frozenset[str]
) -> str:
    """Tupla `(v1, v2, …)` de una fila para el `VALUES` de un `INSERT`.

    Las columnas en `null_columns` se emiten como `NULL` aunque la fila ya tenga
    su valor real: se insertan a NULL para romper un ciclo y una `UpdatePhase`
    posterior las fija.
    """
    parts = [
        "NULL" if column.name in null_columns else _render_value(row.get(column.name), column)
        for column in columns
    ]
    return "(" + ", ".join(parts) + ")"


def _insert_statements(
    table: TableSpec,
    rows: Sequence[Mapping[str, Any]],
    null_columns: frozenset[str],
    batch_size: int,
) -> list[str]:
    """`INSERT` multi-fila por lotes de `batch_size` para una tabla."""
    if not rows:
        return []
    qualified = _qualified_table(table)
    columns = _insert_columns(table)
    if not columns:
        # Tabla cuyas únicas columnas las asigna la BD (p. ej. solo un SERIAL):
        # una fila por INSERT con DEFAULT VALUES (no admite multi-fila).
        return [f"INSERT INTO {qualified} DEFAULT VALUES;" for _ in rows]
    column_list = ", ".join(_ident(column.name) for column in columns)
    statements: list[str] = []
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        body = ",\n  ".join(_value_tuple(row, columns, null_columns) for row in chunk)
        statements.append(f"INSERT INTO {qualified} ({column_list}) VALUES\n  {body};")
    return statements


def _update_statements(
    table: TableSpec, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]
) -> list[str]:
    """`UPDATE` por fila que fija las columnas antes insertadas a `NULL`.

    Solo se emite para las filas cuyo valor final de esas columnas no es `NULL`
    (las que sí recibieron un padre real al cerrar el ciclo).
    """
    if not table.primary_key:
        raise ValueError(
            f"tabla {table.name}: una UpdatePhase requiere clave primaria para "
            "localizar la fila a actualizar."
        )
    by_name = {column.name: column for column in table.columns}
    qualified = _qualified_table(table)
    statements: list[str] = []
    for row in rows:
        changed = [(name, row[name]) for name in columns if row.get(name) is not None]
        if not changed:
            continue
        set_clause = ", ".join(
            f"{_ident(name)} = {_render_value(value, by_name[name])}" for name, value in changed
        )
        where = " AND ".join(
            f"{_ident(pk)} = {_render_value(row[pk], by_name[pk])}" for pk in table.primary_key
        )
        statements.append(f"UPDATE {qualified} SET {set_clause} WHERE {where};")
    return statements


def _emit_transaction(
    lines: list[str], statements: Sequence[str], *, comment: str, deferred: bool = False
) -> None:
    """Añade una fase a `lines` envuelta en `BEGIN`/`COMMIT` (si tiene contenido)."""
    if not statements:
        return
    lines.append("")
    lines.append(f"-- {comment}")
    lines.append("BEGIN;")
    if deferred:
        lines.append("SET CONSTRAINTS ALL DEFERRED;")
    lines.extend(statements)
    lines.append("COMMIT;")


def render_sql(spec: SchemaSpec, dataset: Dataset, config: Config) -> str:
    r"""Renderiza el `seed.sql` de un `Dataset` respetando el orden de fases.

    Args:
        spec: La IR del esquema; fija tablas, columnas, tipos y esquemas.
        dataset: Resultado del motor (filas válidas, fases y actualizaciones
            diferidas ya aplicadas en memoria).
        config: Configuración validada; de aquí sale `output.batch_size` y la
            semilla que se anota en la cabecera.

    Returns:
        El script SQL completo como texto UTF-8, terminado en `\\n`.
    """
    by_name = {table.name: table for table in spec.tables}
    batch_size = config.output.batch_size
    lines: list[str] = [
        "-- Generado por SynthDB (synthdb export --format sql).",
        f"-- Dialecto: postgres. Semilla: {config.seed}.",
        "-- Carga: psql -v ON_ERROR_STOP=1 -f seed.sql (el esquema debe existir ya).",
        "SET client_encoding = 'UTF8';",
        "SET standard_conforming_strings = on;",
    ]

    for phase in dataset.phases:
        if isinstance(phase, InsertPhase):
            null_by_table = {ref.table: frozenset(ref.null_columns) for ref in phase.null_fks}
            statements: list[str] = []
            for table_name in phase.tables:
                statements += _insert_statements(
                    by_name[table_name],
                    dataset.tables.get(table_name, []),
                    null_by_table.get(table_name, frozenset()),
                    batch_size,
                )
            _emit_transaction(lines, statements, comment=f"INSERT: {', '.join(phase.tables)}")
        elif isinstance(phase, InsertLeveledPhase):
            statements = _insert_statements(
                by_name[phase.table],
                dataset.tables.get(phase.table, []),
                frozenset(),
                batch_size,
            )
            _emit_transaction(
                lines, statements, comment=f"INSERT por niveles (autorreferencia): {phase.table}"
            )
        elif isinstance(phase, DeferredPhase):
            statements = []
            for table_name in phase.tables:
                statements += _insert_statements(
                    by_name[table_name],
                    dataset.tables.get(table_name, []),
                    frozenset(),
                    batch_size,
                )
            _emit_transaction(
                lines,
                statements,
                comment=f"INSERT diferido (ciclo): {', '.join(phase.tables)}",
                deferred=True,
            )
        elif isinstance(phase, UpdatePhase):
            statements = _update_statements(
                by_name[phase.table],
                dataset.tables.get(phase.table, []),
                phase.columns,
            )
            _emit_transaction(
                lines,
                statements,
                comment=f"UPDATE diferido: {phase.table} ({', '.join(phase.columns)})",
            )

    return "\n".join(lines) + "\n"
