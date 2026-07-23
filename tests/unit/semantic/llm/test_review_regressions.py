from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from synthdb.ir.schema import (
    ColumnSpec,
    RelationshipSpec,
    SchemaSpec,
    TableSpec,
    TypeSpec,
)
from synthdb.semantic.llm.contract import (
    MAX_SEMANTIC_PROPOSAL_BYTES,
    SemanticProposal,
    relationship_identifier,
    validate_proposal_against_schema,
    validate_semantic_proposal_json,
)
from synthdb.semantic.plan_artifact import (
    ArtifactDiagnostics,
    ResolvedPlanArtifact,
    ResolvedTablePlan,
)


def _schema_with_column(
    *,
    kind: str,
    nullable: bool = False,
    enum_values: list[str] | None = None,
    unique: bool = False,
) -> SchemaSpec:
    return SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind=kind),  # type: ignore[arg-type]
                        nullable=nullable,
                        enum_values=enum_values,
                    )
                ],
                uniques=[["value"]] if unique else [],
            )
        ],
    )


def _proposal_payload(generator: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "semantic-proposal/1",
        "schema_hash": "e" * 64,
        "tables": [
            {
                "table_name": "items",
                "entity": "item",
                "confidence": 0.9,
                "evidence": [{"kind": "table_name", "table_name": "items"}],
                "columns": [
                    {
                        "table_name": "items",
                        "column_name": "value",
                        "semantic_role": "valor",
                        "generator": generator,
                        "confidence": 0.8,
                        "evidence": [
                            {
                                "kind": "column_name",
                                "table_name": "items",
                                "column_name": "value",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _diagnostics() -> ArtifactDiagnostics:
    return ArtifactDiagnostics(created_at=datetime(2026, 7, 23, tzinfo=UTC))


def _resolved_table(
    generator: dict[str, Any] | None,
    *,
    source: str = "llm",
    confidence: float = 0.9,
    role: str | None = "valor",
    schema_name: str | None = None,
    table_name: str = "items",
    column_name: str = "value",
) -> ResolvedTablePlan:
    column_payload: dict[str, Any] = {
        "table_name": table_name,
        "column_name": column_name,
        "generator": generator,
        "source": source,
        "confidence": confidence,
        "role": role,
    }
    table_payload: dict[str, Any] = {
        "table_name": table_name,
        "columns": [column_payload],
    }
    if schema_name is not None:
        column_payload["schema_name"] = schema_name
        table_payload["schema_name"] = schema_name
    return ResolvedTablePlan.model_validate(table_payload)


@pytest.mark.parametrize(
    ("schema", "generator"),
    [
        (
            _schema_with_column(kind="text"),
            {"type": "numeric_range", "params": {"min": 0, "max": 1}},
        ),
        (
            _schema_with_column(kind="enum", enum_values=["permitido"]),
            {"type": "choice", "params": {"values": ["prohibido"]}},
        ),
    ],
    ids=["numeric_on_text", "choice_outside_enum"],
)
def test_semantic_proposal_rejects_generator_or_domain_incompatible_with_ir(
    schema: SchemaSpec, generator: dict[str, Any]
) -> None:
    proposal = SemanticProposal.model_validate(_proposal_payload(generator))

    with pytest.raises(ValueError, match="incompatible|dominio"):
        validate_proposal_against_schema(proposal, schema)


@pytest.mark.parametrize(
    ("kind", "level"),
    [("table_comment", "table"), ("constraint", "column")],
)
def test_evidence_must_refer_to_content_that_really_exists(kind: str, level: str) -> None:
    payload = _proposal_payload({"type": "choice", "params": {"values": ["permitido"]}})
    evidence = {"kind": kind, "table_name": "items"}
    if level == "column":
        evidence["column_name"] = "value"
        payload["tables"][0]["columns"][0]["evidence"] = [evidence]
    else:
        payload["tables"][0]["evidence"] = [evidence]
    proposal = SemanticProposal.model_validate(payload)

    schema = (
        _schema_with_column(kind="text")
        if kind == "constraint"
        else _schema_with_column(kind="enum", enum_values=["permitido"])
    )
    with pytest.raises(ValueError, match="evidencia"):
        validate_proposal_against_schema(proposal, schema)


def test_parent_reference_must_resolve_a_real_fk_and_parent_column() -> None:
    payload = _proposal_payload({"type": "faker", "params": {"provider": "email"}})
    payload["tables"][0]["columns"][0]["rules"] = [
        {
            "rule_id": "parent_inventado",
            "table_name": "items",
            "target_column": "value",
            "kind": "derivation",
            "expression": "value = parent(fake_fk).missing",
            "rationale": "Sonda de referencia estructural inventada.",
            "confidence": 0.8,
            "evidence": [
                {
                    "kind": "column_name",
                    "table_name": "items",
                    "column_name": "value",
                }
            ],
        }
    ]
    proposal = SemanticProposal.model_validate(payload)
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                name="parents",
                columns=[
                    ColumnSpec(
                        name="id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            ),
            TableSpec(
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind="text"),
                        nullable=False,
                    ),
                    ColumnSpec(
                        name="parent_id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    ),
                ],
                foreign_keys=[
                    RelationshipSpec(
                        columns=["parent_id"],
                        ref_table="parents",
                        ref_columns=["id"],
                        nullable=False,
                    )
                ],
            ),
        ],
    )

    with pytest.raises(ValueError, match=r"parent\(fake_fk\)"):
        validate_proposal_against_schema(proposal, schema)


def test_parent_reference_accepts_a_real_fk_and_rejects_missing_parent_column() -> None:
    payload = _proposal_payload({"type": "faker", "params": {"provider": "email"}})
    rule = {
        "rule_id": "parent_real",
        "table_name": "items",
        "target_column": "value",
        "kind": "derivation",
        "expression": "value = parent(parent_id).id",
        "rationale": "Referencia estructural real.",
        "confidence": 0.8,
        "evidence": [
            {
                "kind": "column_name",
                "table_name": "items",
                "column_name": "value",
            }
        ],
    }
    payload["tables"][0]["columns"][0]["rules"] = [rule]
    relationship = RelationshipSpec(
        columns=["parent_id"],
        ref_table="parents",
        ref_columns=["id"],
        nullable=False,
    )
    items = TableSpec(
        name="items",
        columns=[
            ColumnSpec(name="value", type=TypeSpec(kind="text"), nullable=False),
            ColumnSpec(
                name="parent_id",
                type=TypeSpec(kind="integer"),
                nullable=False,
            ),
        ],
        foreign_keys=[relationship],
    )
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                name="parents",
                columns=[
                    ColumnSpec(
                        name="id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            ),
            items,
        ],
    )

    valid = SemanticProposal.model_validate(payload)
    assert validate_proposal_against_schema(valid, schema) is valid

    payload["tables"][0]["columns"][0]["rules"][0]["expression"] = (
        "value = parent(parent_id).missing"
    )
    invalid = SemanticProposal.model_validate(payload)
    with pytest.raises(ValueError, match="no contiene esa columna"):
        validate_proposal_against_schema(invalid, schema)


def test_relationship_identifiers_include_owner_namespace() -> None:
    relationship = RelationshipSpec(
        columns=["parent_id"],
        ref_table="parents",
        ref_columns=["id"],
        nullable=False,
    )

    assert relationship_identifier(
        TableSpec(schema="a", name="items", columns=[], foreign_keys=[relationship]),
        relationship,
    ) != relationship_identifier(
        TableSpec(schema="b", name="items", columns=[], foreign_keys=[relationship]),
        relationship,
    )


def test_proposal_can_distinguish_homonymous_tables_by_schema() -> None:
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                schema="a",
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            ),
            TableSpec(
                schema="b",
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            ),
        ],
    )
    tables: list[dict[str, Any]] = []
    for schema_name in ("a", "b"):
        table = _proposal_payload({"type": "numeric_range", "params": {"min": 0, "max": 10}})[
            "tables"
        ][0]
        table["schema_name"] = schema_name
        table["evidence"][0]["schema_name"] = schema_name
        table["columns"][0]["schema_name"] = schema_name
        table["columns"][0]["evidence"][0]["schema_name"] = schema_name
        tables.append(table)
    payload = _proposal_payload({"type": "numeric_range", "params": {"min": 0, "max": 10}})
    payload["tables"] = tables

    proposal = SemanticProposal.model_validate(payload)

    assert validate_proposal_against_schema(proposal, schema) is proposal
    assert [(table.schema_name, table.table_name) for table in proposal.tables] == [
        ("a", "items"),
        ("b", "items"),
    ]


def test_resolved_artifact_rejects_generator_incompatible_with_column_type() -> None:
    schema = _schema_with_column(kind="text")
    table = _resolved_table(
        {
            "type": "numeric_range",
            "params": {"min": 0, "max": 1},
        }
    )

    with pytest.raises(ValueError, match="incompatible"):
        ResolvedPlanArtifact.create(
            schema=schema,
            tables=[table],
            diagnostics=_diagnostics(),
        )


def test_choice_rejects_value_outside_numeric_precision() -> None:
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind="numeric", precision=3, scale=2),
                        nullable=False,
                    )
                ],
            )
        ],
    )
    proposal = SemanticProposal.model_validate(
        _proposal_payload(
            {"type": "choice", "params": {"values": [1000]}},
        )
    )

    with pytest.raises(ValueError, match="incompatibles"):
        validate_proposal_against_schema(proposal, schema)


