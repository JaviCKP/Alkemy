"""Tests de los generadores de texto e identificadores (T2.3).

Cubre choice, template, uuid y el fallback seguro por kind del TypeSpec.
"""

import uuid
from collections import Counter
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from synthdb.generation.generators import resolve
from synthdb.ir.schema import GeneratorSpec


def gen(type_: str, **params: object):
    return resolve(GeneratorSpec(type=type_, params=params))


# --- choice ---------------------------------------------------------------


def test_choice_respects_weights(sample, make_column):
    col = make_column("text")
    vals = sample(gen("choice", values=["a", "b", "c"], weights=[0.8, 0.1, 0.1]), 2000, column=col)
    assert set(vals) <= {"a", "b", "c"}
    counts = Counter(vals)
    assert counts["a"] > counts["b"]
    assert counts["a"] > counts["c"]
    assert counts["a"] / len(vals) > 0.6  # ~0.8, tolerancia amplia


def test_choice_uniform_without_weights(sample, make_column):
    col = make_column("text")
    vals = sample(gen("choice", values=[1, 2, 3, 4]), 100, column=col)
    assert set(vals) <= {1, 2, 3, 4}


def test_choice_rejects_mismatched_weights():
    with pytest.raises(ValidationError):
        gen("choice", values=["a", "b"], weights=[1.0])


def test_choice_rejects_empty_values():
    with pytest.raises(ValidationError):
        gen("choice", values=[])


# --- template -------------------------------------------------------------


def test_template_default_and_counter(sample, make_column):
    col = make_column("text", name="email")
    vals = sample(gen("template"), 3, column=col, table="clientes")
    assert vals == ["clientes_email_0", "clientes_email_1", "clientes_email_2"]


def test_template_custom_with_start(sample, make_column):
    col = make_column("text", name="c")
    vals = sample(gen("template", template="X-{n}", start=100), 2, column=col)
    assert vals == ["X-100", "X-101"]


def test_template_rejects_unknown_placeholder():
    with pytest.raises(ValueError, match="plantilla inválida"):
        gen("template", template="{nope}")


# --- uuid -----------------------------------------------------------------


def test_uuid_is_v4_deterministic_and_distinct(sample, make_column):
    col = make_column("uuid")
    a = sample(gen("uuid"), 50, column=col)
    b = sample(gen("uuid"), 50, column=col)
    assert all(isinstance(v, uuid.UUID) and v.version == 4 for v in a)
    assert a == b  # determinista desde el RNG de fila
    assert len(set(a)) == 50  # de facto únicos


# --- fallback -------------------------------------------------------------


def test_fallback_integer_small_range(sample, make_column):
    vals = sample(gen("fallback"), 100, column=make_column("integer"))
    assert all(isinstance(v, int) for v in vals)
    assert all(0 <= v <= 1000 for v in vals)


def test_fallback_numeric_is_float(sample, make_column):
    vals = sample(gen("fallback"), 20, column=make_column("numeric"))
    assert all(isinstance(v, float) for v in vals)


def test_fallback_boolean(sample, make_column):
    vals = sample(gen("fallback"), 40, column=make_column("boolean"))
    assert all(isinstance(v, bool) for v in vals)
    assert set(vals) == {True, False}


def test_fallback_uuid(sample, make_column):
    vals = sample(gen("fallback"), 20, column=make_column("uuid"))
    assert all(isinstance(v, uuid.UUID) for v in vals)


def test_fallback_date_and_timestamp(sample, make_column):
    dates = sample(gen("fallback"), 20, column=make_column("date"))
    assert all(type(v) is date for v in dates)
    stamps = sample(gen("fallback"), 20, column=make_column("timestamp"))
    assert all(isinstance(v, datetime) for v in stamps)


def test_fallback_varchar_respects_length(sample, make_column):
    col = make_column("varchar", name="c", length=5)
    vals = sample(gen("fallback"), 50, column=col)
    assert all(isinstance(v, str) and len(v) <= 5 for v in vals)


def test_fallback_enum_uses_enum_values(sample, make_column):
    col = make_column("enum", name="estado", enum_values=["a", "b", "c"])
    vals = sample(gen("fallback"), 100, column=col)
    assert set(vals) <= {"a", "b", "c"}


def test_fallback_enum_without_values_raises(sample, make_column):
    col = make_column("enum", name="estado", enum_values=None)
    with pytest.raises(ValueError, match="no declara"):
        sample(gen("fallback"), 1, column=col)
