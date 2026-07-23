from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from synthdb.ir.schema import (
    CheckSpec,
    ColumnSpec,
    RelationshipSpec,
    SchemaSpec,
    TableSpec,
    TypeSpec,
)
from synthdb.semantic.llm.contract import (
    SemanticProposal,
    relationship_identifier,
    validate_proposal_against_schema,
)

_CASES_PATH = Path(__file__).parent / "fixtures" / "contract_cases.json"
_CASES: list[dict[str, Any]] = json.loads(_CASES_PATH.read_text("utf-8"))


def _case(name: str) -> dict[str, Any]:
    return copy.deepcopy(next(case["payload"] for case in _CASES if case["name"] == name))


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_contract_fixtures_are_classified_as_expected(case: dict[str, Any]) -> None:
    if case["valid"]:
        SemanticProposal.model_validate(case["payload"])
    else:
        with pytest.raises(ValidationError):
            SemanticProposal.model_validate(case["payload"])


def test_minimal_proposal_is_valid_and_keeps_generator_params_typed() -> None:
    proposal = SemanticProposal.model_validate(_case("minimal_valid"))

    column = proposal.tables[0].columns[0]
    assert proposal.version == "semantic-proposal/1"
    assert column.table_name == "clientes"
    assert column.generator is not None
    assert column.generator.type == "faker"
    assert column.generator.params.provider == "email"


@pytest.mark.parametrize(
    "case_name",
    [
        "extra_field",
        "unknown_generator",
        "extra_generator_param",
        "wrong_owner",
        "duplicate_column",
        "invalid_rule_shape",
        "invalid_fk_identifier",
        "unknown_version",
        "forbidden_execution_fields",
    ],
)
def test_invalid_contract_case_is_rejected(case_name: str) -> None:
    with pytest.raises(ValidationError):
        SemanticProposal.model_validate(_case(case_name))


