"""Tests de RowContext, orden de columnas intra-fila y generador `derived` (T2.10)."""

from __future__ import annotations

import random

import pytest

from synthdb.generation.context import (
    PlanError,
    RowContext,
    build_column_order,
    mapping_resolver,
)
from synthdb.generation.generators import GenContext, registered_names, resolve
from synthdb.generation.generators.base import GenContext as BaseGenContext
from synthdb.ir.plans import ColumnPlan, TablePlan
from synthdb.ir.schema import ColumnSpec, GeneratorSpec, TypeSpec
from synthdb.rules import parse_rule


def _table_plan(*columns: str) -> TablePlan:
    return TablePlan(
        table="t",
        columns=[ColumnPlan(column=name, source="fallback", confidence=0.0) for name in columns],
    )


def _order(columns: list[str], rules: list[str]) -> list[str]:
    return build_column_order(_table_plan(*columns), [parse_rule(r) for r in rules])


def _row_ctx(**kwargs: object) -> RowContext:
    return RowContext(
        rng=random.Random(0),
        column=ColumnSpec(name="x", type=TypeSpec(kind="numeric"), nullable=True),
        table="t",
        **kwargs,  # type: ignore[arg-type]
    )


# --- RowContext -----------------------------------------------------------


def test_rowcontext_is_a_gencontext() -> None:
    # Un RowContext ES un GenContext: los generadores de la sesión A lo aceptan sin
    # cambiar de firma.
    ctx = _row_ctx()
    assert isinstance(ctx, BaseGenContext)


def test_rowcontext_parent_row_and_refs() -> None:
    ctx = _row_ctx(
        row={"a": 1},
        refs={"m2": 2350},
        resolve_parent=mapping_resolver({"vivienda_id": {"anio_construccion": 2004}}),
    )
    assert ctx.row["a"] == 1
    assert ctx.refs["m2"] == 2350
    assert ctx.parent("vivienda_id") == {"anio_construccion": 2004}


def test_rowcontext_parent_defaults_to_none() -> None:
    # Sin resolutor inyectado, no hay padres.
    assert _row_ctx().parent("cualquier_fk") is None
    # Y una FK ausente del mapa también da None.
    assert _row_ctx(resolve_parent=mapping_resolver({})).parent("otra") is None


# --- build_column_order ---------------------------------------------------


def test_chain_a_b_c_is_ordered() -> None:
    # Criterio de aceptación T2.10: la cadena a→b→c se genera en orden.
    order = _order(["c", "b", "a"], ["b = a + 1", "c = b * 2"])
    assert order == ["a", "b", "c"]


def test_bound_rule_also_creates_order() -> None:
    # `fin >= inicio` acota `fin` leyendo `inicio`: inicio antes que fin.
    order = _order(["fin", "inicio"], ["fin >= inicio"])
    assert order.index("inicio") < order.index("fin")


def test_independent_columns_break_ties_alphabetically() -> None:
    assert _order(["zeta", "alfa", "mu"], []) == ["alfa", "mu", "zeta"]


def test_order_is_deterministic_regardless_of_input_order() -> None:
    rules = ["b = a + 1", "c = b + 1", "d = a + 1"]
    first = _order(["a", "b", "c", "d"], rules)
    scrambled = _order(["d", "c", "b", "a"], list(reversed(rules)))
    assert first == scrambled == ["a", "b", "c", "d"]


def test_assertions_and_unknown_columns_do_not_constrain_order() -> None:
    # Una aserción (a+b=c+d) no impone orden; una regla que lee una columna ajena a
    # la tabla no añade arista (su existencia se valida al evaluar, no aquí).
    order = _order(["a", "b"], ["a + b = 10", "a = fantasma + 1"])
    assert order == ["a", "b"]


def test_cycle_raises_plan_error_naming_the_cycle() -> None:
    with pytest.raises(PlanError) as excinfo:
        _order(["a", "b"], ["a = b + 1", "b = a + 1"])
    message = str(excinfo.value)
    assert "a" in message and "b" in message
    assert "ciclo" in message.lower()


def test_three_column_cycle_detected() -> None:
    with pytest.raises(PlanError):
        _order(["a", "b", "c"], ["a = c + 1", "b = a + 1", "c = b + 1"])


# --- Generador derived ----------------------------------------------------


def test_derived_is_registered() -> None:
    assert "derived" in registered_names()


def test_derived_generator_evaluates_expression() -> None:
    generator = resolve(
        GeneratorSpec(type="derived", params={"expression": "superficie * ref('m2')"})
    )
    ctx = _row_ctx(row={"superficie": 10}, refs={"m2": 23.5})
    assert generator.generate(ctx) == pytest.approx(235.0)


def test_derived_generator_requires_row_context() -> None:
    generator = resolve(GeneratorSpec(type="derived", params={"expression": "1 + 1"}))
    plain = GenContext(
        rng=random.Random(0),
        column=ColumnSpec(name="x", type=TypeSpec(kind="integer"), nullable=True),
        table="t",
    )
    with pytest.raises(TypeError, match="RowContext"):
        generator.generate(plain)


def test_derived_generator_parse_error_surfaces_at_build() -> None:
    from synthdb.rules import RuleParseError

    with pytest.raises(RuleParseError):
        resolve(GeneratorSpec(type="derived", params={"expression": "upper(nombre)"}))
