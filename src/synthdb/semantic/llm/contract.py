"""Contrato versionado de propuestas semánticas no confiables.

`SemanticProposal` representa exclusivamente lo que un modelo puede proponer.
No es un plan ejecutable y no se convierte implícitamente en uno: la frontera
`validate_proposal_against_schema` comprueba que identificadores, evidencias,
referencias parent(), generadores y dominios pertenecen a la IR recibida, pero
la aceptación o el descarte final de cada propuesta corresponde al fusor de
entregas posteriores.

El contrato evita deliberadamente superficies abiertas. Todos los modelos usan
``extra="forbid"`` y cada generador permitido tiene parámetros tipados. El
modelo no puede proponer estructura, claves concretas, ``null_ratio``,
``depends_on``, pools de texto ni generación de contenido por LLM.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from string import Formatter
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from synthdb.ir.schema import RelationshipSpec, SchemaSpec, TableSpec
from synthdb.rules import RuleParseError, parse_rule
from synthdb.rules.dsl import (
    Arith,
    BoolOp,
    Call,
    Compare,
    Neg,
    Node,
    Not,
    ParentCol,
    referenced_columns,
)
from synthdb.semantic.compatibility import (
    TableIdentity,
    identity_text,
    index_tables,
    resolve_referenced_table,
    table_identity,
    validate_generator_compatibility,
)

SEMANTIC_PROPOSAL_VERSION: Literal["semantic-proposal/1"] = "semantic-proposal/1"
"""Versión inicial del contrato de entrada no confiable."""

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RELATIONSHIP_ID_PATTERN = r"^fk:[0-9a-f]{64}$"
_SAFE_TEMPLATE_FIELDS = frozenset({"tabla", "table", "columna", "column", "n"})
MAX_SEMANTIC_PROPOSAL_BYTES = 1_000_000
"""Techo del payload completo aceptado en la frontera del proveedor."""

_MAX_SCALAR_STRING_LENGTH = 4096

Sha256Hex = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
RelationshipId = Annotated[str, StringConstraints(pattern=_RELATIONSHIP_ID_PATTERN)]
BoundedStrictStr = Annotated[
    StrictStr,
    StringConstraints(max_length=_MAX_SCALAR_STRING_LENGTH),
]
StrictNumber = StrictInt | StrictFloat
Scalar = BoundedStrictStr | StrictInt | StrictFloat | StrictBool


def _parse_iso_value(value: str) -> date | datetime:
    if "T" in value or " " in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.fromisoformat(value)


class ProposalModel(BaseModel):
    """Base cerrada de toda entrada procedente del modelo."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


EvidenceKind = Literal[
    "table_name",
    "table_comment",
    "column_name",
    "column_comment",
    "type",
    "constraint",
]


class EvidenceRef(ProposalModel):
    """Referencia auditable a evidencia ya presente en la IR enviada al modelo."""

    kind: EvidenceKind
    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    column_name: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_shape(self) -> EvidenceRef:
        table_only = self.kind in {"table_name", "table_comment"}
        if table_only and self.column_name is not None:
            raise ValueError(f"evidence kind={self.kind!r} es de tabla y no admite 'column_name'.")
        if not table_only and self.column_name is None:
            raise ValueError(f"evidence kind={self.kind!r} exige identificar 'column_name'.")
        return self


UncertaintyCode = Literal[
    "ambiguous_name",
    "missing_comment",
    "insufficient_context",
    "conflicting_evidence",
    "unsupported_semantics",
]


class UncertaintyReason(ProposalModel):
    """Motivo normalizado por el que la propuesta debe revisarse."""

    code: UncertaintyCode
    detail: str = Field(min_length=1, max_length=500)


class UniformDistributionProposal(ProposalModel):
    """Distribución uniforme, sin parámetros adicionales."""

    family: Literal["uniform"] = "uniform"


class NormalDistributionProposal(ProposalModel):
    """Distribución normal recortada al rango propuesto."""

    family: Literal["normal"] = "normal"
    mean: StrictNumber | None = None
    std: StrictNumber | None = Field(default=None, ge=0.0)


