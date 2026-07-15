"""Tests parametrizados del mapeo de tipos de PostgreSQL (T1.2)."""

import pytest

from synthdb.ir.schema import TypeSpec
from synthdb.parsing.types import map_postgres_type

KNOWN_TYPE_CASES = [
    # Enteros: serial implica autoincrement; el resto de la familia, no.
    ("SERIAL", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("serial4", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("bigserial", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("serial8", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("smallserial", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("serial2", {}, TypeSpec(kind="integer", autoincrement=True)),
    ("INT", {}, TypeSpec(kind="integer")),
    ("integer", {}, TypeSpec(kind="integer")),
    ("int4", {}, TypeSpec(kind="integer")),
    ("bigint", {}, TypeSpec(kind="integer")),
    ("int8", {}, TypeSpec(kind="integer")),
    ("smallint", {}, TypeSpec(kind="integer")),
    ("int2", {}, TypeSpec(kind="integer")),
    # Numéricos, con y sin precisión/escala explícitas.
    ("numeric", {"precision": 7, "scale": 2}, TypeSpec(kind="numeric", precision=7, scale=2)),
    ("NUMERIC", {}, TypeSpec(kind="numeric")),
    ("decimal", {"precision": 12, "scale": 4}, TypeSpec(kind="numeric", precision=12, scale=4)),
    # Texto, con y sin longitud.
    ("text", {}, TypeSpec(kind="text")),
    ("varchar", {"length": 50}, TypeSpec(kind="varchar", length=50)),
    ("VARCHAR", {}, TypeSpec(kind="varchar")),
    ("character varying", {"length": 100}, TypeSpec(kind="varchar", length=100)),
    ("char", {"length": 2}, TypeSpec(kind="char", length=2)),
    ("character", {"length": 1}, TypeSpec(kind="char", length=1)),
    ("bpchar", {}, TypeSpec(kind="char")),
    # Fecha y hora: timestamp/timestamptz difieren solo en with_timezone.
    ("date", {}, TypeSpec(kind="date")),
    ("timestamp", {}, TypeSpec(kind="timestamp", with_timezone=False)),
    ("timestamp without time zone", {}, TypeSpec(kind="timestamp", with_timezone=False)),
    ("timestamptz", {}, TypeSpec(kind="timestamp", with_timezone=True)),
    ("timestamp with time zone", {}, TypeSpec(kind="timestamp", with_timezone=True)),
    ("TimestampTZ", {}, TypeSpec(kind="timestamp", with_timezone=True)),
    # Resto del catálogo canónico.
    ("boolean", {}, TypeSpec(kind="boolean")),
    ("bool", {}, TypeSpec(kind="boolean")),
    ("uuid", {}, TypeSpec(kind="uuid")),
    ("json", {}, TypeSpec(kind="json")),
    ("jsonb", {}, TypeSpec(kind="json")),
    ("bytea", {}, TypeSpec(kind="bytea")),
    # Normalización: mayúsculas y espacios extra no cambian el resultado.
    ("  Character Varying ", {"length": 10}, TypeSpec(kind="varchar", length=10)),
    ("  int   ", {}, TypeSpec(kind="integer")),
]

UNKNOWN_TYPE_CASES = [
    "hstore",
    "inet",
    "cidr",
    "tsvector",
    "money",
    "point",
    "xml",
    "int4range",
    "",
    "no_existe_este_tipo",
]


@pytest.mark.parametrize("raw_type,kwargs,expected", KNOWN_TYPE_CASES)
def test_known_postgres_types_map_without_warnings(raw_type, kwargs, expected) -> None:
    result = map_postgres_type(raw_type, **kwargs)

    assert result.type_spec == expected
    assert result.warnings == []


@pytest.mark.parametrize("raw_type", UNKNOWN_TYPE_CASES)
def test_unknown_postgres_types_degrade_to_text_with_a_registered_warning(raw_type) -> None:
    result = map_postgres_type(raw_type)

    assert result.type_spec == TypeSpec(kind="text")
    assert len(result.warnings) == 1
    assert repr(raw_type) in result.warnings[0]


def test_unknown_type_never_raises() -> None:
    # Aviso registrado, nunca fallo silencioso ni excepción que detenga todo
    # el pipeline de parseo (CLAUDE.md: "nada falla ni se ignora en silencio").
    result = map_postgres_type("un_tipo_que_no_existe_en_absoluto")

    assert result.type_spec.kind == "text"
    assert result.warnings


@pytest.mark.parametrize("raw_type", ["estado_pedido", "mi_tipo_enum", "direccion_postal"])
def test_enum_types_bypass_the_lookup_table(raw_type) -> None:
    result = map_postgres_type(raw_type, is_enum=True)

    assert result.type_spec == TypeSpec(kind="enum")
    assert result.warnings == []


def test_is_enum_takes_priority_over_a_colliding_builtin_name() -> None:
    result = map_postgres_type("text", is_enum=True)

    assert result.type_spec == TypeSpec(kind="enum")


def test_numeric_without_params_has_no_precision_or_scale() -> None:
    result = map_postgres_type("numeric")

    assert result.type_spec.precision is None
    assert result.type_spec.scale is None


def test_varchar_without_length_is_unbounded() -> None:
    result = map_postgres_type("varchar")

    assert result.type_spec.length is None
