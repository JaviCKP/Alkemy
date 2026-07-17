"""Tests parametrizados del mapeo de tipos de PostgreSQL (T1.2)."""

import pytest

from synthdb.ir.schema import TypeSpec
from synthdb.parsing.types import map_postgres_type

KNOWN_TYPE_CASES = [
    # Enteros: serial implica autoincrement; el resto de la familia, no. El
    # ancho en bits acompaña a cada alias (smallint=16, integer=32, bigint=64).
    ("SERIAL", {}, TypeSpec(kind="integer", autoincrement=True, bits=32)),
    ("serial4", {}, TypeSpec(kind="integer", autoincrement=True, bits=32)),
    ("bigserial", {}, TypeSpec(kind="integer", autoincrement=True, bits=64)),
    ("serial8", {}, TypeSpec(kind="integer", autoincrement=True, bits=64)),
    ("smallserial", {}, TypeSpec(kind="integer", autoincrement=True, bits=16)),
    ("serial2", {}, TypeSpec(kind="integer", autoincrement=True, bits=16)),
    ("INT", {}, TypeSpec(kind="integer", bits=32)),
    ("integer", {}, TypeSpec(kind="integer", bits=32)),
    ("int4", {}, TypeSpec(kind="integer", bits=32)),
    ("bigint", {}, TypeSpec(kind="integer", bits=64)),
    ("int8", {}, TypeSpec(kind="integer", bits=64)),
    ("smallint", {}, TypeSpec(kind="integer", bits=16)),
    ("int2", {}, TypeSpec(kind="integer", bits=16)),
    # Numéricos, con y sin precisión/escala explícitas.
    ("numeric", {"precision": 7, "scale": 2}, TypeSpec(kind="numeric", precision=7, scale=2)),
    ("NUMERIC", {}, TypeSpec(kind="numeric")),
    ("decimal", {"precision": 12, "scale": 4}, TypeSpec(kind="numeric", precision=12, scale=4)),
    # Coma flotante binaria: mapea a numeric sin precisión/escala.
    ("real", {}, TypeSpec(kind="numeric")),
    ("float4", {}, TypeSpec(kind="numeric")),
    ("double precision", {}, TypeSpec(kind="numeric")),
    ("float8", {}, TypeSpec(kind="numeric")),
    ("float", {}, TypeSpec(kind="numeric")),
    ("DOUBLE PRECISION", {}, TypeSpec(kind="numeric")),
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
    ("  int   ", {}, TypeSpec(kind="integer", bits=32)),
]

INTEGER_BITS_CASES = [
    ("smallint", 16),
    ("int2", 16),
    ("smallserial", 16),
    ("serial2", 16),
    ("integer", 32),
    ("int", 32),
    ("int4", 32),
    ("serial", 32),
    ("serial4", 32),
    ("bigint", 64),
    ("int8", 64),
    ("bigserial", 64),
    ("serial8", 64),
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


@pytest.mark.parametrize("raw_type,expected_bits", INTEGER_BITS_CASES)
def test_integer_aliases_carry_their_bit_width(raw_type, expected_bits) -> None:
    # Sin CHECK, el ancho del tipo es la cota implícita del generador (H2):
    # un smallint admite hasta 32767, no el rango de un integer.
    result = map_postgres_type(raw_type)

    assert result.type_spec.bits == expected_bits


def test_is_array_marks_the_type_without_changing_the_element() -> None:
    # El sufijo [] lo detecta el parser desde el AST y lo pasa como is_array; el
    # kind y los parámetros siguen siendo los del elemento (ADR-004).
    assert map_postgres_type("text", is_array=True).type_spec == TypeSpec(
        kind="text", is_array=True
    )
    assert map_postgres_type("numeric", precision=7, scale=2, is_array=True).type_spec == TypeSpec(
        kind="numeric", precision=7, scale=2, is_array=True
    )


def test_is_array_is_false_by_default() -> None:
    assert map_postgres_type("text").type_spec.is_array is False


@pytest.mark.parametrize("raw_type", ["real", "float4", "double precision", "float8", "float"])
def test_float_family_maps_to_numeric_dropping_any_precision(raw_type) -> None:
    # El argumento de float(p) selecciona real vs. double, no una precisión
    # decimal: no debe propagarse como numeric(p). Se descarta aunque llegue.
    result = map_postgres_type(raw_type, precision=24, scale=6)

    assert result.type_spec == TypeSpec(kind="numeric")
    assert result.type_spec.precision is None
    assert result.type_spec.scale is None
    assert result.type_spec.bits is None
    assert result.warnings == []
