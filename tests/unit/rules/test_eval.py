"""Tests del intérprete del mini-DSL (T2.9): evaluate/check y errores de evaluación."""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable

import pytest

from synthdb.generation.context import RowContext
from synthdb.rules import Rule, RuleEvalError, check, evaluate, parse_rule

MakeCtx = Callable[..., RowContext]


def _eval(text: str, make_ctx: MakeCtx, **ctx_kwargs: object) -> object:
    return evaluate(parse_rule(text), make_ctx(**ctx_kwargs))  # type: ignore[arg-type]


# --- Valores y operadores -------------------------------------------------


def test_literals_and_arithmetic(make_ctx: MakeCtx) -> None:
    assert _eval("2 + 3 * 4", make_ctx) == 14
    assert _eval("(2 + 3) * 4", make_ctx) == 20
    assert _eval("7 / 2", make_ctx) == 3.5  # división real
    assert _eval("-5 + 1", make_ctx) == -4


def test_columns_and_comparisons(make_ctx: MakeCtx) -> None:
    assert _eval("superficie * 2", make_ctx, row={"superficie": 21}) == 42
    assert _eval("precio > 100", make_ctx, row={"precio": 150}) is True
    assert _eval("estado = 'activo'", make_ctx, row={"estado": "activo"}) is True


def test_boolean_operators_shortcircuit(make_ctx: MakeCtx) -> None:
    assert _eval("a > 0 and b > 0", make_ctx, row={"a": 1, "b": 2}) is True
    assert _eval("a > 0 or b > 0", make_ctx, row={"a": -1, "b": 2}) is True
    assert _eval("not activo", make_ctx, row={"activo": False}) is True


# --- Referencias: columnas, refs, padre -----------------------------------


def test_ref_resolved_and_unknown(make_ctx: MakeCtx) -> None:
    assert _eval("ref('m2')", make_ctx, refs={"m2": 2350}) == 2350
    with pytest.raises(RuleEvalError, match="ref"):
        _eval("ref('desconocida')", make_ctx, refs={"m2": 2350})


def test_parent_resolved_and_missing(make_ctx: MakeCtx) -> None:
    parents = {"vivienda_id": {"anio_construccion": 2004}}
    assert _eval("parent(vivienda_id).anio_construccion", make_ctx, parents=parents) == 2004
    # Padre inexistente (FK NULL o no inyectada) ⇒ RuleEvalError.
    with pytest.raises(RuleEvalError, match="parent"):
        _eval("parent(vivienda_id).anio_construccion", make_ctx, parents={})
    # Padre presente pero sin esa columna ⇒ RuleEvalError.
    with pytest.raises(RuleEvalError, match="columna"):
        _eval("parent(vivienda_id).superficie", make_ctx, parents=parents)


def test_missing_column_raises(make_ctx: MakeCtx) -> None:
    with pytest.raises(RuleEvalError, match="no está disponible"):
        _eval("superficie * 2", make_ctx, row={})


# --- Errores de dominio ---------------------------------------------------


def test_division_by_zero_is_controlled(make_ctx: MakeCtx) -> None:
    with pytest.raises(RuleEvalError):
        _eval("precio / cero", make_ctx, row={"precio": 10, "cero": 0})


def test_type_incompatibility_raises(make_ctx: MakeCtx) -> None:
    # Comparar orden entre texto y número: Python lanza TypeError, se envuelve.
    with pytest.raises(RuleEvalError):
        _eval("nombre >= 5", make_ctx, row={"nombre": "abc"})


# --- Funciones de la lista blanca -----------------------------------------


def test_date_functions(make_ctx: MakeCtx) -> None:
    assert _eval("date(2020, 3, 15)", make_ctx) == dt.date(2020, 3, 15)
    assert _eval("date_add(f, 30)", make_ctx, row={"f": dt.date(2020, 1, 1)}) == dt.date(
        2020, 1, 31
    )
    row = {"a": dt.date(2020, 6, 1), "b": dt.date(2000, 6, 1)}
    assert _eval("years_between(a, b)", make_ctx, row=row) == 20


def test_round_and_len(make_ctx: MakeCtx) -> None:
    assert _eval("round(3.14159, 2)", make_ctx) == 3.14
    assert _eval("round(3.7)", make_ctx) == 4
    assert _eval("len(nombre)", make_ctx, row={"nombre": "hola"}) == 4
    with pytest.raises(RuleEvalError):
        _eval("len(edad)", make_ctx, row={"edad": 30})  # len sobre un número


def test_noise_is_deterministic_with_fixed_rng(make_ctx: MakeCtx) -> None:
    rule = parse_rule("noise(0.2)")
    a = evaluate(rule, make_ctx(rng=random.Random(123)))
    b = evaluate(rule, make_ctx(rng=random.Random(123)))
    assert a == b  # mismo rng ⇒ mismo ruido (determinismo, CLAUDE.md)
    assert a == pytest.approx(1.0 + random.Random(123).gauss(0.0, 0.2))
    # rng distinto ⇒ ruido distinto.
    assert evaluate(rule, make_ctx(rng=random.Random(999))) != a


def test_noise_rejects_negative_sigma(make_ctx: MakeCtx) -> None:
    with pytest.raises(RuleEvalError):
        _eval("noise(-0.1)", make_ctx)


# --- check(): la regla como aserción --------------------------------------


def test_check_returns_bool(make_ctx: MakeCtx) -> None:
    assert check(parse_rule("precio > 0"), make_ctx(row={"precio": 5})) is True
    assert check(parse_rule("precio > 0"), make_ctx(row={"precio": -5})) is False


def test_check_rejects_non_boolean_rule(make_ctx: MakeCtx) -> None:
    with pytest.raises(RuleEvalError, match="booleano"):
        check(parse_rule("superficie + 1"), make_ctx(row={"superficie": 3}))


def test_derivation_double_use_as_assertion(make_ctx: MakeCtx) -> None:
    # Una derivación determinista (sin noise) se re-evalúa como aserción (§7.2):
    # tras escribir la columna, `col = expr` debe cumplirse.
    rule = parse_rule("precio = superficie * ref('m2')")
    ctx = make_ctx(row={"superficie": 10, "precio": 235}, refs={"m2": 23.5})
    assert check(rule, ctx) is True
    bad = make_ctx(row={"superficie": 10, "precio": 999}, refs={"m2": 23.5})
    assert check(rule, bad) is False


def test_eval_error_names_rule_and_row(make_ctx: MakeCtx) -> None:
    rule: Rule = parse_rule("superficie * 2")
    with pytest.raises(RuleEvalError) as excinfo:
        evaluate(rule, make_ctx(row={"otra": 1}))
    message = str(excinfo.value)
    assert "superficie * 2" in message  # la regla
    assert "otra" in message  # un extracto de la fila