class LognormalDistributionProposal(ProposalModel):
    """Distribución lognormal recortada al rango propuesto."""

    family: Literal["lognormal"] = "lognormal"
    median: StrictNumber | None = Field(default=None, gt=0.0)
    sigma: StrictNumber | None = Field(default=None, gt=0.0)


class ZipfDistributionProposal(ProposalModel):
    """Distribución Zipf sobre un rango entero."""

    family: Literal["zipf"] = "zipf"
    s: StrictNumber = Field(default=1.2, gt=0.0)


DistributionProposal = Annotated[
    UniformDistributionProposal
    | NormalDistributionProposal
    | LognormalDistributionProposal
    | ZipfDistributionProposal,
    Field(discriminator="family"),
]

FakerProvider = Literal[
    "email",
    "iban",
    "ssn",
    "postcode",
    "user_name",
    "ipv4",
    "url",
    "image_url",
    "phone_number",
    "last_name",
    "name",
    "company",
    "job",
    "word",
    "street_address",
    "city",
    "region",
    "country_code",
    "country",
    "currency_code",
    "color_name",
    "license_plate",
    "slug",
    "catch_phrase",
]


class FakerProposalParams(ProposalModel):
    """Parámetros cerrados de una propuesta Faker."""

    provider: FakerProvider


class FakerGeneratorProposal(ProposalModel):
    """Propuesta de un proveedor Faker conocido."""

    type: Literal["faker"] = "faker"
    params: FakerProposalParams


class ChoiceProposalParams(ProposalModel):
    """Valores discretos y pesos opcionales."""

    values: list[Scalar] = Field(min_length=1, max_length=500)
    weights: list[StrictNumber] | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate_weights(self) -> ChoiceProposalParams:
        identities = [(type(value), value) for value in self.values]
        if len(set(identities)) != len(identities):
            raise ValueError("'values' no admite valores duplicados.")
        if self.weights is None:
            return self
        if len(self.weights) != len(self.values):
            raise ValueError("'weights' debe tener la misma longitud que 'values'.")
        if any(weight < 0 for weight in self.weights) or sum(self.weights) <= 0:
            raise ValueError("'weights' debe contener pesos no negativos con suma > 0.")
        return self


class ChoiceGeneratorProposal(ProposalModel):
    """Propuesta de elección entre valores cerrados."""

    type: Literal["choice"] = "choice"
    params: ChoiceProposalParams


class NumericRangeProposalParams(ProposalModel):
    """Rango numérico con distribución tipada."""

    min: StrictNumber | None = None
    max: StrictNumber | None = None
    min_exclusive: StrictBool = False
    max_exclusive: StrictBool = False
    distribution: DistributionProposal = Field(default_factory=UniformDistributionProposal)
    round_to: StrictNumber | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _validate_range(self) -> NumericRangeProposalParams:
        if self.min is not None and self.max is not None:
            if self.min > self.max:
                raise ValueError("'min' no puede ser mayor que 'max'.")
            if self.min == self.max and (self.min_exclusive or self.max_exclusive):
                raise ValueError("las exclusividades dejan un rango numérico vacío.")
        return self


class NumericRangeGeneratorProposal(ProposalModel):
    """Propuesta de rango numérico."""

    type: Literal["numeric_range"] = "numeric_range"
    params: NumericRangeProposalParams = Field(default_factory=NumericRangeProposalParams)


class DatetimeRangeProposalParams(ProposalModel):
    """Cotas ISO para una propuesta temporal.

    Se conservan como texto porque siguen siendo una propuesta no ejecutable.
    La compilación posterior decide si se aplican a ``date`` o ``timestamp``.
    """

    min: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?$",
    )
    max: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?$",
    )

    @field_validator("min", "max")
    @classmethod
    def _validate_iso_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            _parse_iso_value(value)
        except ValueError as exc:
            raise ValueError(f"fecha/hora ISO inválida: {value!r}.") from exc
        return value

    @model_validator(mode="after")
    def _validate_order(self) -> DatetimeRangeProposalParams:
        if self.min is None or self.max is None:
            return self
        low = _parse_iso_value(self.min)
        high = _parse_iso_value(self.max)
        try:
            if high < low:
                raise ValueError("'max' temporal debe ser mayor o igual que 'min'.")
        except TypeError as exc:
            raise ValueError(
                "las cotas temporales no pueden mezclar valores con y sin zona horaria."
            ) from exc
        return self


