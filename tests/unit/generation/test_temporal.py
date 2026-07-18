"""Tests del generador temporal datetime_range (T2.3)."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from synthdb.generation.generators import resolve
from synthdb.ir.schema import GeneratorSpec


def dt(**params: object):
    return resolve(GeneratorSpec(type="datetime_range", params=params))


def test_date_column_returns_date_in_range(sample, make_column):
    col = make_column("date")
    vals = sample(dt(min="2020-01-01", max="2020-12-31"), 200, column=col)
    # date, no datetime (que es subclase de date).
    assert all(type(v) is date for v in vals)
    assert all(date(2020, 1, 1) <= v <= date(2020, 12, 31) for v in vals)


def test_timestamp_column_returns_naive_datetime_in_range(sample, make_column):
    col = make_column("timestamp", with_timezone=False)
    lo, hi = datetime(2020, 1, 1), datetime(2020, 1, 2)
    vals = sample(dt(min="2020-01-01T00:00:00", max="2020-01-02T00:00:00"), 200, column=col)
    assert all(isinstance(v, datetime) for v in vals)
    assert all(lo <= v <= hi for v in vals)
    assert all(v.tzinfo is None for v in vals)


def test_timestamptz_column_is_timezone_aware(sample, make_column):
    col = make_column("timestamp", with_timezone=True)
    vals = sample(dt(min="2020-01-01", max="2020-01-02"), 50, column=col)
    assert all(v.tzinfo is not None for v in vals)


def test_reproducible(sample, make_column):
    col = make_column("date")
    a = sample(dt(min="2000-01-01", max="2010-01-01"), 100, column=col)
    b = sample(dt(min="2000-01-01", max="2010-01-01"), 100, column=col)
    assert a == b


def test_default_range_is_the_fixed_decade(sample, make_column):
    col = make_column("date")
    vals = sample(dt(), 100, column=col)
    assert all(date(2015, 1, 1) <= v <= date(2025, 1, 1) for v in vals)


def test_rejects_inverted_range():
    with pytest.raises(ValidationError):
        dt(min="2020-01-01", max="2019-01-01")