def test_resolved_artifact_only_allows_none_for_database_managed_columns() -> None:
    schema = _schema_with_column(kind="text")
    table = _resolved_table(None, source="ir", confidence=1.0, role=None)

    with pytest.raises(ValueError, match="autoincremental|GENERATED"):
        ResolvedPlanArtifact.create(
            schema=schema,
            tables=[table],
            diagnostics=_diagnostics(),
        )


@pytest.mark.parametrize(
    "column",
    [
        ColumnSpec(
            name="value",
            type=TypeSpec(kind="integer", autoincrement=True),
            nullable=False,
        ),
        ColumnSpec(
            name="value",
            type=TypeSpec(kind="integer"),
            nullable=False,
            generated=True,
        ),
    ],
    ids=["autoincrement", "generated"],
)
def test_resolved_artifact_allows_none_for_database_managed_columns(
    column: ColumnSpec,
) -> None:
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[TableSpec(name="items", columns=[column])],
    )

    artifact = ResolvedPlanArtifact.create(
        schema=schema,
        tables=[
            _resolved_table(
                None,
                source="ir",
                confidence=1.0,
                role=None,
            )
        ],
        diagnostics=_diagnostics(),
    )

    assert artifact.tables[0].columns[0].generator is None


def test_resolved_artifact_rejects_non_fk_generator_for_fk_column() -> None:
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                name="parents",
                columns=[
                    ColumnSpec(
                        name="id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
                primary_key=["id"],
            ),
            TableSpec(
                name="items",
                columns=[
                    ColumnSpec(
                        name="parent_id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
                foreign_keys=[
                    RelationshipSpec(
                        columns=["parent_id"],
                        ref_table="parents",
                        ref_columns=["id"],
                        nullable=False,
                    )
                ],
            ),
        ],
    )
    tables = [
        _resolved_table(
            {
                "type": "numeric_range",
                "params": {"min": 1, "max": 100},
                "unique": True,
            },
            table_name="parents",
            column_name="id",
        ),
        _resolved_table(
            {
                "type": "numeric_range",
                "params": {"min": 1, "max": 100},
            },
            table_name="items",
            column_name="parent_id",
        ),
    ]

    with pytest.raises(ValueError, match="columna FK"):
        ResolvedPlanArtifact.create(
            schema=schema,
            tables=tables,
            diagnostics=_diagnostics(),
        )

    valid_tables = [
        tables[0],
        _resolved_table(
            {
                "type": "fk",
                "params": {"strategy": "uniform"},
            },
            source="ir",
            confidence=1.0,
            role="fk",
            table_name="items",
            column_name="parent_id",
        ),
    ]
    artifact = ResolvedPlanArtifact.create(
        schema=schema,
        tables=valid_tables,
        diagnostics=_diagnostics(),
    )
    assert artifact.tables[1].columns[0].generator is not None
    assert artifact.tables[1].columns[0].generator.type == "fk"


@pytest.mark.parametrize(
    ("schema", "generator", "message"),
    [
        (
            _schema_with_column(kind="text", nullable=False),
            {
                "type": "faker",
                "params": {"provider": "email"},
                "null_ratio": 0.1,
            },
            "NOT NULL",
        ),
        (
            _schema_with_column(kind="integer", unique=True),
            {
                "type": "numeric_range",
                "params": {"min": 0, "max": 10},
                "unique": False,
            },
            "UNIQUE",
        ),
        (
            _schema_with_column(kind="enum", enum_values=["permitido"]),
            {
                "type": "choice",
                "params": {"values": ["prohibido"]},
            },
            "dominio",
        ),
    ],
    ids=["not_null", "unique", "enum_domain"],
)
def test_resolved_artifact_enforces_ir_nullability_uniqueness_and_domain(
    schema: SchemaSpec, generator: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ResolvedPlanArtifact.create(
            schema=schema,
            tables=[_resolved_table(generator)],
            diagnostics=_diagnostics(),
        )


def test_resolved_artifact_represents_homonymous_tables_without_collision() -> None:
    schema = SchemaSpec(
        dialect="postgres",
        hash="e" * 64,
        tables=[
            TableSpec(
                schema=schema_name,
                name="items",
                columns=[
                    ColumnSpec(
                        name="value",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            )
            for schema_name in ("a", "b")
        ],
    )
    tables = [
        _resolved_table(
            {
                "type": "numeric_range",
                "params": {"min": 0, "max": 10},
            },
            schema_name=schema_name,
        )
        for schema_name in ("a", "b")
    ]

    artifact = ResolvedPlanArtifact.create(
        schema=schema,
        tables=tables,
        diagnostics=_diagnostics(),
    )

    assert [(table.schema_name, table.table_name) for table in artifact.tables] == [
        ("a", "items"),
        ("b", "items"),
    ]
    with pytest.raises(ValueError, match="TablePlans"):
        artifact.to_table_plans(schema)


def test_fingerprint_covers_all_runtime_semantics_versions() -> None:
    artifact = ResolvedPlanArtifact.create(
        schema=_schema_with_column(kind="text"),
        tables=[
            _resolved_table(
                {"type": "faker", "params": {"provider": "email"}},
            )
        ],
        diagnostics=_diagnostics(),
    )
    payload = artifact.model_dump(mode="json")

    assert payload["rule_dsl_version"] == "rule-dsl/1"
    assert payload["generator_catalog_version"] == "generator-catalog/1"
    assert payload["seed_derivation_version"] == "seed-derivation/1"


def test_audit_fields_do_not_change_execution_fingerprint_but_remain_sealed() -> None:
    schema = _schema_with_column(kind="text")
    generator = {"type": "faker", "params": {"provider": "email"}}
    first = ResolvedPlanArtifact.create(
        schema=schema,
        tables=[
            _resolved_table(
                generator,
                source="llm",
                confidence=0.9,
                role="email",
            )
        ],
        diagnostics=_diagnostics(),
    )
    second = ResolvedPlanArtifact.create(
        schema=schema,
        tables=[
            _resolved_table(
                generator,
                source="heuristic",
                confidence=0.7,
                role="correo",
            )
        ],
        diagnostics=_diagnostics(),
    )

    assert first.fingerprint == second.fingerprint
    assert first.audit_fingerprint != second.audit_fingerprint


def test_audit_field_tampering_is_rejected_by_audit_fingerprint() -> None:
    artifact = ResolvedPlanArtifact.create(
        schema=_schema_with_column(kind="text"),
        tables=[
            _resolved_table(
                {"type": "faker", "params": {"provider": "email"}},
            )
        ],
        diagnostics=_diagnostics(),
    )
    payload = artifact.model_dump(mode="json")
    payload["tables"][0]["columns"][0]["role"] = "manipulado"

    with pytest.raises(ValidationError, match="audit_fingerprint"):
        ResolvedPlanArtifact.model_validate(payload)


def _set_confidence_string(payload: dict[str, Any]) -> None:
    payload["tables"][0]["confidence"] = "0.8"


def _set_numeric_string(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["generator"]["params"]["min"] = "1"


@pytest.mark.parametrize(
    "mutate",
    [_set_confidence_string, _set_numeric_string],
    ids=["confidence_string", "numeric_string"],
)
def test_proposal_rejects_numbers_encoded_as_strings(
    mutate: Any,
) -> None:
    payload = _proposal_payload({"type": "numeric_range", "params": {"min": 0, "max": 10}})
    mutate(payload)

    with pytest.raises(ValidationError):
        SemanticProposal.model_validate(payload)


def test_proposal_rejects_oversized_scalar_string() -> None:
    payload = _proposal_payload({"type": "choice", "params": {"values": ["x" * 2_000_000]}})

    with pytest.raises(ValidationError, match="at most|demasiado|longitud"):
        SemanticProposal.model_validate(payload)


def test_provider_boundary_rejects_oversized_json_before_parsing() -> None:
    oversized = b" " * (MAX_SEMANTIC_PROPOSAL_BYTES + 1)

    with pytest.raises(ValueError, match="proveedor supera el límite"):
        validate_semantic_proposal_json(oversized)


def test_proposal_rejects_aggregate_payload_over_global_limit() -> None:
    payload = _proposal_payload(
        {
            "type": "choice",
            "params": {"values": [f"{index:03d}" + "x" * 4090 for index in range(300)]},
        }
    )

    with pytest.raises(ValidationError, match="supera el límite"):
        SemanticProposal.model_validate(payload)


def test_choice_rejects_duplicate_values() -> None:
    payload = _proposal_payload({"type": "choice", "params": {"values": ["same", "same"]}})

    with pytest.raises(ValidationError, match="duplicados"):
        SemanticProposal.model_validate(payload)


@pytest.mark.parametrize(
    "generator",
    [
        {
            "type": "numeric_range",
            "params": {"min": 1, "max": 1, "min_exclusive": True},
        },
        {
            "type": "datetime_range",
            "params": {"min": "2030-01-01", "max": "2020-01-01"},
        },
    ],
    ids=["empty_numeric", "inverted_datetime"],
)
def test_proposal_rejects_empty_or_inverted_ranges(generator: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SemanticProposal.model_validate(_proposal_payload(generator))
