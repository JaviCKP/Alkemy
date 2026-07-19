"""Tests del parser del mini-DSL (T2.9): gramática admitida y batería de rechazo.

La batería de rechazo es la parte de seguridad: cada línea maliciosa o fuera de la
gramática debe producir `RuleParseError` en compilación, JAMÁS ejecutarse ni pasar
un nodo de sqlglot sin traducir (CLAUDE.md prohíbe eval/exec; esta es la barrera).
"""

from __future__ import annotations

import pytest

from synthdb.rules import parse_rule
from synthdb.rules.dsl import (
    Arith,
    BoolOp,
    Call,
    Col,
    Compare,
    Const,
    Neg,
    Not,
    ParentCol,
    Ref,
    RuleParseError,
)

# --- Gramática admitida ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", Const(42)),
        ("3.5", Const(3.5)),
        ("-7", Const(-7)),
        ("'piso'", Const("piso")),
        ("true", Const(True)),
        ("false", Const(False)),
        ("null", Const(None)),
        ("superficie", Col("superficie")),
        ("ref('precio_m2_base')", Ref("precio_m2_base")),
        ("parent(vivienda_id).anio_construccion", ParentCol("vivienda_id", "anio_construccion")),
    ],
)
def test_atoms_parse(text: str, expected: object) -> None:
    assert parse_rule(text).root == expected


@pytest.mark.parametrize("op", ["=", "<>", "<", "<=", ">", ">="])
def test_all_comparisons_parse(op: str) -> None:
    root = parse_rule(f"a {op} b").root
    assert root == Compare(op, Col("a"), Col("b"))


@pytest.mark.parametrize("op", ["+", "-", "*", "/"])
def test_all_arithmetic_parse(op: str) -> None:
    root = parse_rule(f"a {op} b").root
    assert root == Arith(op, Col("a"), Col("b"))


def test_boolean_operators_parse() -> None:
    assert parse_rule("not activo").root == Not(Col("activo"))
    assert parse_rule("a and b").root == BoolOp("and", Col("a"), Col("b"))
    assert parse_rule("a or b").root == BoolOp("or", Col("a"), Col("b"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("date(2020, 1, 1)", Call("date", (Const(2020), Const(1), Const(1)))),
        ("date_add(fecha, 30)", Call("date_add", (Col("fecha"), Const(30)))),
        ("years_between(a, b)", Call("years_between", (Col("a"), Col("b")))),
        ("noise(0.2)", Call("noise", (Const(0.2),))),
        ("round(x)", Call("round", (Col("x"),))),
        ("round(x, 2)", Call("round", (Col("x"), Const(2)))),
        ("len(nombre)", Call("len", (Col("nombre"),))),
    ],
)
def test_whitelisted_functions_parse(text: str, expected: object) -> None:
    assert parse_rule(text).root == expected


def test_precedence_and_nesting() -> None:
    # (a + b) * c  →  Mul(Paren(Add), c): el paréntesis se despliega.
    assert parse_rule("(a + b) * c").root == Arith("*", Arith("+", Col("a"), Col("b")), Col("c"))
    # Anidamiento profundo: date(parent(fk).anio, 1, 1)
    assert parse_rule("date(parent(vivienda_id).anio_construccion, 1, 1)").root == Call(
        "date", (ParentCol("vivienda_id", "anio_construccion"), Const(1), Const(1))
    )
    # noise dentro de una cadena aritmética
    assert parse_rule("superficie * ref('m2') * noise(0.2)").root == Arith(
        "*",
        Arith("*", Col("superficie"), Ref("m2")),
        Call("noise", (Const(0.2),)),
    )


def test_unary_minus_on_expression_is_neg_not_const() -> None:
    # -(a) no es un literal, así que se conserva como Neg; -5 sí se pliega a Const.
    assert parse_rule("-(a)").root == Neg(Col("a"))
    assert parse_rule("-5").root == Const(-5)


# --- Batería de rechazo (una por construcción prohibida) ------------------

REJECTED = {
    "dunder_import": "__import__('os')",
    "getattr": "getattr(x, 5)",
    "subscript": "arr[0]",
    "attribute_chain": "x.y.z",
    "qualified_column": "t.col",
    "function_upper": "upper(nombre)",
    "function_concat": "concat(a, b)",
    "function_system": "system('x')",
    "like": "nombre like 'A%'",
    "sql_comment": "precio > 0 -- malicioso",
    "block_comment": "precio /* x */ > 0",
    "semicolon": "x = 1; y = 2",
    "subquery": "vivienda_id in (select id from viviendas)",
    "concat_operator": "a || b",
    "power_operator": "a ^ b",
    "modulo_operator": "a % b",
    "cast": "cast(x as int)",
    "case": "case when a then b else c end",
    "between": "x between 1 and 2",
    "in_list": "tipo in ('piso', 'chalet')",
    "is_null": "col is null",
    "fstring_like": "f'{x}'",
    "star": "count(*)",
    "empty": "   ",
}


@pytest.mark.parametrize("text", list(REJECTED.values()), ids=list(REJECTED))
def test_rejection_battery(text: str) -> None:
    with pytest.raises(RuleParseError):
        parse_rule(text)


def test_aggregate_rejection_mentions_v1() -> None:
    # sum-like sobre el grupo de hijos es de la v1.0 (sum_over_group), no del MVP.
    with pytest.raises(RuleParseError, match=r"v1\.0"):
        parse_rule("sum(importe) = parent(compraventa_id).precio")


@pytest.mark.parametrize(
    "text",
    [
        "date(2020, 1)",  # faltan argumentos
        "date(2020, 1, 1, 1)",  # sobran
        "noise()",  # noise necesita sigma
        "noise(0.1, 0.2)",  # noise es unario
        "round(x, 2, 3)",  # el 3.er argumento (truncate) no es del DSL
        "len(a, b)",  # len es unario
        "date_add(x, 1, 2)",  # date_add no lleva 'unit'
    ],
)
def test_arity_and_extra_args_rejected(text: str) -> None:
    with pytest.raises(RuleParseError):
        parse_rule(text)


@pytest.mark.parametrize(
    "text",
    [
        "ref()",  # ref necesita un nombre
        "ref(columna)",  # el nombre debe ser una cadena, no una columna
        "ref('a', 'b')",  # ref es unario
        "parent(vivienda_id)",  # parent sin acceso a columna
        "parent('vivienda_id').x",  # el argumento de parent es una columna, no cadena
        "foo(x).y",  # el único acceso con punto es parent(...).col
    ],
)
def test_parent_and_ref_shape_rejected(text: str) -> None:
    with pytest.raises(RuleParseError):
        parse_rule(text)
