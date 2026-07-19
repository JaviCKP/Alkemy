"""Tests de clasificación de reglas (T2.9): bound / derivation / assertion (§7.2, §16)."""

from __future__ import annotations

import pytest

from synthdb.rules import as_bound, as_derivation, clasify_rule, parse_rule, rule_dependencies


@pytest.mark.parametrize(
    "text",
    [
        # Ejemplo temporal de §16 (columna local acotada por el padre).
        "fecha >= parent(vivienda_id).anio_construccion",
        # Restricción temporal entre dos columnas de la fila.
        "fecha_defuncion > fecha_nacimiento",
        # Forma volteada: 0 < precio  ≡  precio > 0.
        "0 < precio",
        "superficie_m2 <= 450",
    ],
)
def test_bound_classification(text: str) -> None:
    assert clasify_rule(parse_rule(text)) == "bound"


@pytest.mark.parametrize(
    "text",
    [
        # Derivación de §16.
        "precio = superficie * ref('precio_m2_base') * noise(0.2)",
        # Copia directa de otra columna.
        "total = subtotal",
        # Forma volteada: expr = col.
        "superficie * ref('m2') = precio",
    ],
)
def test_derivation_classification(text: str) -> None:
    assert clasify_rule(parse_rule(text)) == "derivation"


@pytest.mark.parametrize(
    "text",
    [
        "a + b = c + d",  # ninguna columna despejada
        "activo and superficie > 0",  # conjunción booleana
        "precio <> 0",  # '<>' con columna despejada no es cota
        "a = a + 1",  # la columna aparece en ambos lados: no es derivación
    ],
)
def test_assertion_classification(text: str) -> None:
    assert clasify_rule(parse_rule(text)) == "assertion"


def test_bound_direction_and_exclusivity() -> None:
    lower_incl = as_bound(parse_rule("fecha >= parent(vivienda_id).anio_construccion"))
    assert lower_incl is not None
    assert (lower_incl.column, lower_incl.side, lower_incl.exclusive) == ("fecha", "lower", False)

    # Volteada y estricta: 0 < precio  ⇒  precio > 0  ⇒  cota inferior exclusiva.
    lower_excl = as_bound(parse_rule("0 < precio"))
    assert lower_excl is not None
    assert (lower_excl.column, lower_excl.side, lower_excl.exclusive) == ("precio", "lower", True)

    upper_incl = as_bound(parse_rule("superficie_m2 <= 450"))
    assert upper_incl is not None
    assert (upper_incl.column, upper_incl.side, upper_incl.exclusive) == (
        "superficie_m2",
        "upper",
        False,
    )


def test_derivation_target_and_dependencies() -> None:
    rule = parse_rule("precio = superficie * ref('precio_m2_base') * noise(0.2)")
    derivation = as_derivation(rule)
    assert derivation is not None
    assert derivation.column == "precio"
    # La expresión lee la columna local `superficie` (ref/noise no son columnas).
    target, reads = rule_dependencies(rule)  # type: ignore[misc]
    assert target == "precio"
    assert reads == frozenset({"superficie"})


def test_assertion_has_no_ordering_dependency() -> None:
    assert rule_dependencies(parse_rule("a + b = c + d")) is None
    assert as_bound(parse_rule("precio <> 0")) is None
    assert as_derivation(parse_rule("fecha > fecha_inicio")) is None


def test_parent_and_ref_do_not_count_as_local_dependencies() -> None:
    # La cota lee del padre y de refs, no de columnas locales: sin dependencia local.
    rule = parse_rule("fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)")
    target, reads = rule_dependencies(rule)  # type: ignore[misc]
    assert target == "fecha"
    assert reads == frozenset()
