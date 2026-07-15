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

from typing import NamedTuple

from synthdb.ir.schema import TypeKind, TypeSpec

_AUTOINCREMENT_ALIASES: frozenset[str] = frozenset(
    {"serial", "serial4", "bigserial", "serial8", "smallserial", "serial2"}
)
"""Alias de PostgreSQL que implican `TypeSpec.autoincrement=True`."""

_TIMEZONE_AWARE_ALIASES: frozenset[str] = frozenset({"timestamptz", "timestamp with time zone"})
"""Alias de PostgreSQL que implican `TypeSpec.with_timezone=True`."""

_POSTGRES_TYPE_MAP: dict[str, TypeKind] = {
    # Enteros (la distinción de bits no forma parte del catálogo canónico;
    # las cotas reales las aporta el CHECK propagado, no el tipo).
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
) -> TypeMappingResult:
    """Traduce un tipo de columna de PostgreSQL al catálogo canónico de la IR.

    Args:
        raw_type: Nombre del tipo tal como aparece en el DDL (`INT`,
            `varchar`, `NUMERIC`, `timestamptz`...). No sensible a
            mayúsculas ni a espacios extra.
        precision: Precisión declarada en `NUMERIC(precision, scale)`.
        scale: Escala declarada en `NUMERIC(precision, scale)`.
        length: Longitud declarada en `VARCHAR(length)`/`CHAR(length)`.
        is_enum: `True` si `raw_type` nombra un tipo `CREATE TYPE ... AS ENUM`
            ya reconocido por el llamador, en vez de un tipo base de
            PostgreSQL.

    Returns:
        El `TypeSpec` canónico junto con los avisos generados (lista vacía
        si el tipo se reconoció sin ambigüedad). Un tipo no reconocido nunca
        lanza una excepción: degrada a `TypeSpec(kind="text")` con un aviso.
    """
    if is_enum:
        return TypeMappingResult(TypeSpec(kind="enum"), [])

    normalized = _normalize(raw_type)
    kind = _POSTGRES_TYPE_MAP.get(normalized)

    if kind is None:
        warning = (
            f"Tipo PostgreSQL desconocido: {raw_type!r}; se trata como texto "
            "sin restricciones. Añade un mapeo en parsing/types.py o registra "
            "el caso en docs/limitations.md."
        )
        return TypeMappingResult(TypeSpec(kind="text"), [warning])

    if kind == "integer":
        return TypeMappingResult(
            TypeSpec(kind="integer", autoincrement=normalized in _AUTOINCREMENT_ALIASES), []
        )
    if kind == "numeric":
        return TypeMappingResult(TypeSpec(kind="numeric", precision=precision, scale=scale), [])
    if kind == "varchar":
        return TypeMappingResult(TypeSpec(kind="varchar", length=length), [])
    if kind == "char":
        return TypeMappingResult(TypeSpec(kind="char", length=length), [])
    if kind == "timestamp":
        return TypeMappingResult(
            TypeSpec(kind="timestamp", with_timezone=normalized in _TIMEZONE_AWARE_ALIASES), []
        )
    return TypeMappingResult(TypeSpec(kind=kind), [])
