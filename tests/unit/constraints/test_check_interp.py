"""Tests de src/synthdb/constraints/check_interp.py (T1.4, especificacion.md §5 y §7.5).

`interpret_checks` recibe la `SchemaSpec` ya parseada por `parsing/ddl.py`
(T1.3, con `ast_supported=False` y `bounds_derived=None` en todos sus
`CheckSpec`) y devuelve una copia con los que entiende ya interpretados.
Todos los tests pasan por el `parse_ddl` real en vez de construir `CheckSpec`
a mano: lo que se ejercita es el subconjunto de AST que `check_interp.py`
sabe leer, no un formato inventado por el propio test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthdb.constraints.check_interp import interpret_checks
from synthdb.ir.hashing import schema_hash
from synthdb.ir.schema import CheckSpec, SchemaSpec, TableSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _table(schema: SchemaSpec, name: str) -> TableSpec:
    for table in schema.tables:
        if table.name == name:
            return table
    raise AssertionError(f"tabla {name!r} no encontrada en {[t.name for t in schema.tables]}")


def _check(predicate_sql: str) -> CheckSpec:
    """`CheckSpec` de tabla, de una única columna `x`, ya interpretado."""
    schema = parse_ddl(f"CREATE TABLE t (x INT, CHECK ({predicate_sql}));")
    return interpret_checks(schema).tables[0].checks[0]


# --- Comparaciones (col op literal, y su forma invertida literal op col) ----


@pytest.mark.parametrize(
    "predicate,expected_bounds",
    [
        ("x > 5", {"min": 5, "min_exclusive": True}),
        ("x >= 5", {"min": 5, "min_exclusive": False}),
        ("x < 5", {"max": 5, "max_exclusive": True}),
        ("x <= 5", {"max": 5, "max_exclusive": False}),
        ("x = 5", {"equals": 5}),
        ("x <> 5", {"excluded_values": [5]}),
        ("x != 5", {"excluded_values": [5]}),
        ("5 < x", {"min": 5, "min_exclusive": True}),
        ("5 <= x", {"min": 5, "min_exclusive": False}),
        ("5 > x", {"max": 5, "max_exclusive": True}),
        ("5 >= x", {"max": 5, "max_exclusive": False}),
        ("5 = x", {"equals": 5}),
        ("5 <> x", {"excluded_values": [5]}),
    ],
)
def test_comparison_operators_and_their_flipped_form(
    predicate: str, expected_bounds: dict[str, object]
) -> None:
    check = _check(predicate)

    assert check.ast_supported is True
    assert check.bounds_derived == expected_bounds


def test_negative_literal_is_a_valid_bound() -> None:
    check = _check("x > -100")

    assert check.bounds_derived == {"min": -100, "min_exclusive": True}


def test_float_literal_is_preserved_as_float() -> None:
    check = _check("x > 0.5")

    assert check.bounds_derived == {"min": 0.5, "min_exclusive": True}


def test_boolean_literal_equals() -> None:
    check = _check("x = true")

    assert check.bounds_derived == {"equals": True}


def test_date_like_string_literal_is_kept_as_iso_string() -> None:
    check = _check("x > '2020-01-01'")

    assert check.bounds_derived == {"min": "2020-01-01", "min_exclusive": True}


# --- BETWEEN -----------------------------------------------------------------


def test_between_produces_inclusive_min_and_max() -> None:
    check = _check("x BETWEEN 1900 AND 2026")

    assert check.ast_supported is True
    assert check.bounds_derived == {
        "min": 1900,
        "min_exclusive": False,
        "max": 2026,
        "max_exclusive": False,
    }


def test_between_with_string_dates() -> None:
    check = _check("x BETWEEN '2020-01-01' AND '2021-01-01'")

    assert check.bounds_derived == {
        "min": "2020-01-01",
        "min_exclusive": False,
        "max": "2021-01-01",
        "max_exclusive": False,
    }


# --- IN / NOT IN --------------------------------------------------------------


def test_in_with_string_literals_produces_values() -> None:
    check = _check("x IN ('piso', 'chalet', 'adosado')")

    assert check.ast_supported is True
    assert check.bounds_derived == {"values": ["piso", "chalet", "adosado"]}


def test_in_with_numeric_literals_produces_values() -> None:
    check = _check("x IN (1, 2, 3)")

    assert check.ast_supported is True
    assert check.bounds_derived == {"values": [1, 2, 3]}


def test_not_in_produces_excluded_values() -> None:
    check = _check("x NOT IN (1, 2, 3)")

    assert check.ast_supported is True
    assert check.bounds_derived == {"excluded_values": [1, 2, 3]}


def test_not_in_with_explicit_parentheses_around_in() -> None:
    # "NOT (x IN (...))" y "x NOT IN (...)" son el mismo AST para sqlglot
    # (un Not envolviendo un In, con un Paren de por medio); ambas formas
    # deben interpretarse igual.
    check = _check("NOT (x IN (1, 2))")

    assert check.ast_supported is True
    assert check.bounds_derived == {"excluded_values": [1, 2]}


# --- NOT sobre una comparación -----------------------------------------------


@pytest.mark.parametrize(
    "predicate,expected_bounds",
    [
        ("NOT x > 5", {"max": 5, "max_exclusive": False}),
        ("NOT x >= 5", {"max": 5, "max_exclusive": True}),
        ("NOT x < 5", {"min": 5, "min_exclusive": False}),
        ("NOT x <= 5", {"min": 5, "min_exclusive": True}),
        ("NOT x = 5", {"excluded_values": [5]}),
        ("NOT x <> 5", {"equals": 5}),
    ],
)
def test_not_of_comparison_inverts_the_operator(
    predicate: str, expected_bounds: dict[str, object]
) -> None:
    check = _check(predicate)

    assert check.ast_supported is True
    assert check.bounds_derived == expected_bounds


# --- AND: intersección de cotas ----------------------------------------------


def test_and_of_two_bounds_intersects_to_the_tighter_range() -> None:
    check = _check("x > 0 AND x <= 100")

    assert check.ast_supported is True
    assert check.bounds_derived == {
        "min": 0,
        "min_exclusive": True,
        "max": 100,
        "max_exclusive": False,
    }


def test_chained_and_of_three_clauses_intersects_all() -> None:
    check = _check("x > 0 AND x < 100 AND x <> 50")

    assert check.ast_supported is True
    assert check.bounds_derived == {
        "min": 0,
        "min_exclusive": True,
        "max": 100,
        "max_exclusive": True,
        "excluded_values": [50],
    }


def test_and_with_empty_intersection_is_supported_but_warns() -> None:
    schema = parse_ddl("CREATE TABLE t (x INT, CHECK (x > 5 AND x < 3));")

    result = interpret_checks(schema)

    check = result.tables[0].checks[0]
    assert check.ast_supported is True
    assert check.bounds_derived == {
        "min": 5,
        "min_exclusive": True,
        "max": 3,
        "max_exclusive": True,
    }
    assert any(
        "t" in warning and "x" in warning and "CHECK" in warning for warning in result.warnings
    )


def test_unsatisfiable_and_does_not_raise_and_does_not_relax_the_constraint() -> None:
    # Principio de CLAUDE.md: las contradicciones se detectan y avisan, nunca
    # se "arreglan" relajando la restricción ni lanzando una excepción.
    schema = parse_ddl("CREATE TABLE t (x INT, CHECK (x = 5 AND x = 6));")

    result = interpret_checks(schema)

    check = result.tables[0].checks[0]
    assert check.ast_supported is True
    assert len(result.warnings) == 1


# --- No soportado (silencioso, sin aviso nuevo) -------------------------------


def test_or_is_not_supported() -> None:
    check = _check("x < 3 OR x > 9")

    assert check.ast_supported is False
    assert check.bounds_derived is None


def test_or_nested_inside_and_is_not_supported() -> None:
    check = _check("(x < 3 OR x > 9) AND x <> 0")

    assert check.ast_supported is False
    assert check.bounds_derived is None


def test_multi_column_predicate_is_not_supported() -> None:
    schema = parse_ddl("CREATE TABLE t (a INT, b INT, CHECK (a < b));")

    check = interpret_checks(schema).tables[0].checks[0]

    assert check.ast_supported is False
    assert check.bounds_derived is None


def test_and_across_two_different_columns_is_not_supported() -> None:
    schema = parse_ddl("CREATE TABLE t (a INT, b INT, CHECK (a > 0 AND b > 0));")

    check = interpret_checks(schema).tables[0].checks[0]

    assert check.ast_supported is False


def test_function_call_is_not_supported() -> None:
    check = _check("length(x) > 3")

    assert check.ast_supported is False
    assert check.bounds_derived is None


def test_function_call_on_both_sides_of_equality_is_not_supported() -> None:
    check = _check("upper(x) = 'ABC'")

    assert check.ast_supported is False


def test_like_is_not_supported() -> None:
    # Pospuesto por completo (ver docstring de check_interp.py): aporta poco
    # como cota de generación frente a su complejidad.
    check = _check("x LIKE 'foo%'")

    assert check.ast_supported is False
    assert check.bounds_derived is None


def test_subquery_in_in_list_is_not_supported() -> None:
    check = _check("x IN (SELECT id FROM other)")

    assert check.ast_supported is False


def test_not_of_between_is_not_supported() -> None:
    # El subconjunto solo invierte "comparación o IN" (especificación de la
    # tarea); NOT sobre BETWEEN cae fuera a propósito.
    check = _check("NOT (x BETWEEN 1 AND 10)")

    assert check.ast_supported is False


def test_column_compared_to_column_is_not_supported() -> None:
    # Aun siendo la misma columna en ambos lados, no es "col op literal".
    check = _check("x > x")

    assert check.ast_supported is False


# --- Ambos orígenes: columna y tabla -----------------------------------------


def test_column_level_check_is_interpreted_too() -> None:
    schema = parse_ddl("CREATE TABLE t (x INT CHECK (x > 0));")

    result = interpret_checks(schema)

    check = result.tables[0].columns[0].checks[0]
    assert check.ast_supported is True
    assert check.bounds_derived == {"min": 0, "min_exclusive": True}


def test_table_level_check_is_interpreted() -> None:
    schema = parse_ddl("CREATE TABLE t (x INT, CHECK (x > 0));")

    result = interpret_checks(schema)

    check = result.tables[0].checks[0]
    assert check.ast_supported is True
    assert check.bounds_derived == {"min": 0, "min_exclusive": True}


def test_unrelated_columns_and_checks_are_left_untouched() -> None:
    schema = parse_ddl("CREATE TABLE t (x INT CHECK (x > 0), y TEXT NOT NULL, z INT);")

    result = interpret_checks(schema)

    table = result.tables[0]
    assert table.columns[1].name == "y"
    assert table.columns[1].checks == []
    assert table.columns[2].name == "z"
    assert table.columns[2].checks == []


# --- Fixture real: inmobiliaria.sql ------------------------------------------


def test_inmobiliaria_fixture_checks_get_the_exact_bounds_from_the_ddl() -> None:
    sql = (_SCHEMAS_DIR / "inmobiliaria.sql").read_text(encoding="utf-8")
    schema = parse_ddl(sql)

    result = interpret_checks(schema)

    viviendas = _table(result, "viviendas")
    tipo = next(c for c in viviendas.columns if c.name == "tipo")
    superficie = next(c for c in viviendas.columns if c.name == "superficie_m2")
    anio = next(c for c in viviendas.columns if c.name == "anio_construccion")

    assert tipo.checks[0].ast_supported is True
    assert tipo.checks[0].bounds_derived == {"values": ["piso", "chalet", "adosado"]}

    assert superficie.checks[0].ast_supported is True
    assert superficie.checks[0].bounds_derived == {"min": 0, "min_exclusive": True}

    assert anio.checks[0].ast_supported is True
    assert anio.checks[0].bounds_derived == {
        "min": 1900,
        "min_exclusive": False,
        "max": 2026,
        "max_exclusive": False,
    }

    compraventas = _table(result, "compraventas")
    precio = next(c for c in compraventas.columns if c.name == "precio")
    assert precio.checks[0].bounds_derived == {"min": 0, "min_exclusive": True}

    pagos = _table(result, "pagos")
    importe = next(c for c in pagos.columns if c.name == "importe")
    assert importe.checks[0].bounds_derived == {"min": 0, "min_exclusive": True}

    # Ninguno de los 5 CHECK de este fixture es insatisfacible: no hay avisos
    # nuevos más allá de los que ya trajera el propio parseo (ninguno, T1.3).
    assert result.warnings == []


# --- Invariantes de hash e IR -------------------------------------------------


def test_schema_hash_is_unchanged_by_interpreting_checks() -> None:
    sql = (_SCHEMAS_DIR / "inmobiliaria.sql").read_text(encoding="utf-8")
    schema = parse_ddl(sql)

    assert schema_hash(schema) == schema_hash(interpret_checks(schema))


def test_original_schema_spec_is_not_mutated_in_place() -> None:
    schema = parse_ddl("CREATE TABLE t (x INT, CHECK (x > 0));")

    interpret_checks(schema)

    original_check = schema.tables[0].checks[0]
    assert original_check.ast_supported is False
    assert original_check.bounds_derived is None
