"""Tests de src/synthdb/parsing/ddl.py (T1.3, entrega 1/3: columnas, tipos, PK).

FK, UNIQUE, CHECK, DEFAULT, enums y comentarios son las entregas 2 y 3
(plan-ejecucion-mvp.md, fila T1.3): aquí solo se comprueba que su presencia
en el DDL no rompe el parseo de lo que sí se soporta y que queda registrada
como aviso en vez de perderse en silencio (CLAUDE.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlglot.errors import ParseError as SqlglotParseError
from syrupy.assertion import SnapshotAssertion

from synthdb.ir.hashing import schema_hash
from synthdb.ir.schema import SchemaSpec, TableSpec
from synthdb.parsing.ddl import ParseError, parse_ddl

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _table(schema: SchemaSpec, name: str) -> TableSpec:
    for table in schema.tables:
        if table.name == name:
            return table
    raise AssertionError(f"tabla {name!r} no encontrada en {[t.name for t in schema.tables]}")


def test_minimal_table_with_inline_primary_key() -> None:
    sql = """
        CREATE TABLE clientes (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            email TEXT
        );
    """

    schema = parse_ddl(sql)

    assert schema.dialect == "postgres"
    table = _table(schema, "clientes")
    assert table.primary_key == ["id"]
    assert [c.name for c in table.columns] == ["id", "nombre", "email"]
    assert table.columns[0].type.kind == "integer"
    assert table.columns[0].type.autoincrement is True
    assert table.columns[1].nullable is False
    assert table.columns[2].nullable is True


def test_inline_primary_key_column_is_forced_not_nullable() -> None:
    # PRIMARY KEY implica NOT NULL en PostgreSQL aunque el DDL no lo escriba
    # explícitamente (sqlglot no añade un NotNullColumnConstraint aparte).
    sql = "CREATE TABLE t (id INT PRIMARY KEY, nombre TEXT);"

    table = _table(parse_ddl(sql), "t")

    assert table.columns[0].nullable is False


def test_serial_column_without_primary_key_is_still_not_nullable() -> None:
    # serial expande a "integer NOT NULL DEFAULT nextval(...)": el NOT NULL
    # va incluido aunque la columna no sea (ni forme parte de) la PK.
    sql = "CREATE TABLE t (contador SERIAL, id INT PRIMARY KEY);"

    table = _table(parse_ddl(sql), "t")

    contador = table.columns[0]
    assert contador.type.autoincrement is True
    assert contador.nullable is False


def test_composite_primary_key_at_table_level() -> None:
    sql = """
        CREATE TABLE pedido_items (
            pedido_id INT NOT NULL,
            producto_id INT NOT NULL,
            cantidad INT NOT NULL,
            PRIMARY KEY (pedido_id, producto_id)
        );
    """

    table = _table(parse_ddl(sql), "pedido_items")

    assert table.primary_key == ["pedido_id", "producto_id"]


def test_composite_primary_key_columns_are_forced_not_nullable() -> None:
    sql = """
        CREATE TABLE pedido_items (
            pedido_id INT,
            producto_id INT,
            PRIMARY KEY (pedido_id, producto_id)
        );
    """

    table = _table(parse_ddl(sql), "pedido_items")

    assert [c.nullable for c in table.columns] == [False, False]


def test_named_table_level_primary_key_constraint() -> None:
    sql = """
        CREATE TABLE pedidos (
            id INT,
            CONSTRAINT pedidos_pkey PRIMARY KEY (id)
        );
    """

    table = _table(parse_ddl(sql), "pedidos")

    assert table.primary_key == ["id"]


def test_table_without_primary_key() -> None:
    sql = "CREATE TABLE log (mensaje TEXT NOT NULL, creado_en TIMESTAMP NOT NULL);"

    table = _table(parse_ddl(sql), "log")

    assert table.primary_key == []


def test_column_order_is_preserved() -> None:
    sql = """
        CREATE TABLE t (
            z_col INT,
            a_col INT,
            m_col INT
        );
    """

    table = _table(parse_ddl(sql), "t")

    assert [c.name for c in table.columns] == ["z_col", "a_col", "m_col"]


@pytest.mark.parametrize(
    "column_sql,expected_kind,expected_attrs",
    [
        ("precio NUMERIC(7, 2)", "numeric", {"precision": 7, "scale": 2}),
        ("nombre VARCHAR(50)", "varchar", {"length": 50}),
        ("cantidad SMALLINT", "integer", {"bits": 16}),
    ],
)
def test_types_with_parameters_are_mapped_via_map_postgres_type(
    column_sql: str, expected_kind: str, expected_attrs: dict[str, int]
) -> None:
    sql = f"CREATE TABLE t (id INT PRIMARY KEY, {column_sql});"

    table = _table(parse_ddl(sql), "t")

    column_type = table.columns[1].type
    assert column_type.kind == expected_kind
    for attr, value in expected_attrs.items():
        assert getattr(column_type, attr) == value


def test_unknown_column_type_degrades_to_text_and_propagates_the_warning() -> None:
    sql = "CREATE TABLE t (id INT PRIMARY KEY, ubicacion POINT);"

    schema = parse_ddl(sql)

    table = _table(schema, "t")
    assert table.columns[1].type.kind == "text"
    assert any(
        "t" in warning and "ubicacion" in warning and "POINT" in warning
        for warning in schema.warnings
    )


def test_explicit_namespace_is_captured_as_schema() -> None:
    sql = "CREATE TABLE ventas.users (id INT PRIMARY KEY);"

    table = _table(parse_ddl(sql), "users")

    assert table.schema_ == "ventas"


def test_table_without_explicit_namespace_has_no_schema() -> None:
    sql = "CREATE TABLE users (id INT PRIMARY KEY);"

    table = _table(parse_ddl(sql), "users")

    assert table.schema_ is None


def test_unquoted_identifiers_fold_to_lowercase_like_postgres() -> None:
    # Para PostgreSQL, "CREATE TABLE Clientes" y "CREATE TABLE clientes" son
    # la misma tabla: sin plegado, dos DDL equivalentes producirían IRs (y
    # hashes) distintos.
    mixed_case = parse_ddl("CREATE TABLE Clientes (ID SERIAL PRIMARY KEY, Nombre TEXT);")
    lowercase = parse_ddl("CREATE TABLE clientes (id SERIAL PRIMARY KEY, nombre TEXT);")

    assert mixed_case == lowercase
    assert schema_hash(mixed_case) == schema_hash(lowercase)


def test_quoted_identifiers_preserve_case() -> None:
    sql = 'CREATE TABLE "MiTabla" ("MiColumna" TEXT, id INT PRIMARY KEY);'

    table = _table(parse_ddl(sql), "MiTabla")

    assert table.columns[0].name == "MiColumna"


def test_unquoted_table_with_a_quoted_column_mixes_both_rules() -> None:
    sql = 'CREATE TABLE Clientes ("Nombre" TEXT, ID INT PRIMARY KEY);'

    table = _table(parse_ddl(sql), "clientes")

    assert table.columns[0].name == "Nombre"
    assert table.columns[1].name == "id"


def test_explicit_namespace_folds_to_lowercase_when_unquoted() -> None:
    table = _table(parse_ddl("CREATE TABLE Ventas.Users (id INT PRIMARY KEY);"), "users")

    assert table.schema_ == "ventas"


def test_table_level_primary_key_identifiers_fold_to_lowercase_too() -> None:
    sql = """
        CREATE TABLE PedidoItems (
            PedidoId INT,
            ProductoId INT,
            PRIMARY KEY (PedidoId, ProductoId)
        );
    """

    table = _table(parse_ddl(sql), "pedidoitems")

    assert table.primary_key == ["pedidoid", "productoid"]


def test_syntax_error_raises_parse_error_with_line_and_column() -> None:
    sql = "CREATE TABLE t (id INT PRIMARY KEY"  # falta el paréntesis de cierre

    with pytest.raises(ParseError) as exc_info:
        parse_ddl(sql)

    error = exc_info.value
    assert error.line == 1
    assert error.col is not None
    assert "PRIMARY KEY" in error.statement


def test_syntax_error_never_leaks_a_raw_sqlglot_parse_error() -> None:
    sql = "CREATE TABLE t (id INT PRIMARY KEY"

    try:
        parse_ddl(sql)
    except SqlglotParseError:
        pytest.fail("parse_ddl no debe propagar sqlglot.errors.ParseError sin traducir")
    except ParseError:
        pass


def test_foreign_key_is_not_yet_supported_but_table_still_parses_with_a_warning() -> None:
    sql = """
        CREATE TABLE viviendas (
            id SERIAL PRIMARY KEY,
            propietario_id INT NOT NULL REFERENCES clientes(id)
        );
    """

    schema = parse_ddl(sql)

    table = _table(schema, "viviendas")
    assert [c.name for c in table.columns] == ["id", "propietario_id"]
    assert table.columns[1].nullable is False
    assert table.foreign_keys == []
    assert any(
        "viviendas" in warning and "propietario_id" in warning for warning in schema.warnings
    )


def test_table_level_foreign_key_is_reported_as_a_warning_too() -> None:
    sql = """
        CREATE TABLE t (
            a INT,
            FOREIGN KEY (a) REFERENCES other(id)
        );
    """

    schema = parse_ddl(sql)

    table = _table(schema, "t")
    assert table.foreign_keys == []
    assert any("t" in warning for warning in schema.warnings)


def test_check_default_unique_are_not_yet_supported_but_registered_as_warnings() -> None:
    sql = """
        CREATE TABLE t (
            id INT PRIMARY KEY,
            estado TEXT DEFAULT 'activo',
            email TEXT UNIQUE,
            edad INT CHECK (edad > 0)
        );
    """

    schema = parse_ddl(sql)

    table = _table(schema, "t")
    assert table.columns[1].default is None
    assert table.uniques == []
    assert table.checks == []
    assert len(schema.warnings) == 3


def test_multiple_create_table_statements_all_parse() -> None:
    sql = """
        CREATE TABLE a (id INT PRIMARY KEY);
        CREATE TABLE b (id INT PRIMARY KEY, a_id INT REFERENCES a(id));
    """

    schema = parse_ddl(sql)

    assert {t.name for t in schema.tables} == {"a", "b"}


def test_unsupported_top_level_statement_is_a_warning_not_a_crash() -> None:
    sql = "CREATE INDEX idx_t_nombre ON t (nombre);"

    schema = parse_ddl(sql)

    assert schema.tables == []
    assert len(schema.warnings) == 1


def test_dialect_is_recorded_on_the_schema() -> None:
    schema = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY);", dialect="postgres")

    assert schema.dialect == "postgres"


def test_hash_is_left_unset_for_the_hashing_step_to_fill_in() -> None:
    schema = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY);")

    assert schema.hash is None


def test_inmobiliaria_fixture_full_parse_golden_snapshot(snapshot: SnapshotAssertion) -> None:
    """Primer snapshot golden de la IR (T1.3).

    Documenta el estado exacto de esta entrega sobre un fixture real: además
    de columnas/tipos/PK, el propio snapshot deja constancia de los avisos
    por construcciones que las entregas 2-3 todavía no soportan (UNIQUE
    inline y de tabla, CHECK, FK inline vía REFERENCES).
    """
    sql = (_SCHEMAS_DIR / "inmobiliaria.sql").read_text(encoding="utf-8")

    schema = parse_ddl(sql)

    assert schema.model_dump(mode="json", by_alias=True) == snapshot
