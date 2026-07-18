"""Tests de los generadores numéricos: numeric_range y sequence (T2.3)."""

import statistics
from collections import Counter

import pytest
from pydantic import ValidationError

from synthdb.generation.generators import resolve
from synthdb.ir.schema import GeneratorSpec


def num(**params: object):
    return resolve(GeneratorSpec(type="numeric_range", params=params))


def test_integer_uniform_type_bounds_and_reproducibility(sample, make_column):
    col = make_column("integer")
    vals = sample(num(min=10, max=20), 200, column=col)
    assert all(isinstance(v, int) for v in vals)
    assert all(10 <= v <= 20 for v in vals)
    # Misma semilla ⇒ misma secuencia.
    assert sample(num(min=10, max=20), 200, column=col) == vals
    # Distinta semilla ⇒ secuencia distinta.
    assert sample(num(min=10, max=20), 200, column=col, seed=999) != vals


def test_integer_uses_type_bits_as_implicit_bound(sample, make_column):
    col = make_column("integer", bits=16)  # smallint: [-32768, 32767]
    vals = sample(num(), 500, column=col)  # sin min/max explícitos
    assert all(isinstance(v, int) for v in vals)
    assert all(-(2**15) <= v <= 2**15 - 1 for v in vals)


def test_integer_exclusive_bounds(sample, make_column):
    col = make_column("integer")
    vals = sample(num(min=0, max=3, min_exclusive=True, max_exclusive=True), 300, column=col)
    assert set(vals) <= {1, 2}  # 0 y 3 quedan excluidos


def test_round_to_quantizes(sample, make_column):
    col = make_column("numeric")
    vals = sample(num(min=0, max=10, round_to=0.5), 200, column=col)
    assert all(0 <= v <= 10 for v in vals)
    assert all(abs(v * 2 - round(v * 2)) < 1e-9 for v in vals)  # múltiplos de 0.5


def test_float_range_type_and_bounds(sample, make_column):
    col = make_column("numeric")
    vals = sample(num(min=1.5, max=2.5), 200, column=col)
    assert all(isinstance(v, float) for v in vals)
    assert all(1.5 <= v <= 2.5 for v in vals)


def test_empty_integer_range_raises(sample, make_column):
    col = make_column("integer")
    gen = num(min=5, max=5, min_exclusive=True, max_exclusive=True)
    with pytest.raises(ValueError, match="rango entero vacío"):
        sample(gen, 1, column=col)


def test_normal_distribution_is_centered(sample, make_column):
    col = make_column("numeric")
    dist = {"family": "normal", "params": {"mean": 50, "std": 10}}
    vals = sample(num(min=0, max=100, distribution=dist), 2000, column=col)
    assert all(0 <= v <= 100 for v in vals)
    assert abs(statistics.mean(vals) - 50) < 5  # tolerancia amplia
    within_2std = sum(1 for v in vals if 30 <= v <= 70)
    assert within_2std / len(vals) > 0.85


def test_lognormal_is_right_skewed(sample, make_column):
    col = make_column("numeric")
    dist = {"family": "lognormal", "params": {"median": 90, "sigma": 0.5}}
    vals = sample(num(min=10, max=1000, distribution=dist), 2000, column=col)
    assert all(10 <= v <= 1000 for v in vals)
    assert statistics.median(vals) < statistics.mean(vals)  # cola a la derecha
    assert 50 < statistics.median(vals) < 150  # cerca de la mediana pedida (90)


def test_zipf_favors_the_low_end(sample, make_column):
    col = make_column("integer")
    dist = {"family": "zipf", "params": {"s": 1.5}}
    vals = sample(num(min=1, max=10, distribution=dist), 2000, column=col)
    assert all(1 <= v <= 10 for v in vals)
    counts = Counter(vals)
    assert counts[1] > counts[10]  # el mínimo es más frecuente que el máximo
    assert counts.most_common(1)[0][0] == 1  # la moda es el mínimo


def test_numeric_rejects_min_greater_than_max():
    with pytest.raises(ValidationError):
        num(min=10, max=1)


def test_numeric_rejects_flat_distribution_params():
    # La forma plana (parámetros hermanos de distribution) ya no existe.
    with pytest.raises(ValidationError):
        num(min=0, max=1, mean=5)


def test_numeric_rejects_unknown_param_for_family():
    # 'mean' no pertenece a la familia zipf ⇒ error de campo exacto.
    with pytest.raises(ValidationError):
        num(min=1, max=10, distribution={"family": "zipf", "params": {"mean": 5}})


def test_sequence_arithmetic_and_reproducibility(sample, make_column):
    col = make_column("integer")
    gen = resolve(GeneratorSpec(type="sequence", params={"start": 5, "step": 2}))
    assert sample(gen, 5, column=col) == [5, 7, 9, 11, 13]
    fresh = resolve(GeneratorSpec(type="sequence", params={"start": 5, "step": 2}))
    assert sample(fresh, 5, column=col) == [5, 7, 9, 11, 13]


def test_sequence_rejects_zero_step():
    with pytest.raises(ValidationError):
        resolve(GeneratorSpec(type="sequence", params={"step": 0}))
