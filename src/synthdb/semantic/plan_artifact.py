"""Artefacto semántico resuelto, canónico y protegido por fingerprint.

La propuesta del modelo vive en ``semantic.llm.contract`` y nunca se ejecuta.
Este módulo define una segunda forma deliberadamente distinta:
``ResolvedPlanArtifact``. Sus generadores ya usan los modelos de parámetros del
motor actual y pueden convertirse al ``TablePlans`` que este consume.

``fingerprint`` cubre versiones, hash de la IR y la proyección ejecutable.
``audit_fingerprint`` añade fuente, confianza y rol para sellar la trazabilidad.
``ArtifactDiagnostics`` queda fuera de ambas: fecha, tokens, latencia y mensajes
sirven para observabilidad, no cambian la ejecución ni la decisión resuelta.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from synthdb.config.models import FkStrategy, FkUniform
from synthdb.generation.generators.base import GeneratorParams
from synthdb.generation.generators.derived import DerivedParams
from synthdb.generation.generators.faker_gen import FakerParams
from synthdb.generation.generators.numeric import NumericRangeParams, SequenceParams
from synthdb.generation.generators.temporal import DatetimeRangeParams
from synthdb.generation.generators.text import (
    ChoiceParams,
    FallbackParams,
    TemplateParams,
    UuidParams,
)
from synthdb.ir.plans import ColumnPlan, PlanSource, TablePlan, TablePlans
from synthdb.ir.schema import GeneratorSpec, SchemaSpec
from synthdb.semantic.compatibility import (
    TableIdentity,
    identity_text,
    table_identity,
    validate_generator_compatibility,
)

RESOLVED_PLAN_VERSION: Literal["resolved-plan/1"] = "resolved-plan/1"
PLAN_CANONICALIZATION_VERSION: Literal["plan-canonicalization/1"] = "plan-canonicalization/1"
MERGE_POLICY_VERSION: Literal["merge-policy/1"] = "merge-policy/1"
RULE_DSL_VERSION: Literal["rule-dsl/1"] = "rule-dsl/1"
GENERATOR_CATALOG_VERSION: Literal["generator-catalog/1"] = "generator-catalog/1"
SEED_DERIVATION_VERSION: Literal["seed-derivation/1"] = "seed-derivation/1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
DiagnosticMessage = Annotated[str, StringConstraints(max_length=2000)]


class ArtifactModel(BaseModel):
    """Base cerrada de los modelos persistidos en el artefacto."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ArtifactDiagnostics(ArtifactModel):
    """Metadatos auditables excluidos explícitamente del fingerprint."""

    created_at: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    messages: list[DiagnosticMessage] = Field(default_factory=list, max_length=100)

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("'created_at' debe incluir zona horaria.")
        return value


class PlanFingerprint(ArtifactModel):
    """Huella SHA-256 del payload ejecutable canónico."""

    algorithm: Literal["sha256"] = "sha256"
    value: Sha256Hex


class ResolvedGeneratorBase(ArtifactModel):
    """Campos comunes de cualquier generador ya resuelto."""

    null_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    unique: bool = False


class ResolvedFakerGenerator(ResolvedGeneratorBase):
    """Generador Faker validado por el mismo modelo que usa el motor."""

    type: Literal["faker"] = "faker"
    params: FakerParams


class ResolvedChoiceGenerator(ResolvedGeneratorBase):
    """Generador choice validado."""

    type: Literal["choice"] = "choice"
    params: ChoiceParams


class ResolvedNumericRangeGenerator(ResolvedGeneratorBase):
    """Generador numeric_range validado."""

    type: Literal["numeric_range"] = "numeric_range"
    params: NumericRangeParams = Field(default_factory=NumericRangeParams)


class ResolvedDatetimeRangeGenerator(ResolvedGeneratorBase):
    """Generador datetime_range validado."""

    type: Literal["datetime_range"] = "datetime_range"
    params: DatetimeRangeParams = Field(default_factory=DatetimeRangeParams)


class ResolvedTemplateGenerator(ResolvedGeneratorBase):
    """Generador template validado."""

    type: Literal["template"] = "template"
    params: TemplateParams = Field(default_factory=TemplateParams)


class ResolvedSequenceGenerator(ResolvedGeneratorBase):
    """Generador sequence validado."""

    type: Literal["sequence"] = "sequence"
    params: SequenceParams = Field(default_factory=SequenceParams)


