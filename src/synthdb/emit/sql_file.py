r"""Emisor de `seed.sql` para PostgreSQL (T2.14, especificacion.md §11).

`synthdb export --format sql` recorre las **fases** del plan del `Dataset` y
produce un script cargable en un PostgreSQL que ya tenga el esquema creado
(`psql -v ON_ERROR_STOP=1 -f seed.sql`). El emisor no crea DDL: solo `INSERT`,
`UPDATE` y el marco transaccional.

Garantías del archivo (criterios del Hito 2 y CLAUDE.md):

- **Los literales escalares** se construyen como expresiones de sqlglot
  (`exp.Literal`, `exp.null/true/false`, dialecto `postgres`). sqlglot escapa el
  literal SQL exterior: dobla las comillas simples de las cadenas y las dobles
  de los identificadores.
- **Los arrays** (vacíos o no) se emiten como un único literal de texto en el
  formato nativo de arrays de PostgreSQL (`'{}'`, `'{a,b}'`, `'{"a,b","c\"d"}'`).
  El contenido de cada elemento se codifica según §8.15.2, «Array Value Input»:
  comas, llaves, comillas dobles, backslashes, espacios, cadena vacía y el
  texto `NULL` se protegen dentro del array, mientras que un `None` real queda
  como elemento SQL `NULL`. Después, sqlglot escapa el literal SQL exterior que
  envuelve ese texto. Así PostgreSQL resuelve el array contra el tipo real de la
  columna destino, incluidos `enum[]`, sin que el emisor tenga que reconstruir
  el nombre del tipo. El round-trip de todos los tipos soportados se verifica
  contra PostgreSQL real en CI.
- **Orden de fases estricto.** Cada `Phase` del plan se emite en orden: los
  `INSERT` de una `InsertPhase`/`InsertLeveledPhase`/`DeferredPhase` y, después,
  la `UpdatePhase` que cierra un ciclo (columnas insertadas a `NULL` y luego
  actualizadas a su valor real). Cada fase va envuelta en `BEGIN`/`COMMIT`; la
  fase diferida añade `SET CONSTRAINTS ALL DEFERRED`.
- **`INSERT` multi-fila por lotes** del tamaño de `output.batch_size`.
- **Columnas autoincrementales (`SERIAL`) y `GENERATED` se omiten** del `INSERT`:
  las asigna la base de datos. Para un dataset sin cuarentena, la secuencia de
  PostgreSQL reproduce los mismos ids que usó el motor (1, 2, 3…), así que las
  FKs cuadran; con huecos por cuarentena en una tabla autoincremental, `render_sql`
  **rechaza el dataset** (`ExportIntegrityError`) en vez de emitir un `seed.sql`
  que violaría la integridad referencial al recargarse (ver `_check_autoincrement_sequence`).
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
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec

_DIALECT = "postgres"

_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*")
"""Identificador que PostgreSQL deja intacto sin comillas: minúsculas ASCII,
dígitos, `_` y `$`, sin empezar por dígito. Cualquier otra forma (mayúsculas,
espacios, acentos, símbolos) cambia al plegarse y por tanto exige comillas."""

_LINE_BREAK_CHARS = re.compile("[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")
"""Todo carácter que Python (y por tanto un lector de texto ingenuo) trata como
fin de línea: `\\r`, `\\n`, `\\v`, `\\f`, los separadores de registro/grupo
(`\\x1c`-`\\x1e`), `NEL` (`\\x85`) y los separadores Unicode de línea/párrafo
(`\\u2028`/`\\u2029`). Superconjunto deliberado de `str.splitlines()`."""


def _sanitize_comment_text(text: str) -> str:
    r"""Reemplaza cualquier terminador de línea por un espacio.

    Los nombres de tabla/columna vienen de la IR (en última instancia, del
    DDL) y viajan sin escapar dentro de comentarios `-- ...`: un nombre que
    contenga un salto de línea podría cerrar el comentario e introducir una
    línea SQL ejecutable (p. ej. una tabla llamada
    ``"x\\nDROP TABLE victims; --"``). Los identificadores y literales reales
    de los `INSERT`/`UPDATE` siempre pasan por sqlglot (`_ident`/`_literal`),
    que sí es seguro frente a esto; esta función protege el único lugar del
    archivo que interpola texto crudo: los comentarios informativos. Se aplica
    en el único punto por el que pasan todos ellos (`_emit_transaction`), para
    que ninguna llamada futura pueda olvidarlo.
    """
    return _LINE_BREAK_CHARS.sub(" ", text)


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


_ARRAY_ELEMENT_NEEDS_QUOTE = re.compile(r'[,{}"\\\s]')
"""Caracteres que el formato de texto nativo de un array de PostgreSQL usa
como delimitadores o espacio en blanco: si un elemento contiene alguno, debe
ir entrecomillado con `"..."` dentro del array (PostgreSQL, «8.15.2. Array
Value Input»)."""


def _quote_array_element(text: str) -> str:
    r"""Entrecomilla UN elemento ya convertido a texto si el formato de array lo exige.

    Vacío, la palabra `NULL` (sin distinguir mayúsculas: sin comillas se
    leería como un elemento NULL real) o cualquier carácter reservado del
    formato (`_ARRAY_ELEMENT_NEEDS_QUOTE`) disparan el entrecomillado, con `\\`
    y `"` internos doblados/escapados.
    """
    if text == "" or text.upper() == "NULL" or _ARRAY_ELEMENT_NEEDS_QUOTE.search(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _array_element_text(value: Any) -> str:
    """Representa un elemento en el formato de texto nativo de PostgreSQL.

    `None` es un elemento SQL `NULL` (sin comillas). Los objetos y listas se
    serializan como JSON compacto antes de aplicar el escapado del formato de
    arrays; esto conserva JSON anidado en columnas `json[]` en vez de usar la
    representación Python con comillas simples.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # antes que int: bool es subclase de int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.date | datetime.datetime):
        return _quote_array_element(value.isoformat())
    if isinstance(value, bytes | bytearray):
        return _quote_array_element("\\x" + bytes(value).hex())
    if isinstance(value, dict | list | tuple):
        return _quote_array_element(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    if isinstance(value, str):
        return _quote_array_element(value)
    return _quote_array_element(str(value))


def _literal(value: Any) -> exp.Expression:
    r"""Construye la expresión sqlglot del valor de una celda.

    Los escalares pasan por la expresión correspondiente de sqlglot. Un array
    se convierte primero a su representación textual nativa de PostgreSQL y
    ese texto completo se entrega a `exp.Literal.string`; el contenido interno
    y el literal SQL exterior son, por tanto, dos capas de escapado distintas.
    El literal sin tipo permite que PostgreSQL lo resuelva contra la columna de
    destino, también cuando el elemento es un enum o un tipo con sintaxis propia.
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
        inner = ",".join(_array_element_text(item) for item in value)
        return exp.Literal.string("{" + inner + "}")
    if isinstance(value, str):
        return exp.Literal.string(value)
    return exp.Literal.string(str(value))


def _render_value(value: Any) -> str:
    """Renderiza el valor de una celda como literal SQL de PostgreSQL."""
    return _literal(value).sql(dialect=_DIALECT)


class ExportIntegrityError(ValueError):
    """`render_sql` no puede producir un `seed.sql` íntegro para este `Dataset`."""


def _insert_columns(table: TableSpec) -> list[ColumnSpec]:
    """Columnas que van en el `INSERT`: se omiten autoincrement y generadas."""
    return [
        column for column in table.columns if not column.type.autoincrement and not column.generated
    ]


def _check_autoincrement_sequence(table: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
    """Rechaza una tabla cuyos ids autoincrementales aceptados no son 1..N.

    Las columnas autoincrementales se omiten del `INSERT` (`_insert_columns`):
    la asigna la secuencia `SERIAL` de PostgreSQL, EN EL ORDEN de inserción, a
    partir de 1. Con `Dataset` completo (sin cuarentena) eso reproduce
    exactamente los ids que usó el motor (T2.14). Pero una fila apartada por
    cuarentena dentro de una tabla con autoincrement deja un HUECO en el medio
    (p. ej. `[1, 3, 4, 5]`: la fila 2 se apartó) — un `schema.sql` recién
    cargado asignaría `[1, 2, 3, 4]` a esas MISMAS filas en su orden de
    inserción, un valor distinto del que el `Dataset` en memoria registró. Eso
    desalinea silenciosamente cualquier FK de otra tabla que sobreviviera
    apuntando al id ORIGINAL (p. ej. una FK hacia el id `5`, que tras cargar
    ya no existe: solo hay 4 filas) y también el propio `WHERE {pk} = …` de
    una `UpdatePhase` sobre esta misma tabla.

    No hay traducción general de claves aquí (esa es la responsabilidad del
    emisor de BD del H4, con el mapa id_dataset→id_bd vía `RETURNING`):
    revisión del PR #42, hallazgo 3. Aquí solo se detecta la contradicción,
    ANTES de escribir una sola línea, y se rechaza con un error accionable en
    vez de producir un `seed.sql` que violaría la integridad referencial al
    cargarse en un PostgreSQL limpio.

    Raises:
        ExportIntegrityError: si alguna columna autoincremental de `table`
            tiene, entre las filas aceptadas, un conjunto de valores que no es
            exactamente `1..len(rows)` en ese orden.
    """
    if not rows:
        return
    for column in table.columns:
        if not column.type.autoincrement:
            continue
        actual = [row.get(column.name) for row in rows]
        expected = list(range(1, len(actual) + 1))
        if actual == expected:
            continue
        preview = actual if len(actual) <= 10 else [*actual[:10], "…"]
        raise ExportIntegrityError(
            f"tabla {table.name}, columna {column.name}: los ids aceptados "
            f"({preview}) no forman la secuencia contigua 1..{len(actual)} que "
            "PostgreSQL asignaría a esta columna autoincremental al omitirla del "
            "INSERT. Es señal de filas en cuarentena en esta tabla: cargar este "
            "seed.sql reasignaría estos ids y dejaría colgando cualquier FK u "
            "UPDATE que en el Dataset original apuntara a un id posterior al hueco. "
            "No se puede exportar a SQL una tabla autoincremental con cuarentena; "
            "usa --format csv/json (llevan el id en la propia fila, sin este "
            "problema) o corrige los datos para que la tabla no quede en "
            "cuarentena."
        )


def _value_tuple(
    row: Mapping[str, Any], columns: Sequence[ColumnSpec], null_columns: frozenset[str]
) -> str:
    """Tupla `(v1, v2, …)` de una fila para el `VALUES` de un `INSERT`.

    Las columnas en `null_columns` se emiten como `NULL` aunque la fila ya tenga
    su valor real: se insertan a NULL para romper un ciclo y una `UpdatePhase`
    posterior las fija.
    """
    parts = [
        "NULL" if column.name in null_columns else _render_value(row.get(column.name))
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
    qualified = _qualified_table(table)
    statements: list[str] = []
    for row in rows:
        changed = [(name, row[name]) for name in columns if row.get(name) is not None]
        if not changed:
            continue
        set_clause = ", ".join(
            f"{_ident(name)} = {_render_value(value)}" for name, value in changed
        )
        where = " AND ".join(f"{_ident(pk)} = {_render_value(row[pk])}" for pk in table.primary_key)
        statements.append(f"UPDATE {qualified} SET {set_clause} WHERE {where};")
    return statements


def _emit_transaction(
    lines: list[str], statements: Sequence[str], *, comment: str, deferred: bool = False
) -> None:
    """Añade una fase a `lines` envuelta en `BEGIN`/`COMMIT` (si tiene contenido).

    `comment` puede incluir nombres de tabla arbitrarios (hallazgo de
    seguridad, revisión PR #42): se sanea con `_sanitize_comment_text` antes de
    interpolarse, único punto por el que pasa todo comentario del archivo.
    """
    if not statements:
        return
    lines.append("")
    lines.append(f"-- {_sanitize_comment_text(comment)}")
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

    Raises:
        ExportIntegrityError: si alguna tabla con columna autoincremental
            tiene, entre sus filas aceptadas, un hueco en la secuencia de ids
            (típicamente por cuarentena) — ver `_check_autoincrement_sequence`.
            Se comprueban TODAS las tablas antes de renderizar una sola línea.
    """
    by_name = {table.name: table for table in spec.tables}
    for table in spec.tables:
        _check_autoincrement_sequence(table, dataset.tables.get(table.name, []))
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