class DatetimeRangeGeneratorProposal(ProposalModel):
    """Propuesta de rango de fecha u hora."""

    type: Literal["datetime_range"] = "datetime_range"
    params: DatetimeRangeProposalParams = Field(default_factory=DatetimeRangeProposalParams)


class TemplateProposalParams(ProposalModel):
    """Plantilla limitada a los marcadores que entiende el generador."""

    template: str = Field(
        default="{tabla}_{columna}_{n}",
        min_length=1,
        max_length=500,
    )
    start: int = 0

    @model_validator(mode="after")
    def _validate_template(self) -> TemplateProposalParams:
        try:
            fields: list[str] = []
            for (
                _literal,
                field_name,
                format_spec,
                conversion,
            ) in Formatter().parse(self.template):
                if field_name is None:
                    continue
                if format_spec or conversion is not None:
                    raise ValueError(
                        "la plantilla no admite conversiones ni especificadores de formato."
                    )
                fields.append(field_name)
        except ValueError as exc:
            raise ValueError(f"plantilla inválida: {exc}") from exc
        if any(field not in _SAFE_TEMPLATE_FIELDS for field in fields):
            raise ValueError("la plantilla solo admite {tabla}/{table}, {columna}/{column} y {n}.")
        return self


class TemplateGeneratorProposal(ProposalModel):
    """Propuesta de texto determinista por plantilla."""

    type: Literal["template"] = "template"
    params: TemplateProposalParams = Field(default_factory=TemplateProposalParams)


class SequenceProposalParams(ProposalModel):
    """Parámetros de una secuencia aritmética."""

    start: int = 1
    step: int = 1

    @model_validator(mode="after")
    def _validate_step(self) -> SequenceProposalParams:
        if self.step == 0:
            raise ValueError("'step' no puede ser 0.")
        return self


class SequenceGeneratorProposal(ProposalModel):
    """Propuesta de secuencia."""

    type: Literal["sequence"] = "sequence"
    params: SequenceProposalParams = Field(default_factory=SequenceProposalParams)


class UuidProposalParams(ProposalModel):
    """El generador UUID no admite parámetros."""


class UuidGeneratorProposal(ProposalModel):
    """Propuesta de UUID determinista."""

    type: Literal["uuid"] = "uuid"
    params: UuidProposalParams = Field(default_factory=UuidProposalParams)


GeneratorProposal = Annotated[
    FakerGeneratorProposal
    | ChoiceGeneratorProposal
    | NumericRangeGeneratorProposal
    | DatetimeRangeGeneratorProposal
    | TemplateGeneratorProposal
    | SequenceGeneratorProposal
    | UuidGeneratorProposal,
    Field(discriminator="type"),
]


class ProposedRule(ProposalModel):
    """Hipótesis de regla auditable; validarla no la autoriza para ejecutar."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    rule_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    table_name: str = Field(min_length=1, max_length=256)
    target_column: str = Field(min_length=1, max_length=256)
    kind: Literal["temporal", "derivation", "consistency"]
    expression: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=1000)
    confidence: StrictNumber = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _parse_safe_dsl(self) -> ProposedRule:
        try:
            parse_rule(self.expression)
        except RuleParseError as exc:
            raise ValueError(
                f"la regla propuesta {self.rule_id!r} no compila en el mini-DSL: {exc}"
            ) from exc
        return self


class UniformRelationshipStrategy(ProposalModel):
    """Hint de selección uniforme."""

    strategy: Literal["uniform"] = "uniform"


class ZipfRelationshipStrategy(ProposalModel):
    """Hint de selección Zipf."""

    strategy: Literal["zipf"] = "zipf"
    s: StrictNumber = Field(default=1.2, gt=0.0)


class UniqueSubsetRelationshipStrategy(ProposalModel):
    """Hint de selección sin reemplazo."""

    strategy: Literal["unique_subset"] = "unique_subset"


class QuotaRelationshipStrategy(ProposalModel):
    """Hint de cuotas por padre."""

    strategy: Literal["quota"] = "quota"
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_quota(self) -> QuotaRelationshipStrategy:
        if self.min > self.max:
            raise ValueError("'min' no puede ser mayor que 'max'.")
        return self


RelationshipStrategy = Annotated[
    UniformRelationshipStrategy
    | ZipfRelationshipStrategy
    | UniqueSubsetRelationshipStrategy
    | QuotaRelationshipStrategy,
    Field(discriminator="strategy"),
]


class ProposedRelationshipHint(ProposalModel):
    """Hint sobre una FK existente, identificada de forma opaca y verificable."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    relationship_id: RelationshipId
    strategy: RelationshipStrategy
    confidence: StrictNumber = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=50)
    uncertainties: list[UncertaintyReason] = Field(default_factory=list, max_length=50)


