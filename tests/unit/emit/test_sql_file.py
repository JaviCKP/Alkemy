"""Tests del emisor `seed.sql` de PostgreSQL (T2.14).

Dos frentes: casos de literal/identificador construidos a mano (comillas,
apóstrofes, backslashes, Unicode, arrays vacíos, NULL, schema cualificado,
palabras reservadas) y el orden de fases end-to-end sobre los fixtures reales
(`ciclos_nullable` y `crm_real_minimo`), donde importa que el `INSERT` con
`NULL` preceda al `UPDATE` que cierra el ciclo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from synthdb.config.models import Config, TableConfig
from synthdb.emit.sql_file import _ident, _needs_quote, render_sql
from synthdb.generation.engine import Dataset, generate_dataset
from synthdb.ir.plans import InsertPhase
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _schema(name: str) -> SchemaSpec:
    return parse_ddl((_SCHEMAS / name).read_text("utf-8"))


# --- Identificadores: entrecomillar solo cuando el plegado lo exige ----------


def test_plain_lowercase_identifier_is_not_quoted() -> None:
    assert _needs_quote("clientes") is False
    assert _ident("clientes") == "clientes"


def test_identifiers_that_fold_or_are_reserved_are_quoted() -> None:
    # Mayúsculas (plegarían a minúsculas), espacios, acentos y palabras
    # reservadas: todas exigen comillas dobles.
    for name in ("MiTabla", "weird name", "año", "select", "user", "order"):
        assert _needs_quote(name) is True
    assert _ident("MiTabla") == '"MiTabla"'
    assert _ident("select") == '"select"'


# --- Literales: comillas, backslashes, Unicode, arrays, NULL, schema ---------


def _literal_case_schema() -> SchemaSpec:
    """Tabla `public."MiTabla"` con SERIAL, texto libre, array y palabra reservada."""
    return SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="MiTabla",
                schema_="public",
                columns=[
                    ColumnSpec(
                        name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
                    ),
                    ColumnSpec(name="texto", type=TypeSpec(kind="text"), nullable=True),
                    ColumnSpec(
                        name="tags", type=TypeSpec(kind="text", is_array=True), nullable=True
                    ),
                    ColumnSpec(name="select", type=TypeSpec(kind="integer"), nullable=True),
                ],
                primary_key=["id"],
            )
        ],
    )


def _render_literal_case(rows: list[dict[str, object]]) -> str:
    spec = _literal_case_schema()
    dataset = Dataset(tables={"MiTabla": rows}, phases=[InsertPhase(tables=["MiTabla"])])
    return render_sql(spec, dataset, Config())


def test_qualified_name_quoted_identifiers_and_autoincrement_omitted() -> None:
    sql = _render_literal_case([{"id": 1, "texto": "x", "tags": ["a"], "select": 5}])
    # Nombre cualificado con esquema y el nombre de tabla entrecomillado.
    assert 'INSERT INTO public."MiTabla"' in sql
    # `select` (reservada) va entrecomillada; `texto`/`tags` no; y la columna
    # autoincremental `id` NO aparece: la lista es exactamente esas tres.
    assert '(texto, tags, "select")' in sql


def test_string_literals_escape_quotes_backslashes_and_keep_unicode() -> None:
    value = "O'Brien " + chr(92) + " café ñ 日本"  # apóstrofe, backslash y Unicode
    sql = _render_literal_case([{"id": 1, "texto": value, "tags": ["a'b"], "select": None}])
    # La comilla simple se dobla; el backslash queda literal (standard strings);
    # el Unicode se conserva.
    assert "'O''Brien " + chr(92) + " café ñ 日本'" in sql
    # Los elementos del array también escapan su comilla.
    assert "ARRAY['a''b']" in sql
    # Un valor None se emite como NULL.
    assert "NULL" in sql


def test_empty_array_is_cast_to_its_column_type() -> None:
    sql = _render_literal_case([{"id": 1, "texto": None, "tags": [], "select": 1}])
    assert "CAST(ARRAY[] AS TEXT[])" in sql


def test_header_declares_utf8_and_standard_strings() -> None:
    sql = _render_literal_case([{"id": 1, "texto": "x", "tags": ["a"], "select": 1}])
    assert "SET client_encoding = 'UTF8';" in sql
    assert "SET standard_conforming_strings = on;" in sql


# --- Orden de fases end-to-end ----------------------------------------------


def test_nullable_cycle_inserts_null_then_updates() -> None:
    spec = _schema("ciclos_nullable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=11, tables={"pedidos": TableConfig(rows=6), "facturas": TableConfig(rows=6)}),
    )
    sql = render_sql(spec, dataset, Config(seed=11))
    insert_pos = sql.index("INSERT INTO pedidos")
    update_pos = sql.index("UPDATE pedidos SET")
    # El INSERT de pedidos (con factura_id NULL) precede al UPDATE que lo enlaza.
    assert insert_pos < update_pos
    pedidos_insert = sql[insert_pos:update_pos]
    assert "NULL" in pedidos_insert  # factura_id se inserta a NULL
    assert "factura_id" in sql[update_pos:]  # y se fija después con UPDATE


def test_phases_are_wrapped_in_begin_commit() -> None:
    spec = _schema("ciclos_nullable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=11, tables={"pedidos": TableConfig(rows=4), "facturas": TableConfig(rows=4)}),
    )
    sql = render_sql(spec, dataset, Config(seed=11))
    assert sql.count("BEGIN;") == sql.count("COMMIT;")
    assert sql.count("BEGIN;") >= 2  # fase de INSERT y fase de UPDATE


def test_deferred_cycle_sets_constraints_deferred() -> None:
    spec = _schema("ciclos_deferrable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=13, tables={"pedidos": TableConfig(rows=10), "facturas": TableConfig(rows=10)}),
    )
    sql = render_sql(spec, dataset, Config(seed=13))
    assert "SET CONSTRAINTS ALL DEFERRED;" in sql


def test_multi_row_insert_respects_batch_size() -> None:
    spec = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL);")
    dataset = generate_dataset(
        spec,
        Config(seed=1, tables={"t": TableConfig(rows=5)}, output={"batch_size": 2}),
    )
    sql = render_sql(spec, dataset, Config(seed=1, output={"batch_size": 2}))
    # 5 filas en lotes de 2 ⇒ 3 sentencias INSERT (2 + 2 + 1).
    assert sql.count("INSERT INTO t") == 3


@pytest.mark.parametrize("fixture", ["crm_real_minimo", "ciclos_nullable"])
def test_generated_seed_sql_parses_as_postgres(fixture: str) -> None:
    # Señal fuerte sin base de datos: cada sentencia del seed.sql (SET, BEGIN,
    # INSERT multi-fila, CAST(ARRAY[] AS ...), UPDATE, COMMIT) debe parsear como
    # PostgreSQL válido. No sustituye al test @integration, pero atrapa cualquier
    # error de sintaxis antes de llegar a la BD.
    spec = _schema(f"{fixture}.sql")
    counts = (
        {"inmobiliarias": 5, "clientes": 30, "matches": 30}
        if fixture == "crm_real_minimo"
        else {"pedidos": 8, "facturas": 8}
    )
    config = Config(seed=5, tables={name: TableConfig(rows=n) for name, n in counts.items()})
    sql = render_sql(spec, generate_dataset(spec, config), config)
    statements = sqlglot.parse(sql, dialect="postgres")
    assert statements and all(statement is not None for statement in statements)
