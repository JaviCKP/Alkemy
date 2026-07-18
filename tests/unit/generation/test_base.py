"""Tests del registro, la resolución y la envoltura de unicidad (T2.2)."""

import random

import pytest
from pydantic import ValidationError

from synthdb.generation.generators import base, registered_names, resolve
from synthdb.generation.generators.base import (
    GenContext,
    UniqueExhaustedError,
    UnknownGeneratorError,
    _UniqueGenerator,
)
from synthdb.ir.schema import ColumnSpec, GeneratorSpec, TypeSpec


def _ctx(rng: random.Random, *, table: str = "t", name: str = "c") -> GenContext:
    return GenContext(
        rng=rng,
        column=ColumnSpec(name=name, type=TypeSpec(kind="integer"), nullable=True),
        table=table,
    )


class _Cycler:
    """Generador de prueba: elige de un conjunto pequeño y fijo de valores."""

    def __init__(self, values: list[int]) -> None:
        self._values = values

    def generate(self, ctx: GenContext) -> int:
        return ctx.rng.choice(self._values)


def test_registered_names_include_the_basic_catalog():
    assert set(registered_names()) >= {
        "faker",
        "numeric_range",
        "sequence",
        "datetime_range",
        "choice",
        "template",
        "uuid",
        "fallback",
    }


def test_resolve_unknown_generator():
    with pytest.raises(UnknownGeneratorError) as excinfo:
        resolve(GeneratorSpec(type="nope"))
    assert "nope" in str(excinfo.value)


def test_resolve_rejects_extra_params():
    with pytest.raises(ValidationError):
        resolve(GeneratorSpec(type="sequence", params={"start": 1, "bogus": 2}))


def test_resolve_rejects_missing_required_params():
    with pytest.raises(ValidationError):
        resolve(GeneratorSpec(type="choice", params={}))


def test_register_duplicate_name_raises():
    with pytest.raises(ValueError, match="ya está registrado"):
        base.register("faker", base.GeneratorParams, lambda _p: _Cycler([0]))


def test_resolve_wraps_unique():
    gen = resolve(GeneratorSpec(type="sequence", params={"start": 0}, unique=True))
    assert isinstance(gen, _UniqueGenerator)


def test_unique_wrapper_produces_distinct_values():
    uniq = _UniqueGenerator(_Cycler([1, 2, 3, 4, 5]))
    ctx = _ctx(random.Random(7))
    got = [uniq.generate(ctx) for _ in range(5)]
    assert sorted(got) == [1, 2, 3, 4, 5]


def test_unique_wrapper_exhaustion_is_actionable():
    uniq = _UniqueGenerator(_Cycler([10, 20]))
    ctx = _ctx(random.Random(1), table="clientes", name="cod")
    uniq.generate(ctx)
    uniq.generate(ctx)
    with pytest.raises(UniqueExhaustedError) as excinfo:
        uniq.generate(ctx)
    err = excinfo.value
    assert err.table == "clientes"
    assert err.column == "cod"
    assert err.achieved == 2
    assert "clientes.cod" in str(err)