class ProposedColumn(ProposalModel):
    """Propuesta semántica para una columna que ya existe en la IR."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    column_name: str = Field(min_length=1, max_length=256)
    semantic_role: str = Field(min_length=1, max_length=200)
    generator: GeneratorProposal | None = None
    confidence: StrictNumber = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=50)
    uncertainties: list[UncertaintyReason] = Field(default_factory=list, max_length=50)
    rules: list[ProposedRule] = Field(default_factory=list, max_length=100)


class ProposedTable(ProposalModel):
    """Propuestas asociadas a una tabla ya existente."""

    schema_name: str | None = Field(default=None, min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)
    entity: str = Field(min_length=1, max_length=200)
    confidence: StrictNumber = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=50)
    uncertainties: list[UncertaintyReason] = Field(default_factory=list, max_length=50)
    columns: list[ProposedColumn] = Field(min_length=1, max_length=1000)
    relationship_hints: list[ProposedRelationshipHint] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _validate_owned_items(self) -> ProposedTable:
        seen_columns: set[str] = set()
        seen_rules: set[str] = set()
        for column in self.columns:
            if (column.schema_name, column.table_name) != (
                self.schema_name,
                self.table_name,
            ):
                raise ValueError(
                    f"la tabla propietaria de {column.column_name!r} es "
                    f"{column.table_name!r}, no {self.table_name!r}."
                )
            if column.column_name in seen_columns:
                raise ValueError(f"columna duplicada {self.table_name}.{column.column_name}.")
            seen_columns.add(column.column_name)
            for rule in column.rules:
                if (rule.schema_name, rule.table_name) != (
                    self.schema_name,
                    self.table_name,
                ) or rule.target_column != column.column_name:
                    raise ValueError(
                        f"la regla {rule.rule_id!r} no pertenece a "
                        f"{self.table_name}.{column.column_name}."
                    )
                if rule.rule_id in seen_rules:
                    raise ValueError(f"rule_id duplicado {rule.rule_id!r} en {self.table_name}.")
                seen_rules.add(rule.rule_id)

        seen_relationships: set[str] = set()
        for hint in self.relationship_hints:
            if (hint.schema_name, hint.table_name) != (
                self.schema_name,
                self.table_name,
            ):
                raise ValueError(
                    f"el hint {hint.relationship_id!r} declara tabla propietaria "
                    f"{hint.table_name!r}, no {self.table_name!r}."
                )
            if hint.relationship_id in seen_relationships:
                raise ValueError(f"relationship_id duplicado {hint.relationship_id!r}.")
            seen_relationships.add(hint.relationship_id)
        return self


class SemanticProposal(ProposalModel):
    """Salida no confiable y sin autoridad ejecutiva de una llamada al modelo."""

    version: Literal["semantic-proposal/1"] = SEMANTIC_PROPOSAL_VERSION
    schema_hash: Sha256Hex
    tables: list[ProposedTable] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reject_duplicate_tables(self) -> SemanticProposal:
        seen: set[TableIdentity] = set()
        for table in self.tables:
            identity = (table.schema_name, table.table_name)
            if identity in seen:
                raise ValueError(f"tabla duplicada {identity_text(identity)!r}.")
            seen.add(identity)
        encoded = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_SEMANTIC_PROPOSAL_BYTES:
            raise ValueError(
                f"la propuesta semántica supera el límite de {MAX_SEMANTIC_PROPOSAL_BYTES} bytes."
            )
        return self


def validate_semantic_proposal_json(payload: str | bytes | bytearray) -> SemanticProposal:
    """Valida tamaño antes de parsear JSON y después aplica el contrato cerrado."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > MAX_SEMANTIC_PROPOSAL_BYTES:
        raise ValueError(
            f"la respuesta del proveedor supera el límite de {MAX_SEMANTIC_PROPOSAL_BYTES} bytes."
        )
    return SemanticProposal.model_validate_json(raw)


