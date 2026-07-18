"""Tests del generador basado en Faker (T2.3)."""

import pytest

from synthdb.generation.generators import resolve
from synthdb.ir.schema import GeneratorSpec


def faker(**params: object):
    return resolve(GeneratorSpec(type="faker", params=params))


def test_returns_nonempty_string(sample, make_column):
    col = make_column("text", name="nombre")
    vals = sample(faker(provider="name"), 20, column=col)
    assert all(isinstance(v, str) and v for v in vals)


def test_reproducible_same_seed(sample, make_column):
    col = make_column("text", name="email")
    a = sample(faker(provider="email"), 50, column=col)
    b = sample(faker(provider="email"), 50, column=col)
    assert a == b


def test_different_seed_differs(sample, make_column):
    col = make_column("text", name="email")
    a = sample(faker(provider="email"), 50, column=col, seed=1)
    b = sample(faker(provider="email"), 50, column=col, seed=2)
    assert a != b


def test_default_locale_es_es_runs(sample, make_column):
    # El locale por defecto es es_ES; comprobamos que resuelve y produce texto.
    col = make_column("text", name="ciudad")
    vals = sample(faker(provider="city"), 10, column=col)
    assert all(isinstance(v, str) and v for v in vals)


def test_locale_override(sample, make_column):
    col = make_column("text", name="n")
    vals = sample(faker(provider="name", locale="en_US"), 10, column=col)
    assert all(isinstance(v, str) and v for v in vals)


def test_unique_wrapper_yields_distinct(sample, make_column):
    col = make_column("text", name="email")
    gen = resolve(GeneratorSpec(type="faker", params={"provider": "email"}, unique=True))
    vals = sample(gen, 100, column=col)
    assert len(set(vals)) == 100


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="no existe"):
        faker(provider="definitely_not_a_provider")
