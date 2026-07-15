"""Modelos Pydantic v2 de la IR canónica (especificacion.md §5).

La IR (`SchemaSpec`) es la única fuente de verdad estructural del proyecto:
nada aguas abajo relee SQL ni reinterpreta el esquema (CLAUDE.md). Estos
modelos no parsean DDL ni calculan nada por sí mismos; eso es trabajo de
`parsing/ddl.py` (TODO(T1.3)) y `ir/hashing.py` (TODO(T1.5)) respectivamente.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TypeKind = Literal[
    "integer",
    "numeric",
    "text",
    "varchar",
    "char",
    "date",
    "timestamp",
    "boolean",
    "uuid",
    "enum",
    "json",
    "bytea",
]
"""Catálogo canónico de tipos de columna (especificacion.md §4)."""

ReferentialAction = Literal["cascade", "restrict", "set_null", "set_default", "no_action"]
"""Acción referencial de una FK ante `ON DELETE`/`ON UPDATE`."""

CardinalityHint = Literal["many_to_one", "one_to_one", "self_reference"]
"""Cardinalidad de una relación, derivada por el planificador estructural."""

TableKind = Literal["regular", "bridge", "lookup"]
"""Rol estructural de una tabla, inferido por `graph/dependency.py`."""

DefaultKind = Literal["literal", "expression"]
"""Si un `DEFAULT` de columna es un valor literal o una expresión/función SQL."""


class IRModel(BaseModel):
    """Base común de los modelos de la IR: no admite campos desconocidos."""

    model_config = ConfigDict(extra="forbid")


class TypeSpec(IRModel):
    """Tipo canónico de una columna: familia (`kind`) más sus parámetros."""

    kind: TypeKind
    precision: int | None = Field(default=None, description="Precisión de `numeric(p, s)`.")
    scale: int | None = Field(default=None, description="Escala de `numeric(p, s)`.")
    length: int | None = Field(default=None, description="Longitud de `varchar(n)`/`char(n)`.")
    with_timezone: bool | None = Field(
        default=None, description="Solo aplica a `kind='timestamp'`."
    )
    autoincrement: bool = Field(
        default=False, description="`True` para `serial`/`bigserial`/`smallserial`."
    )


class DefaultSpec(IRModel):
    """Valor por defecto de una columna, ya sea literal o expresión SQL."""

    kind: DefaultKind
    sql_text: str = Field(description="Texto original del DEFAULT tal como aparece en el DDL.")
    value: Any | None = Field(
        default=None, description="Valor Python del literal cuando `kind='literal'`."
    )


class CheckSpec(IRModel):
    """Restricción `CHECK`, interpretada o no, con sus cotas si se conocen."""

    sql_text: str
    ast_supported: bool = Field(
        description="`True` si el subconjunto interpretable de check_interp.py la cubre."
    )
    columns_involved: list[str] = Field(default_factory=list)
    bounds_derived: dict[str, Any] | None = Field(
        default=None,
        description="Cotas propagadas al generador, p. ej. {'min': 1900, 'max': 2026}.",
    )


class ColumnSpec(IRModel):
    """Columna de una tabla: tipo, nulabilidad, default, enum y checks propios."""

    name: str
    type: TypeSpec
    nullable: bool
    default: DefaultSpec | None = None
    enum_values: list[str] | None = None
    generated: bool = Field(
        default=False, description="`True` para `GENERATED ALWAYS AS`; se excluye de los INSERT."
    )
    comment: str | None = None
    checks: list[CheckSpec] = Field(
        default_factory=list, description="Checks que involucran únicamente a esta columna."
    )


class RelationshipSpec(IRModel):
    """Clave foránea: columnas de origen, tabla/columnas referenciadas y semántica."""

    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None
    deferrable: bool = False
    nullable: bool = Field(description="`True` si alguna columna de la FK admite NULL.")
    cardinality_hint: CardinalityHint


class TableSpec(IRModel):
    """Tabla del esquema: columnas, claves, restricciones y rol estructural."""

    name: str
    schema_: str | None = Field(
        default=None, alias="schema", description="Namespace de PostgreSQL (p. ej. 'public')."
    )
    columns: list[ColumnSpec]
    primary_key: list[str] = Field(
        default_factory=list, description="Vacía si la tabla no tiene PK."
    )
    foreign_keys: list[RelationshipSpec] = Field(default_factory=list)
    uniques: list[list[str]] = Field(
        default_factory=list, description="Un grupo de columnas por cada restricción UNIQUE."
    )
    checks: list[CheckSpec] = Field(
        default_factory=list, description="Checks de tabla, potencialmente multi-columna."
    )
    comment: str | None = None
    kind: TableKind = Field(default="regular", description="Inferido por graph/dependency.py.")

    model_config = ConfigDict(populate_by_name=True)


class GeneratorSpec(IRModel):
    """Generador asignado a una columna: id del registro más sus parámetros."""

    type: str = Field(
        description="Id del generador en el registro (faker, numeric_range, choice, "
        "datetime_range, template, sequence, uuid, derived, text_pool...). Catálogo "
        "abierto vía entry points, por eso no es un Literal cerrado."
    )
    params: dict[str, Any] = Field(default_factory=dict)
    null_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    unique: bool = False


class SchemaSpec(IRModel):
    """Raíz de la IR: dialecto, tablas y hash canónico del esquema completo."""

    dialect: str
    tables: list[TableSpec]
    hash: str | None = Field(
        default=None, description="Hash canónico (TODO(T1.5): ir/hashing.py lo calcula)."
    )
    warnings: list[str] = Field(default_factory=list)


def canonical_json(model: BaseModel) -> str:
    """Serializa un modelo de la IR a JSON canónico y estable.

    Las claves se ordenan recursivamente (a cualquier profundidad) y no se
    añade espacio en blanco, de modo que dos instancias estructuralmente
    idénticas producen siempre la misma secuencia de bytes con independencia
    del orden de declaración de campos o del orden de inserción de los
    `dict` internos (`params`, `bounds_derived`...).

    Args:
        model: Cualquier modelo Pydantic de este módulo (o que lo contenga).

    Returns:
        La representación JSON canónica como cadena de texto.
    """
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