def relationship_identifier(table: TableSpec, relationship: RelationshipSpec) -> str:
    """Calcula el identificador opaco de una FK existente.

    El identificador no codifica claves concretas elegidas en ejecución. Es el
    SHA-256 de la identidad estructural de la relación y solo sirve para que el
    modelo pueda referirse a una FK que ya recibió en el prompt.
    """
    payload = {
        "columns": relationship.columns,
        "ref_columns": relationship.ref_columns,
        "ref_table": relationship.ref_table,
        "schema": table.schema_,
        "table": table.name,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"fk:{hashlib.sha256(canonical).hexdigest()}"


def _parent_references(node: Node) -> list[ParentCol]:
    """Recorre el AST cerrado y devuelve todas sus referencias ``parent``."""
    if isinstance(node, ParentCol):
        return [node]
    if isinstance(node, Call):
        children = node.args
    elif isinstance(node, Compare | Arith | BoolOp):
        children = (node.left, node.right)
    elif isinstance(node, Not | Neg):
        children = (node.operand,)
    else:
        children = ()
    return [reference for child in children for reference in _parent_references(child)]


def _column_has_constraint(table: TableSpec, column_name: str) -> bool:
    structural_column = next(
        (column for column in table.columns if column.name == column_name),
        None,
    )
    return (
        column_name in table.primary_key
        or any(column_name in columns for columns in table.uniques)
        or any(column_name in relationship.columns for relationship in table.foreign_keys)
        or bool(
            structural_column is not None
            and (
                structural_column.checks
                or structural_column.enum_values is not None
                or structural_column.generated
            )
        )
        or any(column_name in check.columns_involved for check in table.checks)
    )


def validate_proposal_against_schema(
    proposal: SemanticProposal, schema: SchemaSpec
) -> SemanticProposal:
    """Comprueba referencias de la propuesta contra la IR sin mutar ninguna.

    Esta función decide si la propuesta habla de objetos y evidencia existentes,
    y si sus candidatos son compatibles con la IR del mismo hash. No acepta
    generadores, reglas ni hints para ejecución.

    Args:
        proposal: Propuesta ya validada por su contrato cerrado.
        schema: IR estructural que el modelo recibió.

    Returns:
        La misma propuesta, para encadenar la frontera de validación.

    Raises:
        ValueError: Si el hash, una tabla, columna, evidencia o FK no pertenece
            a la IR.
    """
    if schema.hash is None:
        raise ValueError(
            "la IR no tiene 'hash'; calcúlalo antes de validar una propuesta semántica."
        )
    if proposal.schema_hash != schema.hash:
        raise ValueError(
            "schema_hash de la propuesta no coincide con la IR: "
            f"{proposal.schema_hash} != {schema.hash}."
        )

    tables = index_tables(schema)
    columns = {
        identity: {column.name for column in table.columns} for identity, table in tables.items()
    }
    relationship_ids = {
        table_identity(table): {
            relationship_identifier(table, relationship) for relationship in table.foreign_keys
        }
        for table in schema.tables
    }

    def validate_evidence(evidence: EvidenceRef) -> None:
        identity = (evidence.schema_name, evidence.table_name)
        structural_table = tables.get(identity)
        if structural_table is None:
            raise ValueError(
                f"la evidencia referencia la tabla inexistente {identity_text(identity)!r}."
            )
        if evidence.column_name is not None and evidence.column_name not in columns[identity]:
            raise ValueError(
                "la evidencia referencia la columna inexistente "
                f"{identity_text(identity)}.{evidence.column_name}."
            )
        if evidence.kind == "table_comment" and not structural_table.comment:
            raise ValueError(
                f"la evidencia cita un comentario inexistente en {identity_text(identity)}."
            )
        if evidence.kind == "column_comment":
            structural_column = next(
                column for column in structural_table.columns if column.name == evidence.column_name
            )
            if not structural_column.comment:
                raise ValueError(
                    "la evidencia cita un comentario inexistente en "
                    f"{identity_text(identity)}.{evidence.column_name}."
                )
        if evidence.kind == "constraint" and not _column_has_constraint(
            structural_table, evidence.column_name or ""
        ):
            raise ValueError(
                "la evidencia cita una constraint inexistente para "
                f"{identity_text(identity)}.{evidence.column_name}."
            )

    for table_proposal in proposal.tables:
        identity = (table_proposal.schema_name, table_proposal.table_name)
        if identity not in tables:
            raise ValueError(
                f"la propuesta referencia la tabla inexistente {identity_text(identity)!r}."
            )
        structural_table = tables[identity]
        for evidence in table_proposal.evidence:
            validate_evidence(evidence)
        for column in table_proposal.columns:
            if column.column_name not in columns[identity]:
                raise ValueError(
                    f"la propuesta referencia la columna inexistente "
                    f"{identity_text(identity)}.{column.column_name}."
                )
            structural_column = next(
                item for item in structural_table.columns if item.name == column.column_name
            )
            key_column = (
                column.column_name in structural_table.primary_key
                or any(
                    column.column_name in relationship.columns
                    for relationship in structural_table.foreign_keys
                )
                or structural_column.type.autoincrement
                or structural_column.generated
            )
            if key_column and column.generator is not None:
                raise ValueError(
                    f"{identity_text(identity)}.{column.column_name} es una clave o columna "
                    "gestionada estructuralmente; el modelo no puede proponerle "
                    "un generador."
                )
            if column.generator is not None:
                validate_generator_compatibility(
                    schema=schema,
                    table=structural_table,
                    column=structural_column,
                    generator_type=column.generator.type,
                    params=column.generator.params.model_dump(
                        mode="python",
                        exclude_none=True,
                    ),
                    enforce_unique=False,
                    context=f"{identity_text(identity)}.{column.column_name}",
                )
            for evidence in column.evidence:
                validate_evidence(evidence)
            for rule in column.rules:
                parsed_rule = parse_rule(rule.expression)
                unknown_columns = sorted(referenced_columns(parsed_rule.root) - columns[identity])
                if unknown_columns:
                    raise ValueError(
                        f"la regla {rule.rule_id!r} referencia columnas "
                        f"inexistentes de {identity_text(identity)!r}: "
                        f"{unknown_columns!r}."
                    )
                for parent_ref in _parent_references(parsed_rule.root):
                    relationships = [
                        relationship
                        for relationship in structural_table.foreign_keys
                        if parent_ref.fk in relationship.columns
                    ]
                    if len(relationships) != 1:
                        raise ValueError(
                            f"la regla {rule.rule_id!r} usa parent({parent_ref.fk}), "
                            "pero esa columna no identifica una única FK local."
                        )
                    parent_table = resolve_referenced_table(
                        schema,
                        structural_table,
                        relationships[0].ref_table,
                    )
                    parent_columns = (
                        {item.name for item in parent_table.columns}
                        if parent_table is not None
                        else set()
                    )
                    if parent_ref.column not in parent_columns:
                        raise ValueError(
                            f"la regla {rule.rule_id!r} usa "
                            f"parent({parent_ref.fk}).{parent_ref.column}, pero la "
                            "tabla referenciada no contiene esa columna."
                        )
                for evidence in rule.evidence:
                    validate_evidence(evidence)
        for hint in table_proposal.relationship_hints:
            if hint.relationship_id not in relationship_ids[identity]:
                raise ValueError(
                    f"relationship_id {hint.relationship_id!r} no identifica "
                    f"una FK existente de {identity_text(identity)!r}."
                )
            for evidence in hint.evidence:
                validate_evidence(evidence)
    return proposal
