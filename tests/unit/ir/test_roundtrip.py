"""Round-trip JSON de la IR (T1.1, especificacion.md §5)."""

import hashlib

import pytest
from pydantic import ValidationError

from synthdb.ir.schema import (
    CheckSpec,
    ColumnSpec,
    DefaultSpec,
    GeneratorSpec,
    RelationshipSpec,
    SchemaSpec,
    TableSpec,
    TypeSpec,
    canonical_json,
)


def _build_example_schema() -> SchemaSpec:
    """Esquema de ejemplo (dominio inmobiliaria) que ejercita todos los campos.

    Incluye: los 12 `TypeKind`, ambos `DefaultKind`, las 3 cardinalidades,
    los 3 `TableKind`, las 5 acciones referenciales más `None`, PK simple y
    compuesta, UNIQUE simple y derivado de FK (1:1), checks de columna y de
    tabla (mono y multi-columna, con y sin `bounds_derived`), columna
    generada y columna con enum nativo además del patrón `CHECK ... IN`.
    """
    caracteristicas = TableSpec(
        name="caracteristicas",
        schema="public",
        kind="lookup",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="nombre", type=TypeSpec(kind="varchar", length=30), nullable=False),
        ],
        primary_key=["id"],
        uniques=[["nombre"]],
    )

    agentes = TableSpec(
        name="agentes",
        schema="public",
        kind="regular",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="nombre", type=TypeSpec(kind="text"), nullable=False),
            ColumnSpec(name="manager_id", type=TypeSpec(kind="integer"), nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            RelationshipSpec(
                columns=["manager_id"],
                ref_table="agentes",
                ref_columns=["id"],
                on_delete=None,
                on_update=None,
                deferrable=True,
                nullable=True,
                cardinality_hint="self_reference",
            ),
        ],
    )

    perfil_agente = TableSpec(
        name="perfil_agente",
        schema="public",
        kind="regular",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="agente_id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(name="telefono", type=TypeSpec(kind="varchar", length=20), nullable=True),
        ],
        primary_key=["id"],
        uniques=[["agente_id"]],
        foreign_keys=[
            RelationshipSpec(
                columns=["agente_id"],
                ref_table="agentes",
                ref_columns=["id"],
                on_delete="cascade",
                on_update="cascade",
                deferrable=False,
                nullable=False,
                cardinality_hint="one_to_one",
            ),
        ],
    )

    clientes = TableSpec(
        name="clientes",
        schema="public",
        kind="regular",
        comment="Clientes registrados",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="fecha_alta", type=TypeSpec(kind="date"), nullable=False),
            ColumnSpec(
                name="email",
                type=TypeSpec(kind="text"),
                nullable=False,
                checks=[
                    CheckSpec(
                        sql_text="char_length(email) > 3",
                        ast_supported=False,
                        columns_involved=["email"],
                        bounds_derived=None,
                    ),
                ],
            ),
            ColumnSpec(
                name="estado",
                type=TypeSpec(kind="varchar", length=20),
                nullable=False,
                enum_values=["activo", "inactivo"],
                default=DefaultSpec(kind="literal", sql_text="'activo'", value="activo"),
            ),
            ColumnSpec(
                name="canal_alta",
                type=TypeSpec(kind="enum"),
                nullable=True,
                enum_values=["web", "tienda", "telefono"],
            ),
            ColumnSpec(
                name="creado_en",
                type=TypeSpec(kind="timestamp", with_timezone=True),
                nullable=False,
                default=DefaultSpec(kind="expression", sql_text="now()", value=None),
            ),
            ColumnSpec(
                name="codigo_pais",
                type=TypeSpec(kind="char", length=2),
                nullable=True,
                comment="ISO-3166 alfa-2",
            ),
        ],
        primary_key=["id"],
        uniques=[["email"]],
        checks=[
            CheckSpec(
                sql_text="estado IN ('activo', 'inactivo')",
                ast_supported=True,
                columns_involved=["estado"],
                bounds_derived={"values": ["activo", "inactivo"]},
            ),
        ],
    )

    viviendas = TableSpec(
        name="viviendas",
        schema="public",
        kind="regular",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="propietario_id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(
                name="superficie_m2",
                type=TypeSpec(kind="numeric", precision=7, scale=2),
                nullable=False,
                checks=[
                    CheckSpec(
                        sql_text="superficie_m2 > 0",
                        ast_supported=True,
                        columns_involved=["superficie_m2"],
                        bounds_derived={"min_exclusive": 0},
                    ),
                ],
            ),
            ColumnSpec(name="ficha_tecnica", type=TypeSpec(kind="json"), nullable=True),
            ColumnSpec(name="foto", type=TypeSpec(kind="bytea"), nullable=True),
            ColumnSpec(name="token", type=TypeSpec(kind="uuid"), nullable=True),
            ColumnSpec(name="agente_id", type=TypeSpec(kind="integer"), nullable=True),
            ColumnSpec(
                name="superficie_calculada",
                type=TypeSpec(kind="numeric", precision=7, scale=2),
                nullable=True,
                generated=True,
                comment="GENERATED ALWAYS AS (superficie_m2) STORED",
            ),
        ],
        primary_key=["id"],
        foreign_keys=[
            RelationshipSpec(
                columns=["propietario_id"],
                ref_table="clientes",
                ref_columns=["id"],
                on_delete="restrict",
                on_update="no_action",
                deferrable=False,
                nullable=False,
                cardinality_hint="many_to_one",
            ),
            RelationshipSpec(
                columns=["agente_id"],
                ref_table="agentes",
                ref_columns=["id"],
                on_delete="set_null",
                on_update="cascade",
                deferrable=False,
                nullable=True,
                cardinality_hint="many_to_one",
            ),
        ],
        checks=[
            CheckSpec(
                sql_text="foto IS NULL OR ficha_tecnica IS NOT NULL",
                ast_supported=False,
                columns_involved=["foto", "ficha_tecnica"],
                bounds_derived=None,
            ),
        ],
    )

    viviendas_caracteristicas = TableSpec(
        name="viviendas_caracteristicas",
        schema="public",
        kind="bridge",
        columns=[
            ColumnSpec(name="vivienda_id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(name="caracteristica_id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(
                name="principal",
                type=TypeSpec(kind="boolean"),
                nullable=False,
                default=DefaultSpec(kind="literal", sql_text="false", value=False),
            ),
        ],
        primary_key=["vivienda_id", "caracteristica_id"],
        foreign_keys=[
            RelationshipSpec(
                columns=["vivienda_id"],
                ref_table="viviendas",
                ref_columns=["id"],
                on_delete="cascade",
                on_update="set_default",
                deferrable=False,
                nullable=False,
                cardinality_hint="many_to_one",
            ),
            RelationshipSpec(
                columns=["caracteristica_id"],
                ref_table="caracteristicas",
                ref_columns=["id"],
                on_delete="cascade",
                on_update="cascade",
                deferrable=False,
                nullable=False,
                cardinality_hint="many_to_one",
            ),
        ],
    )

    return SchemaSpec(
        dialect="postgres",
        tables=[
            caracteristicas,
            agentes,
            perfil_agente,
            clientes,
            viviendas,
            viviendas_caracteristicas,
        ],
        hash=hashlib.sha256(b"synthdb-test-fixture").hexdigest(),
        warnings=[
            "viviendas.superficie_calculada es GENERATED ALWAYS AS; se excluye de los INSERT"
        ],
    )


def test_roundtrip_is_lossless() -> None:
    schema = _build_example_schema()

    restored = SchemaSpec.model_validate_json(canonical_json(schema))

    assert restored == schema


def test_same_instance_serialized_twice_is_byte_identical() -> None:
    schema = _build_example_schema()

    first = canonical_json(schema)
    second = canonical_json(schema)

    assert first == second


def test_roundtrip_reserialization_is_byte_identical() -> None:
    schema = _build_example_schema()

    first = canonical_json(schema)
    restored = SchemaSpec.model_validate_json(first)
    second = canonical_json(restored)

    assert first == second


def test_canonical_json_sorts_keys_regardless_of_insertion_order() -> None:
    ordered = GeneratorSpec(type="choice", params={"z_last": 1, "a_first": 2})
    reordered = GeneratorSpec(type="choice", params={"a_first": 2, "z_last": 1})

    assert canonical_json(ordered) == canonical_json(reordered)
    payload = canonical_json(ordered)
    assert payload.index('"a_first"') < payload.index('"z_last"')


def test_table_spec_schema_field_round_trips_under_its_sql_name() -> None:
    schema = _build_example_schema()

    payload = canonical_json(schema)

    assert '"schema":"public"' in payload
    assert "schema_" not in payload


@pytest.mark.parametrize(
    "model_cls,minimal_kwargs",
    [
        (SchemaSpec, {"dialect": "postgres", "tables": []}),
        (
            TableSpec,
            {"name": "t", "columns": [{"name": "c", "type": {"kind": "text"}, "nullable": True}]},
        ),
        (ColumnSpec, {"name": "c", "type": {"kind": "text"}, "nullable": True}),
        (TypeSpec, {"kind": "text"}),
        (GeneratorSpec, {"type": "faker"}),
    ],
)
def test_unknown_field_is_rejected_not_silently_ignored(model_cls, minimal_kwargs) -> None:
    with pytest.raises(ValidationError):
        model_cls.model_validate({**minimal_kwargs, "campo_inventado": True})


def test_generator_spec_roundtrip() -> None:
    generator = GeneratorSpec(
        type="numeric_range",
        params={
            "min": 35,
            "max": 450,
            "distribution": {"family": "lognormal", "params": {"median": 90, "sigma": 0.45}},
        },
        null_ratio=0.05,
        unique=False,
    )

    payload = canonical_json(generator)
    restored = GeneratorSpec.model_validate_json(payload)

    assert restored == generator
    assert canonical_json(restored) == payload
