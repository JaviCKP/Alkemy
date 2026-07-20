"""Integración del mini-DSL sobre el ejemplo trabajado (§16) con filas de juguete.

Las reglas del dominio inmobiliaria de §16, escritas en el mini-DSL real, deben
parsear, clasificarse y evaluarse sobre filas construidas a mano. Es la prueba de
que las tres piezas (parser, clasificador, intérprete) encajan sobre el caso que la
especificación usa como hilo conductor.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable

import pytest

from synthdb.generation.context import RowContext
from synthdb.rules import as_bound, as_derivation, check, clasify_rule, evaluate, parse_rule

MakeCtx = Callable[..., RowContext]

TEMPORAL = "fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)"
DERIVATION = "precio = parent(vivienda_id).superficie_m2 * ref('precio_m2_base') * noise(0.2)"

# Vivienda 3 del ejemplo (§16): chalet de 215 m² construido en 2004.
PARENTS = {"vivienda_id": {"anio_construccion": 2004, "superficie_m2": 215.0}}
REFS = {"precio_m2_base": 2350}


def test_worked_example_rules_classify(make_ctx: MakeCtx) -> None:
    assert clasify_rule(parse_rule(TEMPORAL)) == "bound"
    assert clasify_rule(parse_rule(DERIVATION)) == "derivation"


def test_temporal_rule_as_bound_value(make_ctx: MakeCtx) -> None:
    bound = as_bound(parse_rule(TEMPORAL))
    assert bound is not None
    assert (bound.column, bound.side, bound.exclusive) == ("fecha", "lower", False)
    # La cota inferior de `fecha` es el 1 de enero del año de construcción del padre.
    ctx = make_ctx(parents=PARENTS)
    assert evaluate(bound.expr, ctx) == dt.date(2004, 1, 1)


def test_temporal_rule_double_use_as_assertion(make_ctx: MakeCtx) -> None:
    rule = parse_rule(TEMPORAL)
    # Una compraventa de 2011 sobre una vivienda de 2004 cumple la regla...
    assert check(rule, make_ctx(row={"fecha": dt.date(2011, 6, 19)}, parents=PARENTS)) is True
    # ...pero una anterior a la construcción, no.
    assert check(rule, make_ctx(row={"fecha": dt.date(2003, 1, 1)}, parents=PARENTS)) is False


def test_derivation_value_is_deterministic(make_ctx: MakeCtx) -> None:
    derivation = as_derivation(parse_rule(DERIVATION))
    assert derivation is not None and derivation.column == "precio"
    seed = 42
    ctx = make_ctx(parents=PARENTS, refs=REFS, rng=random.Random(seed))
    precio = evaluate(derivation.expr, ctx)
    # 215 m² · 2350 €/m² · (1 + ruido): reproducible con el mismo rng.
    expected = 215.0 * 2350 * (1.0 + random.Random(seed).gauss(0.0, 0.2))
    assert precio == pytest.approx(expected)
