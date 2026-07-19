"""Utilidades compartidas de los tests del mini-DSL (T2.9/T2.10)."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from synthdb.generation.context import RowContext, mapping_resolver
from synthdb.ir.schema import ColumnSpec, TypeSpec

MakeCtx = Callable[..., RowContext]


@pytest.fixture
def make_ctx() -> MakeCtx:
    """Fábrica de `RowContext` de juguete para evaluar reglas en los tests.

    La `column` es un relleno (el intérprete no la usa; lee `row`/`parent`/`refs`/
    `rng`), así que los tests solo pasan lo que les importa.
    """

    def _make(
        row: Mapping[str, Any] | None = None,
        refs: Mapping[str, Any] | None = None,
        parents: Mapping[str, dict[str, Any] | None] | None = None,
        rng: random.Random | None = None,
        column: str = "x",
        table: str = "t",
    ) -> RowContext:
        return RowContext(
            rng=rng if rng is not None else random.Random(0),
            column=ColumnSpec(name=column, type=TypeSpec(kind="integer"), nullable=True),
            table=table,
            row=dict(row or {}),
            refs=dict(refs or {}),
            resolve_parent=mapping_resolver(parents or {}),
        )

    return _make