class ResolvedUuidGenerator(ResolvedGeneratorBase):
    """Generador UUID validado."""

    type: Literal["uuid"] = "uuid"
    params: UuidParams = Field(default_factory=UuidParams)


class ResolvedDerivedGenerator(ResolvedGeneratorBase):
    """Generador derived validado contra el mini-DSL al resolverlo."""

    type: Literal["derived"] = "derived"
    params: DerivedParams


class ResolvedFallbackGenerator(ResolvedGeneratorBase):
    """Fallback estructural del motor."""

    type: Literal["fallback"] = "fallback"
    params: FallbackParams = Field(default_factory=FallbackParams)


class ResolvedFkGenerator(ResolvedGeneratorBase):
    """Selector de una FK existente; la IR sigue siendo la autoridad."""

    type: Literal["fk"] = "fk"
    params: FkStrategy = Field(
        default_factory=lambda: FkUniform(strategy="uniform"),
        discriminator="strategy",
    )


ResolvedGenerator = Annotated[
    ResolvedFakerGenerator
    | ResolvedChoiceGenerator
    | ResolvedNumericRangeGenerator
    | ResolvedDatetimeRangeGenerator
    | ResolvedTemplateGenerator
    | ResolvedSequenceGenerator
    | ResolvedUuidGenerator
    | ResolvedDerivedGenerator
    | ResolvedFallbackGenerator
    | ResolvedFkGenerator,
    Field(discriminator="type"),
]


def _params_dump(params: GeneratorParams | FkStrategy) -> dict[str, object]:
    """Vuelca solo parámetros no predeterminados en la forma del motor."""
    dumped = params.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    return {str(key): value for key, value in dumped.items()}


class ResolvedColumnPlan(ArtifactModel):
    """Decisión validada para una columna existente."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    column_name: str = Field(min_length=1, max_length=256)
    generator: ResolvedGenerator | None
    source: PlanSource
    confidence: float = Field(ge=0.0, le=1.0)
    role: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_database_managed_column(self) -> ResolvedColumnPlan:
        if self.generator is None and (self.source != "ir" or self.confidence != 1.0):
            raise ValueError(
                "una columna sin generador debe estar gestionada por la IR "
                "con source='ir' y confidence=1.0."
            )
        return self

    def to_column_plan(self) -> ColumnPlan:
        """Convierte la decisión al modelo que consume el motor actual."""
        generator_spec: GeneratorSpec | None = None
        if self.generator is not None:
            generator_spec = GeneratorSpec(
                type=self.generator.type,
                params=_params_dump(self.generator.params),
                null_ratio=self.generator.null_ratio,
                unique=self.generator.unique,
            )
        return ColumnPlan(
            column=self.column_name,
            generator=generator_spec,
            source=self.source,
            confidence=self.confidence,
            role=self.role,
        )


class ResolvedTablePlan(ArtifactModel):
    """Decisiones resueltas de una tabla, sin duplicar estructura de la IR."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    columns: list[ResolvedColumnPlan] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_columns(self) -> ResolvedTablePlan:
        seen: set[str] = set()
        for column in self.columns:
            if (column.schema_name, column.table_name) != (
                self.schema_name,
                self.table_name,
            ):
                raise ValueError(
                    f"la tabla propietaria de {column.column_name!r} es "
                    f"{column.table_name!r}, no {self.table_name!r}."
                )
            if column.column_name in seen:
                raise ValueError(f"columna duplicada {self.table_name}.{column.column_name}.")
            seen.add(column.column_name)
        return self

    def to_table_plan(self) -> TablePlan:
        """Convierte la tabla al modelo de plan vigente."""
        return TablePlan(
            table=self.table_name,
            columns=[column.to_column_plan() for column in self.columns],
        )


