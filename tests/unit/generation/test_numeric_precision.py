"""Respeto de la semántica de NUMERIC(precision, scale) (revisión sesión E, hallazgo 1).

Cubre las tres capas: el módulo puro `numeric_bounds`, los generadores
(`numeric_range` y `fallback`) que producen valores representables y redondeados a
la escala, la validación estructural que rechaza un desbordamiento de precisión, y
la compilación que convierte un rango imposible en un `PlanError` accionable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from synthdb.config.models import ColumnConfig, Config, OutputConfig, TableConfig
from synthdb.generation import engine
from synthdb.generation.engine import PlanError, generate_dataset
from synthdb.generation.generators import resolve
from synthdb.generation.numeric_bounds import (
    fits,
    quantize_to_scale,
    representable_limit,
    scale_step,
)
from synthdb.ir.schema import GeneratorSpec
from synthdb.parsing.ddl import parse_ddl


def num(**params: Any):
    return resolve(GeneratorSpec(type="numeric_range", params=params))


def _decimal_places(value: float) -> int:
    return max(0, -Decimal(str(value)).as_tuple().exponent)


# --- Módulo puro numeric_bounds ------------------------------------------------


def test_representable_limit_and_scale_step_are_exact() -> None:
    assert representable_limit(3, 2) == Decimal("9.99")
    assert representable_limit(5, 0) == Decimal("99999")
    assert representable_limit(12, 4) == Decimal("99999999.9999")
    assert scale_step(2) == Decimal("0.01")
    assert scale_step(None) == Decimal("1")  # NUMERIC(p) ⇒ escala 0


def test_fits_rounds_to_scale_then_checks_precision() -> None:
    assert fits(Decimal("9.99"), 3, 2)
    assert fits(-9.99, 3, 2)
    assert fits(9.994, 3, 2)  # redondea a 9.99, cabe
    assert not fits(9.995, 3, 2)  # redondea a 10.00, desborda
    assert not fits(10.0, 3, 2)
    assert not fits(100.0, 3, 2)  # la reproducción del hallazgo
    assert not fits(-100.0, 3, 2)


def test_quantize_to_scale_is_banker_rounding_without_binary_noise() -> None:
    assert quantize_to_scale(0.3749, 2) == Decimal("0.37")
    assert quantize_to_scale(0.375, 2) == Decimal("0.38")  # medio a par (7 impar ⇒ sube)
    assert quantize_to_scale(0.385, 2) == Decimal("0.38")  # medio a par (8 par ⇒ queda)
    assert quantize_to_scale(0.1, 2) == Decimal("0.10")  # 0.1 no arrastra 0.1000000...


# --- Generador numeric_range ---------------------------------------------------


def test_numeric_range_quantizes_to_declared_scale(sample, make_column) -> None:
    col = make_column("numeric", precision=6, scale=2)
    vals = sample(num(min=0, max=100), 300, column=col)
    assert all(isinstance(v, float) for v in vals)
    assert all(_decimal_places(v) <= 2 for v in vals)
    assert all(fits(v, 6, 2) for v in vals)


def test_numeric_range_clamps_request_to_the_representable_window(sample, make_column) -> None:
    col = make_column("numeric", precision=3, scale=2)  # ±9.99
    # El rango pedido excede al tipo; el generador lo recorta, no desborda.
    vals = sample(num(min=-1000, max=1000), 500, column=col)
    assert all(Decimal("-9.99") <= Decimal(str(v)) <= Decimal("9.99") for v in vals)
    assert all(fits(v, 3, 2) for v in vals)


def test_numeric_range_reaches_the_representable_extremes(sample, make_column) -> None:
    col = make_column("numeric", precision=3, scale=2)
    assert sample(num(min=9.99, max=9.99), 4, column=col) == [9.99] * 4
    assert sample(num(min=-9.99, max=-9.99), 4, column=col) == [-9.99] * 4


def test_numeric_range_without_precision_is_unchanged(sample, make_column) -> None:
    # double precision (sin precision) sigue devolviendo floats sin cuantizar.
    col = make_column("numeric")
    vals = sample(num(min=1.5, max=2.5), 100, column=col)
    assert all(isinstance(v, float) for v in vals)
    assert all(1.5 <= v <= 2.5 for v in vals)


# --- Validación estructural y compilación (end to end) -------------------------


def test_numeric_array_elements_are_all_representable() -> None:
    spec = parse_ddl("CREATE TABLE m (id SERIAL PRIMARY KEY, amounts NUMERIC(4, 2)[] NOT NULL);")
    dataset = generate_dataset(spec, Config(seed=5, tables={"m": TableConfig(rows=30)}))
    assert dataset.quarantine == {}
    seen_nonempty = False
    for row in dataset.tables["m"]:
        assert isinstance(row["amounts"], list)
        for value in row["amounts"]:
            seen_nonempty = True
            assert fits(value, 4, 2)
            assert _decimal_places(value) <= 2
    assert seen_nonempty  # los arrays no son todos vacíos


def test_numeric_range_out_of_representable_window_is_a_plan_error() -> None:
    # Reproducción NUMERIC(3,2) con [100, 100]: ningún valor cabe ⇒ PlanError.
    spec = parse_ddl("CREATE TABLE amounts (id SERIAL PRIMARY KEY, amount NUMERIC(3, 2) NOT NULL);")
    config = Config(
        tables={
            "amounts": TableConfig(
                rows=3,
                columns={
                    "amount": ColumnConfig(
                        generator="numeric_range", params={"min": 100, "max": 100}
                    )
                },
            )
        }
    )
    with pytest.raises(PlanError, match=r"columna amount.*NUMERIC\(3, 2\).*9\.99"):
        generate_dataset(spec, config)


def test_numeric_overflow_value_is_quarantined_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Si un valor no representable llega a validarse (aquí inyectado), se cuarentena
    # en vez de aceptarse: NUMERIC(3,2) no puede almacenar 100.0.
    spec = parse_ddl("CREATE TABLE amounts (id SERIAL PRIMARY KEY, amount NUMERIC(3, 2) NOT NULL);")

    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            row["amount"] = 100.0

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    dataset = generate_dataset(
        spec,
        Config(seed=0, tables={"amounts": TableConfig(rows=3)}, output=OutputConfig(batch_size=5)),
    )
    assert dataset.tables["amounts"] == []
    assert len(dataset.quarantine["amounts"]) == 3
    assert "NUMERIC(3, 2)" in dataset.quarantine["amounts"][0][2]
