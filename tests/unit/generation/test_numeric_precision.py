"""Respeto de la semántica de NUMERIC(precision, scale) (revisión sesión E, hallazgos 1 y 2).

Cubre las tres capas: el módulo puro `numeric_bounds`, los generadores
(`numeric_range` y `fallback`) que producen valores representables y redondeados a
la escala, la validación estructural que rechaza un desbordamiento de precisión, y
la compilación que convierte un rango imposible —incluido uno vacío solo por la
rejilla de la escala o por una exclusividad, hallazgo 2— en un `PlanError` accionable.
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
    has_quantized_value,
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


def test_quantize_to_scale_rounds_ties_away_from_zero_like_postgresql() -> None:
    # PostgreSQL NUMERIC redondea los empates alejándose de cero, no medio-a-par
    # (revisión sesión E, hallazgo 1): ROUND_HALF_UP, no ROUND_HALF_EVEN.
    assert quantize_to_scale(0.3749, 2) == Decimal("0.37")  # no es empate: sin cambios
    assert quantize_to_scale(0.375, 2) == Decimal("0.38")  # empate ⇒ se aleja de cero
    assert quantize_to_scale(0.385, 2) == Decimal("0.39")  # empate ⇒ se aleja de cero
    assert quantize_to_scale(-0.385, 2) == Decimal("-0.39")  # simétrico en negativo
    assert quantize_to_scale(0.1, 2) == Decimal("0.10")  # 0.1 no arrastra 0.1000000...


def test_quantize_to_scale_overflow_after_rounding_the_tie() -> None:
    # 9.995 es un empate en la tercera cifra: se aleja de cero a 10.00, que ya
    # no cabe en NUMERIC(3, 2) aunque 9.995 "pareciera" caber antes de redondear.
    assert quantize_to_scale(9.995, 2) == Decimal("10.00")
    assert not fits(9.995, 3, 2)
    assert quantize_to_scale(-9.995, 2) == Decimal("-10.00")
    assert not fits(-9.995, 3, 2)


@pytest.mark.parametrize(
    ("precision", "scale"),
    [(29, 0), (100, 0), (1000, 0), (50, 10), (1000, 500)],
)
def test_representable_limit_and_fits_are_exact_for_large_precision(
    precision: int, scale: int
) -> None:
    # representable_limit/quantize_to_scale/fits no pueden depender de la
    # precisión ambiente de Decimal (28 dígitos por defecto): deben ser exactos
    # y no reventar con InvalidOperation ni redondear el propio límite, incluso
    # para NUMERIC(1000, 500) (revisión sesión E, hallazgo 1).
    limit = representable_limit(precision, scale)
    assert len(limit.as_tuple().digits) == precision  # nunca truncado a 28
    assert str(limit).lstrip("-").replace(".", "") == "9" * precision
    assert fits(limit, precision, scale)
    assert fits(-limit, precision, scale)
    # Un valor de magnitud mayor, aunque con pocos dígitos ALMACENADOS (coeficiente
    # corto + exponente grande), debe seguir desbordando sin reventar.
    oversized = Decimal(f"2E+{precision + 5}")
    assert not fits(oversized, precision, scale)
    assert not fits(-oversized, precision, scale)


# --- Rangos exclusivos y rejilla representable (hallazgo 2) --------------------


def test_has_quantized_value_exclusive_point_at_the_type_limit_is_empty() -> None:
    # Reproducción: NUMERIC(3,2), min=max=9.99, min_exclusive ⇒ ningún valor (9.99
    # es tanto el único candidato como el máximo representable, y queda excluido).
    assert has_quantized_value(3, 2, low=9.99, high=9.99, min_exclusive=True) is False
    assert has_quantized_value(3, 2, low=9.99, high=9.99) is True  # sin exclusividad, sí cabe
    # Caso simétrico en el límite negativo, con max_exclusive.
    assert has_quantized_value(3, 2, low=-9.99, high=-9.99, max_exclusive=True) is False
    assert has_quantized_value(3, 2, low=-9.99, high=-9.99) is True


def test_has_quantized_value_check_greater_than_the_type_limit_is_empty() -> None:
    # Reproducción: NUMERIC(3,2) CHECK (x > 9.99) — sin máximo explícito, el
    # máximo efectivo es el propio límite del tipo (9.99, inclusivo), así que la
    # intersección con "x > 9.99" es vacía.
    assert has_quantized_value(3, 2, low=9.99, min_exclusive=True) is False
    assert has_quantized_value(3, 2, low=9.98, min_exclusive=True) is True  # 9.99 sigue cabiendo
    # Caso simétrico: CHECK (x < -9.99).
    assert has_quantized_value(3, 2, high=-9.99, max_exclusive=True) is False
    assert has_quantized_value(3, 2, high=-9.98, max_exclusive=True) is True


def test_has_quantized_value_rejects_a_real_interval_with_no_grid_point() -> None:
    # (1.001, 1.004) no es vacío como intervalo real, pero NUMERIC(_, 2) solo
    # almacena múltiplos de 0.01: ninguno cae ahí dentro. No basta con comprobar
    # el solape de intervalos reales (el propio hallazgo 2).
    assert has_quantized_value(6, 2, low=1.001, high=1.004) is False
    assert has_quantized_value(6, 2, low=1.001, high=1.01) is True  # 1.01 sí es múltiplo


def test_has_quantized_value_open_and_regression_ranges() -> None:
    assert has_quantized_value(3, 2) is True  # sin cotas: toda la ventana del tipo
    assert has_quantized_value(6, 2, low=0, high=100) is True
    assert has_quantized_value(3, 2, low=100, high=100) is False  # la reproducción del hallazgo 1


def test_decimal_ambient_context_is_never_mutated() -> None:
    from decimal import getcontext

    before = (getcontext().prec, getcontext().Emin, getcontext().Emax, getcontext().rounding)
    representable_limit(1000, 500)
    quantize_to_scale(Decimal("2E+998"), 0)
    fits(Decimal("9" * 1000), 1000, 0)
    has_quantized_value(1000, 0, low=1, high=2)
    after = (getcontext().prec, getcontext().Emin, getcontext().Emax, getcontext().rounding)
    assert before == after


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


def test_numeric_range_exclusive_point_at_the_limit_is_a_plan_error() -> None:
    # Reproducción: NUMERIC(3,2), min=max=9.99, min_exclusive=True. El intervalo
    # real [9.99, 9.99] "se solapa" con la ventana representable, pero con el
    # extremo excluido no queda ningún valor: antes se aceptaba en silencio.
    spec = parse_ddl("CREATE TABLE amounts (id SERIAL PRIMARY KEY, amount NUMERIC(3, 2) NOT NULL);")
    config = Config(
        tables={
            "amounts": TableConfig(
                rows=3,
                columns={
                    "amount": ColumnConfig(
                        generator="numeric_range",
                        params={"min": 9.99, "max": 9.99, "min_exclusive": True},
                    )
                },
            )
        }
    )
    with pytest.raises(PlanError, match=r"columna amount.*NUMERIC\(3, 2\)"):
        generate_dataset(spec, config)


def test_numeric_range_exclusive_point_at_the_negative_limit_is_a_plan_error() -> None:
    # Caso simétrico en el límite negativo, con max_exclusive.
    spec = parse_ddl("CREATE TABLE amounts (id SERIAL PRIMARY KEY, amount NUMERIC(3, 2) NOT NULL);")
    config = Config(
        tables={
            "amounts": TableConfig(
                rows=3,
                columns={
                    "amount": ColumnConfig(
                        generator="numeric_range",
                        params={"min": -9.99, "max": -9.99, "max_exclusive": True},
                    )
                },
            )
        }
    )
    with pytest.raises(PlanError, match=r"columna amount.*NUMERIC\(3, 2\)"):
        generate_dataset(spec, config)


def test_numeric_check_greater_than_the_type_limit_is_a_plan_error_not_a_crash() -> None:
    # Reproducción: NUMERIC(3,2) CHECK (amount > 9.99). Antes esto llegaba a
    # generación y terminaba en ValueError ("rango vacío") dentro del generador;
    # ahora debe rechazarse en compilación con un PlanError accionable.
    spec = parse_ddl(
        "CREATE TABLE amounts (id SERIAL PRIMARY KEY, "
        "amount NUMERIC(3, 2) NOT NULL CHECK (amount > 9.99));"
    )
    with pytest.raises(PlanError, match=r"columna amount.*NUMERIC\(3, 2\)"):
        generate_dataset(spec, Config(tables={"amounts": TableConfig(rows=3)}))


def test_numeric_check_less_than_the_negative_type_limit_is_a_plan_error() -> None:
    # Caso simétrico: CHECK (amount < -9.99).
    spec = parse_ddl(
        "CREATE TABLE amounts (id SERIAL PRIMARY KEY, "
        "amount NUMERIC(3, 2) NOT NULL CHECK (amount < -9.99));"
    )
    with pytest.raises(PlanError, match=r"columna amount.*NUMERIC\(3, 2\)"):
        generate_dataset(spec, Config(tables={"amounts": TableConfig(rows=3)}))


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