def _canonical_bytes(value: object) -> bytes:
    """Serializa un valor JSON a bytes canónicos según la versión v1."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _fingerprint(
    *,
    version: str,
    proposal_version: str,
    canonicalization_version: str,
    merge_policy_version: str,
    rule_dsl_version: str,
    generator_catalog_version: str,
    seed_derivation_version: str,
    schema_hash: str,
    tables: list[ResolvedTablePlan],
    audit: bool,
) -> PlanFingerprint:
    """Calcula la huella ejecutable o auditable; nunca recibe diagnósticos."""
    if audit:
        table_payload = [table.model_dump(mode="json") for table in tables]
    else:
        table_payload = [
            {
                "columns": [
                    {
                        "column_name": column.column_name,
                        "generator": (
                            column.generator.model_dump(mode="json")
                            if column.generator is not None
                            else None
                        ),
                        "schema_name": column.schema_name,
                        "table_name": column.table_name,
                    }
                    for column in table.columns
                ],
                "schema_name": table.schema_name,
                "table_name": table.table_name,
            }
            for table in tables
        ]
    payload = {
        "canonicalization_version": canonicalization_version,
        "generator_catalog_version": generator_catalog_version,
        "merge_policy_version": merge_policy_version,
        "proposal_version": proposal_version,
        "rule_dsl_version": rule_dsl_version,
        "schema_hash": schema_hash,
        "seed_derivation_version": seed_derivation_version,
        "tables": table_payload,
        "version": version,
    }
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return PlanFingerprint(value=digest)


class ResolvedPlanArtifact(ArtifactModel):
    """Artefacto versionado, validado y convertible al plan ejecutable actual."""

    version: Literal["resolved-plan/1"] = RESOLVED_PLAN_VERSION
    proposal_version: Literal["semantic-proposal/1"] = "semantic-proposal/1"
    canonicalization_version: Literal["plan-canonicalization/1"] = PLAN_CANONICALIZATION_VERSION
    merge_policy_version: Literal["merge-policy/1"] = MERGE_POLICY_VERSION
    rule_dsl_version: Literal["rule-dsl/1"] = RULE_DSL_VERSION
    generator_catalog_version: Literal["generator-catalog/1"] = GENERATOR_CATALOG_VERSION
    seed_derivation_version: Literal["seed-derivation/1"] = SEED_DERIVATION_VERSION
    schema_hash: Sha256Hex
    tables: list[ResolvedTablePlan] = Field(min_length=1)
    fingerprint: PlanFingerprint
    audit_fingerprint: PlanFingerprint
    diagnostics: ArtifactDiagnostics

    @model_validator(mode="after")
    def _verify_integrity(self) -> ResolvedPlanArtifact:
        expected = _fingerprint(
            version=self.version,
            proposal_version=self.proposal_version,
            canonicalization_version=self.canonicalization_version,
            merge_policy_version=self.merge_policy_version,
            rule_dsl_version=self.rule_dsl_version,
            generator_catalog_version=self.generator_catalog_version,
            seed_derivation_version=self.seed_derivation_version,
            schema_hash=self.schema_hash,
            tables=self.tables,
            audit=False,
        )
        if self.fingerprint != expected:
            raise ValueError(
                "fingerprint inválido: el artefacto fue manipulado o se "
                "canonicalizó con datos/versiones distintos."
            )
        expected_audit = _fingerprint(
            version=self.version,
            proposal_version=self.proposal_version,
            canonicalization_version=self.canonicalization_version,
            merge_policy_version=self.merge_policy_version,
            rule_dsl_version=self.rule_dsl_version,
            generator_catalog_version=self.generator_catalog_version,
            seed_derivation_version=self.seed_derivation_version,
            schema_hash=self.schema_hash,
            tables=self.tables,
            audit=True,
        )
        if self.audit_fingerprint != expected_audit:
            raise ValueError(
                "audit_fingerprint inválido: la trazabilidad del artefacto fue "
                "manipulada o se canonicalizó con datos/versiones distintos."
            )
        seen: set[TableIdentity] = set()
        for table in self.tables:
            identity = (table.schema_name, table.table_name)
            if identity in seen:
                raise ValueError(f"tabla duplicada {identity_text(identity)!r}.")
            seen.add(identity)
        return self

    @classmethod
    def create(
        cls,
        *,
        schema: SchemaSpec,
        tables: list[ResolvedTablePlan],
        diagnostics: ArtifactDiagnostics,
        version: Literal["resolved-plan/1"] = RESOLVED_PLAN_VERSION,
        proposal_version: Literal["semantic-proposal/1"] = "semantic-proposal/1",
        canonicalization_version: Literal[
            "plan-canonicalization/1"
        ] = PLAN_CANONICALIZATION_VERSION,
        merge_policy_version: Literal["merge-policy/1"] = MERGE_POLICY_VERSION,
        rule_dsl_version: Literal["rule-dsl/1"] = RULE_DSL_VERSION,
        generator_catalog_version: Literal["generator-catalog/1"] = GENERATOR_CATALOG_VERSION,
        seed_derivation_version: Literal["seed-derivation/1"] = SEED_DERIVATION_VERSION,
    ) -> ResolvedPlanArtifact:
        """Valida contra la IR y sella el payload con su fingerprint correcto."""
        if schema.hash is None:
            raise ValueError("la IR no tiene 'hash'; calcúlalo antes de crear el artefacto.")
        schema_hash = schema.hash
        fingerprint = _fingerprint(
            version=version,
            proposal_version=proposal_version,
            canonicalization_version=canonicalization_version,
            merge_policy_version=merge_policy_version,
            rule_dsl_version=rule_dsl_version,
            generator_catalog_version=generator_catalog_version,
            seed_derivation_version=seed_derivation_version,
            schema_hash=schema_hash,
            tables=tables,
            audit=False,
        )
        audit_fingerprint = _fingerprint(
            version=version,
            proposal_version=proposal_version,
            canonicalization_version=canonicalization_version,
            merge_policy_version=merge_policy_version,
            rule_dsl_version=rule_dsl_version,
            generator_catalog_version=generator_catalog_version,
            seed_derivation_version=seed_derivation_version,
            schema_hash=schema_hash,
            tables=tables,
            audit=True,
        )
        artifact = cls(
            version=version,
            proposal_version=proposal_version,
            canonicalization_version=canonicalization_version,
            merge_policy_version=merge_policy_version,
            rule_dsl_version=rule_dsl_version,
            generator_catalog_version=generator_catalog_version,
            seed_derivation_version=seed_derivation_version,
            schema_hash=schema_hash,
            tables=tables,
            fingerprint=fingerprint,
            audit_fingerprint=audit_fingerprint,
            diagnostics=diagnostics,
        )
        return validate_artifact_against_schema(artifact, schema)

    def canonical_bytes(self) -> bytes:
        """Devuelve el artefacto completo en JSON canónico UTF-8."""
        return _canonical_bytes(self.model_dump(mode="json"))

    def to_table_plans(self, schema: SchemaSpec) -> TablePlans:
        """Valida la IR y convierte al contrato consumido por el motor vigente."""
        validate_artifact_against_schema(self, schema)
        names = [table.table_name for table in self.tables]
        if len(names) != len(set(names)):
            raise ValueError(
                "ResolvedPlanArtifact representa las tablas homónimas por namespace, "
                "pero el TablePlans vigente solo conserva el nombre simple y no puede "
                "recibirlas sin colisión."
            )
        return TablePlans(tables=[table.to_table_plan() for table in self.tables])


def validate_artifact_against_schema(
    artifact: ResolvedPlanArtifact, schema: SchemaSpec
) -> ResolvedPlanArtifact:
    """Exige correspondencia exacta con la IR antes de cualquier ejecución."""
    if schema.hash is None:
        raise ValueError("la IR no tiene 'hash'; calcúlalo antes de validar el artefacto.")
    if artifact.schema_hash != schema.hash:
        raise ValueError(
            "schema_hash del artefacto no coincide con la IR: "
            f"{artifact.schema_hash} != {schema.hash}."
        )

    expected_tables = [table_identity(table) for table in schema.tables]
    actual_tables = [(table.schema_name, table.table_name) for table in artifact.tables]
    if actual_tables != expected_tables:
        raise ValueError(
            "las tablas del artefacto no coinciden exactamente con la IR y en "
            f"su orden: {actual_tables!r} != {expected_tables!r}."
        )
    for resolved, structural in zip(artifact.tables, schema.tables, strict=True):
        table_ctx = identity_text(table_identity(structural))
        expected_columns = [column.name for column in structural.columns]
        actual_columns = [column.column_name for column in resolved.columns]
        if actual_columns != expected_columns:
            raise ValueError(
                f"las columnas de {table_ctx!r} no coinciden exactamente "
                f"con la IR y en su orden: {actual_columns!r} != "
                f"{expected_columns!r}."
            )
        for resolved_column, structural_column in zip(
            resolved.columns, structural.columns, strict=True
        ):
            generator = resolved_column.generator
            validate_generator_compatibility(
                schema=schema,
                table=structural,
                column=structural_column,
                generator_type=generator.type if generator is not None else None,
                params=(_params_dump(generator.params) if generator is not None else None),
                null_ratio=generator.null_ratio if generator is not None else 0.0,
                unique=generator.unique if generator is not None else False,
                context=f"{table_ctx}.{structural_column.name}",
            )
    return artifact
