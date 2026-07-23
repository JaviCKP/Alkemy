from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from synthdb.ir.plans import ColumnPlan, TablePlan, TablePlans
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.semantic.plan_artifact import (
    ArtifactDiagnostics,
    ResolvedColumnPlan,
    ResolvedPlanArtifact,
    ResolvedTablePlan,
)


def _schema() -> SchemaSpec:
    return SchemaSpec(
        dialect="postgres",
        hash="b" * 64,
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


def _artifact(
    *,
    provider: str = "email",
    created_at: datetime | None = None,
    input_tokens: int = 120,
    latency_ms: float = 15.5,
) -> ResolvedPlanArtifact:
    return ResolvedPlanArtifact.create(
        schema=_schema(),
        tables=[
            ResolvedTablePlan(
                table_name="clientes",
                columns=[
                    ResolvedColumnPlan(
                        table_name="clientes",
                        column_name="email",
                        generator={
                            "type": "faker",
                            "params": {"provider": provider},
                            "null_ratio": 0.0,
                            "unique": True,
                        },
                        source="llm",
                        confidence=0.92,
                        role="email",
                    )
                ],
            )
        ],
        diagnostics=ArtifactDiagnostics(
            created_at=created_at or datetime(2026, 7, 23, 8, 30, tzinfo=UTC),
            input_tokens=input_tokens,
            output_tokens=80,
            latency_ms=latency_ms,
            messages=["propuesta aceptada tras validar contra la IR"],
        ),
    )


def test_canonical_roundtrip_is_byte_stable() -> None:
    artifact = _artifact()

    encoded = artifact.canonical_bytes()
    decoded = ResolvedPlanArtifact.model_validate_json(encoded)

    assert decoded.canonical_bytes() == encoded
    assert decoded.fingerprint == artifact.fingerprint


@pytest.mark.parametrize(
    ("provider_a", "provider_b"),
    [("email", "name"), ("name", "street_address")],
)
def test_executable_change_alters_fingerprint(provider_a: str, provider_b: str) -> None:
    assert _artifact(provider=provider_a).fingerprint != _artifact(provider=provider_b).fingerprint


def test_timestamp_tokens_latency_and_diagnostics_do_not_alter_fingerprint() -> None:
    first = _artifact()
    second = _artifact(
        created_at=datetime(2026, 7, 24, 9, 45, tzinfo=UTC),
        input_tokens=999,
        latency_ms=9999.0,
    )
    payload = second.model_dump(mode="json")
    payload["diagnostics"]["messages"] = ["texto diagnóstico completamente distinto"]
    second_with_new_text = ResolvedPlanArtifact.model_validate(payload)

    assert second.fingerprint == first.fingerprint
    assert second_with_new_text.fingerprint == first.fingerprint


def _change_schema_hash(payload: dict[str, Any]) -> None:
    payload["schema_hash"] = "c" * 64


def _change_table(payload: dict[str, Any]) -> None:
    payload["tables"][0]["table_name"] = "empresas"
    payload["tables"][0]["columns"][0]["table_name"] = "empresas"


def _change_column(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["column_name"] = "correo"


def _change_provider(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["generator"]["params"]["provider"] = "name"


def _change_null_ratio(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["generator"]["null_ratio"] = 0.2


def _change_unique(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["generator"]["unique"] = False


def _change_source(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["source"] = "heuristic"


def _change_confidence(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["confidence"] = 0.8


def _change_role(payload: dict[str, Any]) -> None:
    payload["tables"][0]["columns"][0]["role"] = "correo"


@pytest.mark.parametrize(
    "tamper",
    [
        _change_schema_hash,
        _change_table,
        _change_column,
        _change_provider,
        _change_null_ratio,
        _change_unique,
        _change_source,
        _change_confidence,
        _change_role,
    ],
    ids=[
        "schema_hash",
        "table",
        "column",
        "generator_params",
        "null_ratio",
        "unique",
        "source",
        "confidence",
        "role",
    ],
)
def test_tampered_resolved_field_is_rejected(
    tamper: Callable[[dict[str, Any]], None],
) -> None:
    payload: dict[str, Any] = json.loads(_artifact().canonical_bytes())
    tamper(payload)

    with pytest.raises(ValidationError, match="fingerprint"):
        ResolvedPlanArtifact.model_validate(payload)


def test_unknown_artifact_or_canonicalization_version_is_rejected() -> None:
    payload = _artifact().model_dump(mode="json")
    payload["version"] = "resolved-plan/99"
    with pytest.raises(ValidationError):
        ResolvedPlanArtifact.model_validate(payload)

    payload = _artifact().model_dump(mode="json")
    payload["canonicalization_version"] = "plan-canonicalization/99"
    with pytest.raises(ValidationError):
        ResolvedPlanArtifact.model_validate(payload)


def test_resolved_plan_converts_to_existing_ir_plan_models() -> None:
    converted = _artifact().to_table_plans(_schema())

    assert converted == TablePlans(
        tables=[
            TablePlan(
                table="clientes",
                columns=[
                    ColumnPlan(
                        column="email",
                        generator={
                            "type": "faker",
                            "params": {"provider": "email"},
                            "null_ratio": 0.0,
                            "unique": True,
                        },
                        source="llm",
                        confidence=0.92,
                        role="email",
                    )
                ],
            )
        ]
    )


def test_existing_ir_plan_payload_remains_compatible() -> None:
    legacy = {
        "tables": [
            {
                "table": "t",
                "columns": [
                    {
                        "column": "value",
                        "source": "fallback",
                        "confidence": 0.0,
                    }
                ],
            }
        ]
    }

    plan = TablePlans.model_validate(legacy)

    assert plan.tables[0].columns[0].generator is None
    assert plan.model_dump(mode="json")["tables"][0]["columns"][0]["role"] is None


def test_resolved_table_rejects_duplicate_columns_and_wrong_owner() -> None:
    column = ResolvedColumnPlan(
        table_name="clientes",
        column_name="email",
        generator=None,
        source="ir",
        confidence=1.0,
    )
    with pytest.raises(ValidationError, match="duplicada"):
        ResolvedTablePlan(
            table_name="clientes",
            columns=[column, column],
        )

    with pytest.raises(ValidationError, match="propietaria"):
        ResolvedTablePlan(
            table_name="empresas",
            columns=[column],
        )


def test_database_managed_column_requires_ir_authority() -> None:
    with pytest.raises(ValidationError, match="gestionada por la IR"):
        ResolvedColumnPlan(
            table_name="clientes",
            column_name="email",
            generator=None,
            source="llm",
            confidence=0.9,
        )


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="zona horaria"):
        _artifact(created_at=datetime(2026, 7, 23, 8, 30))


def test_non_finite_diagnostic_number_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _artifact(latency_ms=float("nan"))


def test_diagnostic_timestamp_change_keeps_canonical_roundtrip() -> None:
    artifact = _artifact()
    later = _artifact(created_at=artifact.diagnostics.created_at + timedelta(days=1))

    assert ResolvedPlanArtifact.model_validate_json(later.canonical_bytes()) == later


def test_artifact_cannot_omit_or_invent_structural_columns() -> None:
    invented = ResolvedTablePlan(
        table_name="clientes",
        columns=[
            ResolvedColumnPlan(
                table_name="clientes",
                column_name="inventada",
                generator=None,
                source="ir",
                confidence=1.0,
            )
        ],
    )

    with pytest.raises(ValueError, match="no coinciden exactamente"):
        ResolvedPlanArtifact.create(
            schema=_schema(),
            tables=[invented],
            diagnostics=ArtifactDiagnostics(created_at=datetime(2026, 7, 23, tzinfo=UTC)),
        )