def test_schema_boundary_rejects_unknown_table_without_mutating_ir() -> None:
    proposal_payload = _case("minimal_valid")
    proposal_payload["tables"][0]["table_name"] = "inventada"
    proposal_payload["tables"][0]["columns"][0]["table_name"] = "inventada"
    proposal = SemanticProposal.model_validate(proposal_payload)
    schema = SchemaSpec(
        dialect="postgres",
        hash="a" * 64,
        tables=[
            TableSpec(
                name="clientes",
                columns=[
                    ColumnSpec(
                        name="email",
                        type=TypeSpec(kind="text"),
                        nullable=False,
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="inventada"):
        validate_proposal_against_schema(proposal, schema)

    assert [table.name for table in schema.tables] == ["clientes"]


def test_relationship_hint_must_identify_an_existing_ir_relationship() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["relationship_hints"] = [
        {
            "table_name": "clientes",
            "relationship_id": f"fk:{'c' * 64}",
            "strategy": {"strategy": "uniform"},
            "confidence": 0.7,
            "evidence": [
                {
                    "kind": "constraint",
                    "table_name": "clientes",
                    "column_name": "empresa_id",
                }
            ],
            "uncertainties": [],
        }
    ]
    proposal = SemanticProposal.model_validate(payload)
    schema = SchemaSpec(
        dialect="postgres",
        hash="a" * 64,
        tables=[
            TableSpec(
                name="clientes",
                columns=[
                    ColumnSpec(
                        name="email",
                        type=TypeSpec(kind="text"),
                        nullable=False,
                    ),
                    ColumnSpec(
                        name="empresa_id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    ),
                ],
                foreign_keys=[
                    RelationshipSpec(
                        columns=["empresa_id"],
                        ref_table="organizaciones",
                        ref_columns=["id"],
                        nullable=False,
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="relationship_id"):
        validate_proposal_against_schema(proposal, schema)


def test_valid_rule_and_relationship_hint_remain_auditable_proposals() -> None:
    relationship = RelationshipSpec(
        columns=["cliente_id"],
        ref_table="clientes",
        ref_columns=["id"],
        nullable=False,
    )
    pedidos = TableSpec(
        name="pedidos",
        columns=[
            ColumnSpec(
                name="cliente_id",
                type=TypeSpec(kind="integer"),
                nullable=False,
            ),
            ColumnSpec(
                name="total",
                type=TypeSpec(kind="numeric"),
                nullable=False,
                checks=[
                    CheckSpec(
                        sql_text="total >= 0",
                        ast_supported=True,
                        columns_involved=["total"],
                        bounds_derived={"min": 0, "min_exclusive": False},
                    )
                ],
            ),
        ],
        foreign_keys=[relationship],
    )
    schema = SchemaSpec(
        dialect="postgres",
        hash="d" * 64,
        tables=[
            TableSpec(
                name="clientes",
                columns=[
                    ColumnSpec(
                        name="id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
            ),
            pedidos,
        ],
    )
    proposal = SemanticProposal.model_validate(
        {
            "version": "semantic-proposal/1",
            "schema_hash": "d" * 64,
            "tables": [
                {
                    "table_name": "pedidos",
                    "entity": "pedido",
                    "confidence": 0.9,
                    "evidence": [{"kind": "table_name", "table_name": "pedidos"}],
                    "columns": [
                        {
                            "table_name": "pedidos",
                            "column_name": "total",
                            "semantic_role": "importe_total",
                            "generator": {
                                "type": "numeric_range",
                                "params": {"min": 0, "max": 10000},
                            },
                            "confidence": 0.8,
                            "evidence": [
                                {
                                    "kind": "column_name",
                                    "table_name": "pedidos",
                                    "column_name": "total",
                                }
                            ],
                            "rules": [
                                {
                                    "rule_id": "total_positivo",
                                    "table_name": "pedidos",
                                    "target_column": "total",
                                    "kind": "consistency",
                                    "expression": "total >= 0",
                                    "rationale": "Un pedido no tiene total negativo.",
                                    "confidence": 0.8,
                                    "evidence": [
                                        {
                                            "kind": "constraint",
                                            "table_name": "pedidos",
                                            "column_name": "total",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "relationship_hints": [
                        {
                            "table_name": "pedidos",
                            "relationship_id": relationship_identifier(pedidos, relationship),
                            "strategy": {"strategy": "zipf", "s": 1.1},
                            "confidence": 0.7,
                            "evidence": [
                                {
                                    "kind": "constraint",
                                    "table_name": "pedidos",
                                    "column_name": "cliente_id",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    validated = validate_proposal_against_schema(proposal, schema)

    assert validated.tables[0].columns[0].rules[0].expression == "total >= 0"
    assert validated.tables[0].relationship_hints[0].strategy.strategy == "zipf"


def test_contract_json_schema_forbids_additional_properties_recursively() -> None:
    schema = SemanticProposal.model_json_schema()

    assert schema["additionalProperties"] is False
    assert all(
        definition.get("additionalProperties") is False
        for definition in schema["$defs"].values()
        if definition.get("type") == "object"
    )


def test_invalid_calendar_date_is_rejected() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["columns"][0]["generator"] = {
        "type": "datetime_range",
        "params": {"min": "2026-02-30"},
    }

    with pytest.raises(ValidationError, match="ISO inválida"):
        SemanticProposal.model_validate(payload)


def test_model_cannot_assign_a_generator_to_a_foreign_key() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["columns"][0]["column_name"] = "empresa_id"
    payload["tables"][0]["columns"][0]["semantic_role"] = "fk"
    payload["tables"][0]["columns"][0]["evidence"][0]["column_name"] = "empresa_id"
    proposal = SemanticProposal.model_validate(payload)
    schema = SchemaSpec(
        dialect="postgres",
        hash="a" * 64,
        tables=[
            TableSpec(
                name="clientes",
                columns=[
                    ColumnSpec(
                        name="empresa_id",
                        type=TypeSpec(kind="integer"),
                        nullable=False,
                    )
                ],
                foreign_keys=[
                    RelationshipSpec(
                        columns=["empresa_id"],
                        ref_table="empresas",
                        ref_columns=["id"],
                        nullable=False,
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="modelo no puede proponerle"):
        validate_proposal_against_schema(proposal, schema)


def test_rule_cannot_reference_an_unknown_local_column() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["columns"][0]["rules"] = [
        {
            "rule_id": "email_coherente",
            "table_name": "clientes",
            "target_column": "email",
            "kind": "consistency",
            "expression": "email = columna_inventada",
            "rationale": "Hipótesis deliberadamente inválida.",
            "confidence": 0.5,
            "evidence": [
                {
                    "kind": "column_name",
                    "table_name": "clientes",
                    "column_name": "email",
                }
            ],
        }
    ]
    proposal = SemanticProposal.model_validate(payload)
    schema = SchemaSpec(
        dialect="postgres",
        hash="a" * 64,
        tables=[
            TableSpec(
                name="clientes",
                columns=[
                    ColumnSpec(
                        name="email",
                        type=TypeSpec(kind="text"),
                        nullable=False,
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="columna_inventada"):
        validate_proposal_against_schema(proposal, schema)


def test_template_rejects_format_specifiers() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["columns"][0]["generator"] = {
        "type": "template",
        "params": {"template": "{n:1000000}"},
    }

    with pytest.raises(ValidationError, match="especificadores"):
        SemanticProposal.model_validate(payload)


def test_non_finite_number_is_rejected() -> None:
    payload = _case("minimal_valid")
    payload["tables"][0]["columns"][0]["confidence"] = float("nan")

    with pytest.raises(ValidationError):
        SemanticProposal.model_validate(payload)
