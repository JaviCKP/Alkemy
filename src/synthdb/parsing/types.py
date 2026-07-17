"""Catálogo canónico de tipos y mapeo de tipos de columna de PostgreSQL (T1.2).

Traduce nombres de tipo tal como aparecen en DDL de PostgreSQL al catálogo
canónico de la IR (`synthdb.ir.schema.TypeKind`). Es deliberadamente
independiente de `sqlglot`: recibe el nombre del tipo y sus parámetros ya
extraídos (precisión, escala, longitud), no un nodo de AST. Construir ese
puente desde el AST de sqlglot es trabajo del parser DDL (TODO(T1.3),
`parsing/ddl.py`), que es quien conoce si un nombre no reconocido corresponde
a un `CREATE TYPE ... AS ENUM` ya visto (parámetro `is_enum`).

Un tipo de PostgreSQL sin mapeo conocido nunca aborta el proceso: degrada a
`text` (el tipo más permisivo) y devuelve un aviso, siguiendo el principio de
"nada falla en silencio" de CLAUDE.md.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from synthdb.ir.schema import TypeKind, TypeSpec

_AUTOINCREMENT_ALIASES: frozenset[str] = frozenset(
    {"serial", "serial4", "bigserial", "serial8", "smallserial", "serial2"}
)
"""Alias de PostgreSQL que implican `TypeSpec.autoincrement=True`."""

_TIMEZONE_AWARE_ALIASES: frozenset[str] = frozenset({"timestamptz", "timestamp with time zone"})
"""Alias de PostgreSQL que implican `TypeSpec.with_timezone=True`."""

_INTEGER_BITS: dict[str, Literal[16, 32, 64]] = {
    "smallint": 16,
    "int2": 16,
    "smallserial": 16,
    "serial2": 16,
    "integer": 32,
    "int": 32,
    "int4": 32,
    "serial": 32,
    "serial4": 32,
    "bigint": 64,
    "int8": 64,
    "bigserial": 64,
    "serial8": 64,
}
"""Ancho en bits de cada alias entero de PostgreSQL. Sin CHECK, este ancho es
la cota implícita del generador de enteros (H2), de ahí que se registre en
`TypeSpec.bits`."""

_FLOAT_ALIASES: frozenset[str] = frozenset(
    {"real", "float4", "double precision", "float8", "float"}
)
"""Alias de coma flotante binaria. Mapean a `numeric` sin precisión/escala: el
argumento de `float(p)` selecciona el tamaño de almacenamiento (real vs.
double), no una precisión decimal, así que no debe propagarse como `numeric(p)`."""

_POSTGRES_TYPE_MAP: dict[str, TypeKind] = {
    # Enteros: todos comparten el kind canónico "integer"; el ancho en bits se
    # registra aparte en `TypeSpec.bits` (ver `_INTEGER_BITS`) porque, sin
    # CHECK, ese ancho es la cota implícita del generador.
    "serial": "integer",
    "serial4": "integer",
    "bigserial": "integer",
    "serial8": "integer",
    "smallserial": "integer",
    "serial2": "integer",
    "integer": "integer",
    "int": "integer",
    "int4": "integer",
    "bigint": "integer",
    "int8": "integer",
    "smallint": "integer",
    "int2": "integer",
    # Numéricos con precisión/escala.
    "numeric": "numeric",
    "decimal": "numeric",
    # Coma flotante binaria (mapea a numeric sin precisión/escala, ver
    # `_FLOAT_ALIASES`); si no, degradarían a text e invalidarían el INSERT.
    "real": "numeric",
    "float4": "numeric",
    "double precision": "numeric",
    "float8": "numeric",
    "float": "numeric",
    # Texto.
    "text": "text",
    "varchar": "varchar",
    "character varying": "varchar",
    "char": "char",
    "character": "char",
    "bpchar": "char",
    # Fecha y hora.
    "date": "date",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamptz": "timestamp",
    "timestamp with time zone": "timestamp",
    # Resto de tipos base soportados.
    "boolean": "boolean",
    "bool": "boolean",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "json",
    "bytea": "bytea",
}
"""Tabla de mapeo: alias de tipo de PostgreSQL (normalizado) -> `TypeKind`."""


class TypeMappingResult(NamedTuple):
    """Resultado de traducir un tipo de PostgreSQL al catálogo canónico."""

    type_spec: TypeSpec
    warnings: list[str]


def _normalize(raw_type: str) -> str:
    """Normaliza un nombre de tipo para buscarlo en la tabla de mapeo.

    Args:
        raw_type: Nombre de tipo tal como aparece en el DDL.

    Returns:
        El nombre en minúsculas, sin espacios extra al principio/final ni
        espacios internos repetidos (`"  Character  Varying "` -> `"character varying"`).
    """
    return " ".join(raw_type.strip().lower().split())


def map_postgres_type(
    raw_type: str,
    *,
    precision: int | None = None,
    scale: int | None = None,
    length: int | None = None,
    is_enum: bool = False,
    is_array: bool = False,
) -> TypeMappingResult:
    """Traduce un tipo de columna de PostgreSQL al catálogo canónico de la IR.

    Args:
        raw_type: Nombre del tipo tal como aparece en el DDL (`INT`,
            `varchar`, `NUMERIC`, `timestamptz`...). No sensible a
            mayúsculas ni a espacios extra. Para un array (`text[]`) es el
            nombre del ELEMENTO (`text`); el sufijo `[]` no llega aquí (lo
            detecta el parser DDL desde el AST y lo pasa como `is_array`).
        precision: Precisión declarada en `NUMERIC(precision, scale)`.
        scale: Escala declarada en `NUMERIC(precision, scale)`.
        length: Longitud declarada en `VARCHAR(length)`/`CHAR(length)`.
        is_enum: `True` si `raw_type` nombra un tipo `CREATE TYPE ... AS ENUM`
            ya reconocido por el llamador, en vez de un tipo base de
            PostgreSQL.
        is_array: `True` si la columna es un array (`text[]`, `numeric(7,2)[]`).
            El `kind` y los parámetros siguen siendo los del elemento; solo se
            marca `TypeSpec.is_array` (ADR-004).

    Returns:
        El `TypeSpec` canónico junto con los avisos generados (lista vacía
        si el tipo se reconoció sin ambigüedad). Un tipo no reconocido nunca
        lanza una excepción: degrada a `TypeSpec(kind="text")` con un aviso.
    """
    type_spec, warnings = _map_element_type(
        raw_type, precision=precision, scale=scale, length=length, is_enum=is_enum
    )
    if is_array:
        type_spec = type_spec.model_copy(update={"is_array": True})
    return TypeMappingResult(type_spec, warnings)


def _map_element_type(
    raw_type: str,
    *,
    precision: int | None,
    scale: int | None,
    length: int | None,
    is_enum: bool,
) -> tuple[TypeSpec, list[str]]:
    """Traduce el tipo del elemento (sin la dimensión de array) a `TypeSpec`."""
    if is_enum:
        return TypeSpec(kind="enum"), []

    normalized = _normalize(raw_type)
    kind = _POSTGRES_TYPE_MAP.get(normalized)

    if kind is None:
        warning = (
            f"Tipo PostgreSQL desconocido: {raw_type!r}; se trata como texto "
            "sin restricciones. Añade un mapeo en parsing/types.py o registra "
            "el caso en docs/limitations.md."
        )
        return TypeSpec(kind="text"), [warning]

    if kind == "integer":
        return (
            TypeSpec(
                kind="integer",
                autoincrement=normalized in _AUTOINCREMENT_ALIASES,
                bits=_INTEGER_BITS.get(normalized),
            ),
            [],
        )
    if kind == "numeric":
        if normalized in _FLOAT_ALIASES:
            # float(p) selecciona real vs. double (tamaño de almacenamiento),
            # no una precisión decimal: no se propaga como numeric(p, s).
            return TypeSpec(kind="numeric"), []
        return TypeSpec(kind="numeric", precision=precision, scale=scale), []
    if kind == "varchar":
        return TypeSpec(kind="varchar", length=length), []
    if kind == "char":
        return TypeSpec(kind="char", length=length), []
    if kind == "timestamp":
        return TypeSpec(kind="timestamp", with_timezone=normalized in _TIMEZONE_AWARE_ALIASES), []
    return TypeSpec(kind=kind), []
