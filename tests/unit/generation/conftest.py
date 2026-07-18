"""Utilidades compartidas de los tests de generadores (T2.1-T2.3).

Importar `synthdb.generation.generators` registra todo el catálogo básico, así
que estos tests pueden resolver generadores por nombre.
"""

from collections.abc import Callable
from typing import Any

import pytest

import synthdb.generation.generators  # noqa: F401 -- registra el catálogo
from synthdb.generation.generators.base import GenContext, Generator
from synthdb.generation.seeding import rng_for_row, seed_for_table
from synthdb.ir.schema import ColumnSpec, TypeSpec


@pytest.fixture
def make_column() -> Callable[..., ColumnSpec]:
    """Fábrica de `ColumnSpec` con un `TypeSpec` armado a partir de kwargs."""

    def _make(
        kind: str = "integer",
        *,
        name: str = "col",
        enum_values: list[str] | None = None,
        **type_kwargs: Any,
    ) -> ColumnSpec:
        return ColumnSpec(
            name=name,
            type=TypeSpec(kind=kind, **type_kwargs),
            nullable=True,
            enum_values=enum_values,
        )

    return _make


@pytest.fixture
def sample() -> Callable[..., list[Any]]:
    """Genera `n` valores, uno por fila, con el RNG jerárquico real (T2.1).

    Cada fila usa `rng_for_row(seed_for_table(seed, table), i)`, de modo que los
    tests ejercitan la misma derivación determinista que el motor.
    """

    def _sample(
        generator: Generator,
        n: int,
        *,
        column: ColumnSpec,
        table: str = "t",
        seed: int = 123,
    ) -> list[Any]:
        table_seed = seed_for_table(seed, table)
        return [
            generator.generate(
                GenContext(rng=rng_for_row(table_seed, i), column=column, table=table)
            )
            for i in range(n)
        ]

    return _sample
