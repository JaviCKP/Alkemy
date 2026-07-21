"""Comprobaciones de semántica NUMERIC contra PostgreSQL real."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.mark.integration
def test_postgres_numeric_rounding_and_overflow() -> None:
    url = os.environ.get("SYNTHDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SYNTHDB_TEST_POSTGRES_URL no está configurada")

    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 0.385::NUMERIC(3, 2), -0.385::NUMERIC(3, 2)")
        positive, negative = cursor.fetchone()

    assert positive == Decimal("0.39")
    assert negative == Decimal("-0.39")

    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.NumericValueOutOfRange, match="numeric field overflow"):
            cursor.execute("SELECT 9.995::NUMERIC(3, 2)")
        connection.rollback()
