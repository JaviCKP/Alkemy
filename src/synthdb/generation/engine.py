"""Deterministic in-memory generation engine (T2.11 and T2.12).

The engine compiles every table before producing a row, then executes the
structural phases with one RNG per row.  ``Dataset`` is deliberately the MVP
store: it keeps generated parent rows, applied deferred updates and quarantine
records in memory so later emitters can choose their physical representation.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from random import Random
from typing import Any, TypeAlias

from synthdb.config.models import Config
from synthdb.constraints.check_interp import interpret_checks
from synthdb.generation.context import RowContext, build_column_order, mapping_resolver
from synthdb.generation.fk import (
    NullRatioSelector,
    QuotaInfeasibleError,
    UniformSelector,
    UniqueSubsetSelector,
    ZipfSelector,
    build_quota_assignment,
)
from synthdb.generation.generators import Generator, resolve
from synthdb.generation.keystore import KeyStore
from synthdb.generation.numeric_bounds import (
    effective_scale,
    has_quantized_value,
    representable_limit,
)
from synthdb.generation.seeding import rng_for_row, seed_for_table
from synthdb.graph.dependency import analyze_structure, index_tables
from synthdb.graph.strategies import resolve_cycles
from synthdb.ir.plans import (
    DeferredPhase,
    InsertLeveledPhase,
    InsertPhase,
    Phase,
    TablePlan,
    TablePlans,
    UpdatePhase,
)
from synthdb.ir.schema import ColumnSpec, GeneratorSpec, RelationshipSpec, SchemaSpec, TableSpec
from synthdb.rules import RuleParseError, parse_rule
from synthdb.rules.dsl import (
    Arith,
    BoolOp,
    Bound,
    Call,
    Col,
    Compare,
    Const,
    Derivation,
    Neg,
    Node,
    Not,
    ParentCol,
    Ref,
    Rule,
    as_bound,
    as_derivation,
    referenced_columns,
)
from synthdb.rules.eval import evaluate
from synthdb.semantic.merge import PlanError as MergePlanError
from synthdb.semantic.merge import build_plan
from synthdb.validation.structural import ValidationIssue, validate_batch


class PlanError(ValueError):
    """Compilation error naming the table or rule that must be corrected."""


class GenerationError(RuntimeError):
    """Generation aborted because a produced batch failed structural validation."""


@dataclass(frozen=True)
class DatasetUpdate:
    """A deferred FK update already applied to the final in-memory row."""

    table: str
    row_index: int
    values: dict[str, Any]


@dataclass
class _CompiledColumn:
    column: ColumnSpec
    spec: GeneratorSpec | None
    generator: Generator | None
    bounds: list[Bound] = field(default_factory=list)
    derivation: Derivation | None = None


@dataclass
class _CompiledTable:
    spec: TableSpec
    plan: TablePlan
    columns: dict[str, _CompiledColumn]
    order: list[str]
    rules: list[Rule]


@dataclass
class _FkSelectionState:
    """Estado determinista para seleccionar una FK a lo largo de una tabla."""

    fk: RelationshipSpec
    parent_rows: list[dict[str, Any]]
    strategy: str
    params: dict[str, Any]
    shared_columns: bool
    selector: Any | None = None
    quota: list[int] | None = None
    unique_remaining: set[int] | None = None
    all_indices: tuple[int, ...] = ()
    parent_indices_by_projection: dict[tuple[str, ...], dict[tuple[Any, ...], list[int]]] = field(
        default_factory=dict
    )
    filtered_candidates_cache: dict[tuple[Any, ...], list[int]] = field(default_factory=dict)


@dataclass
class Dataset:
    """Complete in-memory result of one generation run.

    ``tables`` contains only valid rows. ``quarantine`` maps a table to the
    rejected ``(row, columns, reason)`` tuples.  Internal metadata is retained
    for deterministic rule re-evaluation by ``validate_batch``.
    """

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    quarantine: dict[str, list[ValidationIssue]] = field(default_factory=dict)
    updates: list[DatasetUpdate] = field(default_factory=list)
    _update_origins: list[int] = field(default_factory=list, repr=False)
    levels: dict[str, list[int]] = field(default_factory=dict)
    phases: list[Phase] = field(default_factory=list)
    table_plans: TablePlans | None = None
    _compiled: dict[str, _CompiledTable] = field(default_factory=dict, repr=False)
    _parents: dict[str, dict[int, dict[str, dict[str, Any] | None]]] = field(
        default_factory=dict, repr=False
    )
    _row_numbers: dict[str, dict[int, int]] = field(default_factory=dict, repr=False)
    _rng_states: dict[str, dict[int, dict[str, tuple[Any, ...]]]] = field(
        default_factory=dict, repr=False
    )
    _validation_unique: dict[str, dict[tuple[str, ...], set[tuple[Any, ...]]]] = field(
        default_factory=dict, repr=False
    )
    _key_sets: dict[str, set[tuple[Any, ...]]] = field(default_factory=dict, repr=False)
    _ref_value_sets: dict[tuple[str, tuple[str, ...]], set[tuple[Any, ...]]] = field(
        default_factory=dict, repr=False
    )
    _config: Config | None = field(default=None, repr=False)

    def __getitem__(self, table: str) -> list[dict[str, Any]]:
        """Return generated rows for ``table``."""
        return self.tables[table]


FkKey: TypeAlias = tuple[str, ...]
RandomState: TypeAlias = tuple[Any, ...]


@dataclass
class _SelectionWork:
    """Instrumentación **privada de tests** del trabajo de selección de FKs.

    No es API pública ni influye en la salida: cuenta las dos rutas que deben
    escalar de forma lineal (revisión PR #45) para que las regresiones de
    complejidad verifiquen una cota estructural sin depender de un umbral
    temporal frágil. Se reinicia al comenzar cada `generate_dataset`.

    - ``filter_scans``: candidatos examinados al filtrar por FKs obligatorias
      compartidas; las proyecciones se construyen una vez por tabla.
    - ``bridge_pairs_examined``: pares de padres inspeccionados por la
      deduplicación del puente; se consume una enumeración barajada una sola
      vez, nunca se reconstruye el producto cartesiano por colisión.
    """

    filter_scans: int = 0
    bridge_pairs_examined: int = 0

    def reset(self) -> None:
        """Pone ambos contadores a cero al inicio de una generación."""
        self.filter_scans = 0
        self.bridge_pairs_examined = 0


_SELECTION_WORK = _SelectionWork()


def complete_batch(batch: list[dict[str, Any]]) -> None:
    """Batch-content seam reserved for ``llm_group`` in H3B.

    It is intentionally empty today, but the engine calls it once with every
    complete generated batch before structural validation.
    """


def generate_dataset(spec: SchemaSpec, config: Config) -> Dataset:
    """Compile and execute a schema into a deterministic in-memory dataset.

    Args:
        spec: Canonical schema IR. It is not mutated.
        config: Validated generation configuration.

    Returns:
        Generated rows, quarantine and applied deferred updates.

    Raises:
        PlanError: If generators or YAML rules cannot be compiled.
        GenerationError: If ``output.on_error`` is ``abort`` and a row is bad.
    """
    interpreted = interpret_checks(spec)
    structural = analyze_structure(interpreted)
    phases = resolve_cycles(structural, interpreted)
    try:
        plans = build_plan(interpreted, config)
    except MergePlanError as exc:
        raise PlanError(str(exc)) from exc

    compiled = _compile_tables(interpreted, plans, config)
    dataset = Dataset(
        tables={table.name: [] for table in interpreted.tables},
        phases=phases,
        table_plans=plans,
        _compiled=compiled,
        _config=config,
    )
    _install_run_context(interpreted, dataset)
    store = KeyStore()

    for phase in phases:
        if isinstance(phase, InsertPhase):
            null_by_table = {
                ref.table: (tuple(ref.columns), frozenset(ref.null_columns))
                for ref in phase.null_fks
            }
            for table_name in phase.tables:
                relation, null_columns = null_by_table.get(table_name, ((), frozenset()))
                _generate_table(
                    compiled[table_name],
                    config,
                    dataset,
                    store,
                    deferred_relation=relation,
                    deferred_null_columns=null_columns,
                )
        elif isinstance(phase, InsertLeveledPhase):
            _generate_leveled_table(compiled[phase.table], phase, config, dataset, store)
        elif isinstance(phase, DeferredPhase):
            # Constraints are physically deferred by the future database sink. In
            # memory, generate the tables in stable order and apply unresolved FKs
            # after all keys in the phase exist.
            for table_name in phase.tables:
                _generate_table(compiled[table_name], config, dataset, store, defer_validation=True)
            for table_name in phase.tables:
                _fill_missing_foreign_keys(table_name, (), config, dataset, store)
            for table_name in phase.tables:
                dataset._validation_unique.pop(table_name, None)
                rows = dataset.tables[table_name]
                ok, bad = validate_batch(rows, compiled[table_name].plan, interpreted, dataset)
                if bad:
                    if config.output.on_error == "abort":
                        _, columns, reason = bad[0]
                        raise GenerationError(
                            f"tabla {table_name}, columnas {', '.join(columns)}: {reason}"
                        )
                    dataset.quarantine.setdefault(table_name, []).extend(bad)
                dataset.tables[table_name] = ok
        elif isinstance(phase, UpdatePhase):
            _fill_missing_foreign_keys(phase.table, tuple(phase.columns), config, dataset, store)

    _enforce_referential_integrity(interpreted, dataset, store)
    return dataset


def _compile_tables(
    spec: SchemaSpec, plans: TablePlans, config: Config
) -> dict[str, _CompiledTable]:
    """Resolve every generator and parse every rule before generation starts."""
    by_table = {table.name: table for table in spec.tables}
    compiled: dict[str, _CompiledTable] = {}
    for plan in plans.tables:
        table = by_table[plan.table]
        table_config = config.tables.get(table.name)
        raw_rules = table_config.rules if table_config is not None else []
        rules: list[Rule] = []
        for text in raw_rules:
            try:
                rule = parse_rule(text)
            except RuleParseError as exc:
                raise PlanError(
                    f"tabla {table.name}: no se pudo compilar la regla {text!r}: {exc}"
                ) from exc
            _validate_rule_references(rule, table, spec)
            rules.append(rule)
        try:
            order = build_column_order(plan, rules)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc

        bounds_by_column: dict[str, list[Bound]] = {}
        derivations: dict[str, Derivation] = {}
        for rule in rules:
            bound = as_bound(rule)
            if bound is not None:
                bounds_by_column.setdefault(bound.column, []).append(bound)
            derivation = as_derivation(rule)
            if derivation is not None:
                derivations[derivation.column] = derivation

        columns_by_name = {column.name: column for column in table.columns}
        compiled_columns: dict[str, _CompiledColumn] = {}
        for column_plan in plan.columns:
            column = columns_by_name[column_plan.column]
            generator: Generator | None = None
            generator_spec = _spec_with_ir_bounds(column_plan.generator, column)
            _check_numeric_representable(generator_spec, column, table.name)
            if (
                generator_spec is not None
                and generator_spec.type != "fk"
                and column.name not in derivations
            ):
                try:
                    generator = resolve(generator_spec.model_copy(update={"unique": False}))
                except Exception as exc:
                    raise PlanError(
                        f"tabla {table.name}, columna {column.name}: no se pudo resolver "
                        f"el generador '{generator_spec.type}': {exc}"
                    ) from exc
            compiled_columns[column.name] = _CompiledColumn(
                column=column,
                spec=generator_spec,
                generator=generator,
                bounds=bounds_by_column.get(column.name, []),
                derivation=derivations.get(column.name),
            )
        compiled[table.name] = _CompiledTable(
            spec=table, plan=plan, columns=compiled_columns, order=order, rules=rules
        )
    return compiled


def _spec_with_ir_bounds(
    generator_spec: GeneratorSpec | None, column: ColumnSpec
) -> GeneratorSpec | None:
    """Ensure interpreted CHECK bounds constrain the executable generator."""
    if generator_spec is None or generator_spec.type == "fk":
        return generator_spec
    bounds: dict[str, Any] = {}
    for constraint in column.checks:
        derived = constraint.bounds_derived or {}
        if "equals" in derived:
            bounds["min"] = bounds["max"] = derived["equals"]
        for name in ("min", "max", "min_exclusive", "max_exclusive"):
            if name in derived:
                if name == "min" and name in bounds:
                    bounds[name] = max(bounds[name], derived[name])
                elif name == "max" and name in bounds:
                    bounds[name] = min(bounds[name], derived[name])
                else:
                    bounds[name] = derived[name]
    if not bounds:
        return generator_spec
    target_type = generator_spec.type
    if target_type == "fallback":
        if column.type.kind in {"integer", "numeric"}:
            target_type = "numeric_range"
        elif column.type.kind in {"date", "timestamp"}:
            target_type = "datetime_range"
        else:
            return generator_spec
    if target_type not in {"numeric_range", "datetime_range"}:
        return generator_spec
    params = dict(generator_spec.params)
    for name, value in bounds.items():
        if target_type == "datetime_range" and name.endswith("_exclusive"):
            continue
        params.setdefault(name, value)
    return generator_spec.model_copy(update={"type": target_type, "params": params})


def _check_numeric_representable(
    spec: GeneratorSpec | None, column: ColumnSpec, table_name: str
) -> None:
    """Rechaza en compilación un rango que ningún valor de `NUMERIC(p, s)` cumple.

    Un `NUMERIC(p, s)` solo almacena magnitudes hasta ``(10**p - 1)/10**s``, en
    múltiplos exactos de su escala. No basta con comprobar que el rango pedido
    (o el derivado de un CHECK) se solapa con esa ventana como intervalos
    reales: la rejilla de la escala o una exclusividad (`min_exclusive`,
    `max_exclusive`) pueden dejarlo sin ningún valor cuantizable aunque el
    solape real sea no vacío (`has_quantized_value`). Si ningún valor
    representable lo cumple, ninguna fila sería válida: es una contradicción de
    plan que se rechaza con un error accionable, no una fila que cuarentenar
    (CLAUDE.md).
    """
    if spec is None or spec.type != "numeric_range":
        return
    type_spec = column.type
    if type_spec.kind != "numeric" or type_spec.precision is None:
        return
    raw_min = spec.params.get("min")
    raw_max = spec.params.get("max")
    min_exclusive = bool(spec.params.get("min_exclusive", False))
    max_exclusive = bool(spec.params.get("max_exclusive", False))
    if has_quantized_value(
        type_spec.precision,
        type_spec.scale,
        low=raw_min,
        high=raw_max,
        min_exclusive=min_exclusive,
        max_exclusive=max_exclusive,
    ):
        return
    limit = representable_limit(type_spec.precision, type_spec.scale)
    scale = effective_scale(type_spec.scale)
    requested = (
        f"[{raw_min if raw_min is not None else 'sin mínimo'}, "
        f"{raw_max if raw_max is not None else 'sin máximo'}]"
        f" (min_exclusive={min_exclusive}, max_exclusive={max_exclusive})"
    )
    raise PlanError(
        f"tabla {table_name}, columna {column.name}: el rango {requested} del generador "
        f"'numeric_range' no contiene ningún valor representable en "
        f"NUMERIC({type_spec.precision}, {scale}) una vez aplicadas la rejilla de la escala "
        f"y las exclusividades (máximo representable ±{limit}). Ajusta el rango, sus "
        f"exclusividades o el tipo de la columna."
    )


def _validate_rule_references(rule: Rule, table: TableSpec, spec: SchemaSpec) -> None:
    """Fail compilation when a local or parent column referenced by a rule is absent."""
    local = {column.name for column in table.columns}
    missing = sorted(referenced_columns(rule.root) - local)
    if missing:
        raise PlanError(
            f"tabla {table.name}: la regla {rule.text!r} referencia columnas inexistentes: "
            f"{', '.join(missing)}. Corrige la regla o el YAML."
        )
    by_name = index_tables(spec)
    relationships = {local_column: fk for fk in table.foreign_keys for local_column in fk.columns}
    for parent_ref in _parent_references(rule.root):
        fk = relationships.get(parent_ref.fk)
        if fk is None:
            raise PlanError(
                f"tabla {table.name}: la regla {rule.text!r} usa parent({parent_ref.fk}), "
                "pero esa columna no pertenece a ninguna FK de la tabla."
            )
        parent = by_name.get(fk.ref_table)
        parent_columns = {column.name for column in parent.columns} if parent is not None else set()
        if parent_ref.column not in parent_columns:
            raise PlanError(
                f"tabla {table.name}: la regla {rule.text!r} referencia "
                f"parent({parent_ref.fk}).{parent_ref.column}, pero la tabla padre "
                f"'{fk.ref_table}' no tiene esa columna."
            )


def _parent_references(node: Node) -> list[ParentCol]:
    if isinstance(node, ParentCol):
        return [node]
    if isinstance(node, Call):
        return [item for arg in node.args for item in _parent_references(arg)]
    if isinstance(node, Compare | Arith | BoolOp):
        return [*_parent_references(node.left), *_parent_references(node.right)]
    if isinstance(node, Not | Neg):
        return _parent_references(node.operand)
    if isinstance(node, Const | Col | Ref):
        return []
    return []


def _row_count(table: str, config: Config) -> int:
    tconf = config.tables.get(table)
    return tconf.rows if tconf is not None and tconf.rows is not None else config.defaults.rows


def _quarantined_empty_parent_relations(
    compiled: _CompiledTable, config: Config, dataset: Dataset
) -> frozenset[FkKey]:
    """Marca FKs obligatorias cuyo padre quedó completamente en cuarentena.

    Una ausencia causada por una cuarentena previa no es una incompatibilidad
    de selección de la fila actual: con ``on_error=quarantine`` la fila actual
    también debe poder apartarse para que el cierre RI continúe. Si hay padres
    aceptados, o si el modo es ``abort``, la selección sigue siendo estricta y
    un conjunto incompatible produce ``GenerationError``.
    """
    if config.output.on_error != "quarantine":
        return frozenset()
    unresolved: set[FkKey] = set()
    for fk in compiled.spec.foreign_keys:
        if _nullable_fk_columns(fk):
            continue
        parent_name = _CURRENT_TABLE_INDEX[fk.ref_table].name
        if not dataset.tables[parent_name] and dataset.quarantine.get(parent_name):
            unresolved.add(tuple(fk.columns))
    return frozenset(unresolved)


def _generate_table(
    compiled: _CompiledTable,
    config: Config,
    dataset: Dataset,
    store: KeyStore,
    *,
    deferred_relation: FkKey = (),
    deferred_null_columns: frozenset[str] = frozenset(),
    defer_validation: bool = False,
) -> None:
    total = _row_count(compiled.spec.name, config)
    unresolved_relations = _quarantined_empty_parent_relations(compiled, config, dataset)
    assignments, rng_states = _prepare_fk_assignments(
        compiled,
        total,
        config,
        dataset,
        store,
        deferred_relation=deferred_relation,
        deferred_null_columns=deferred_null_columns,
        allow_missing_parents=defer_validation,
        unresolved_relations=unresolved_relations,
    )
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    batch_size = config.output.batch_size
    for start in range(0, total, batch_size):
        batch: list[dict[str, Any]] = []
        for row_number in range(start, min(total, start + batch_size)):
            rng = rng_for_row(table_seed, row_number)
            rng.setstate(rng_states[row_number])
            row, parents, states = _generate_row(
                compiled,
                row_number,
                rng,
                assignments[row_number],
                config,
                null_columns_by_fk=(
                    {deferred_relation: deferred_null_columns} if deferred_relation else {}
                ),
                allow_unresolved=defer_validation,
                unresolved_fks=unresolved_relations,
            )
            batch.append(row)
            dataset._parents.setdefault(compiled.spec.name, {})[id(row)] = parents
            dataset._row_numbers.setdefault(compiled.spec.name, {})[id(row)] = row_number
            dataset._rng_states.setdefault(compiled.spec.name, {})[id(row)] = states
        complete_batch(batch)
        _accept_batch(compiled, batch, config, dataset, store, defer_validation=defer_validation)


def _prepare_fk_assignments(
    compiled: _CompiledTable,
    total: int,
    config: Config,
    dataset: Dataset,
    store: KeyStore,
    *,
    deferred_relation: FkKey,
    deferred_null_columns: frozenset[str],
    allow_missing_parents: bool = False,
    unresolved_relations: frozenset[FkKey] = frozenset(),
) -> tuple[list[dict[FkKey, int | None]], list[RandomState]]:
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    row_rngs = [rng_for_row(table_seed, index) for index in range(total)]
    states = _build_fk_selection_states(
        compiled, total, config, dataset, store, row_rngs, deferred_relation, unresolved_relations
    )
    assignments: list[dict[FkKey, int | None]] = []
    for row_index, rng in enumerate(row_rngs):
        row_assignments = _select_fk_assignments(
            compiled,
            row_index,
            rng,
            states,
            deferred_relation=deferred_relation,
            allow_missing_parents=allow_missing_parents,
            unresolved_relations=unresolved_relations,
        )
        assignments.append(row_assignments)
    _deduplicate_bridge_assignments(compiled, assignments, dataset)
    return assignments, [rng.getstate() for rng in row_rngs]


def _usable_quota_parents(
    compiled: _CompiledTable,
    fk: RelationshipSpec,
    dataset: Dataset,
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey],
) -> list[int]:
    """Padres de una FK con cuota que tienen combinación en las demás FKs obligatorias.

    La cuota reparte hijos entre TODOS los padres, pero una FK compartida obliga a
    un discriminador: un padre cuyo discriminador no exista en la otra relación no
    puede recibir ningún hijo sin dejar esa otra FK colgando. Devuelve, en orden de
    inserción, solo los índices de padre utilizables, para que el reparto respete
    `min/max` sin producir referencias incompatibles. Una FK obligatoria cuyo padre
    quedó totalmente en cuarentena (``unresolved_relations``) no restringe: la fila
    hija se apartará por esa vía.
    """
    parent_name = _CURRENT_TABLE_INDEX[fk.ref_table].name
    parent_rows = dataset.tables[parent_name]
    fk_ref = dict(zip(fk.columns, fk.ref_columns, strict=True))
    constraints: list[tuple[list[str], set[tuple[Any, ...]]]] = []
    for other in compiled.spec.foreign_keys:
        if other is fk or tuple(other.columns) == deferred_relation:
            continue
        if _nullable_fk_columns(other) or tuple(other.columns) in unresolved_relations:
            continue
        shared = [column for column in fk.columns if column in other.columns]
        if not shared:
            continue
        other_ref = dict(zip(other.columns, other.ref_columns, strict=True))
        other_rows = dataset.tables[_CURRENT_TABLE_INDEX[other.ref_table].name]
        present = {
            tuple(_marker(row.get(other_ref[column])) for column in shared) for row in other_rows
        }
        constraints.append((shared, present))
    if not constraints:
        return list(range(len(parent_rows)))
    return [
        index
        for index, row in enumerate(parent_rows)
        if all(
            tuple(_marker(row.get(fk_ref[column])) for column in shared) in present
            for shared, present in constraints
        )
    ]


def _build_shared_quota(
    compiled: _CompiledTable,
    fk: RelationshipSpec,
    dataset: Dataset,
    quota_rng: Random,
    non_null: list[int],
    total: int,
    params: dict[str, Any],
    parent_count: int,
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey],
) -> list[int]:
    """Reparte una cuota respetando las FKs compartidas, o falla de forma accionable.

    Solo se reparte entre padres utilizables (con combinación compatible en las
    demás FKs obligatorias compartidas). Si `min > 0` y algún padre es inutilizable
    no podría alcanzar su mínimo: es una contradicción de plan y se rechaza con un
    error que nombra la FK, la cuota y la incompatibilidad (CLAUDE.md: las
    contradicciones se rechazan, no se relajan). El reparto sobre padres utilizables
    es idéntico al normal cuando todos lo son, así que no altera las cuotas ya
    factibles.
    """
    usable = _usable_quota_parents(compiled, fk, dataset, deferred_relation, unresolved_relations)
    min_per, max_per = int(params["min"]), int(params["max"])
    incompatible = parent_count - len(usable)
    relation = f"FK ({', '.join(fk.columns)}) -> {fk.ref_table}"
    if incompatible > 0 and min_per > 0:
        raise GenerationError(
            f"tabla {compiled.spec.name}, {relation} con cuota [min={min_per}, max={max_per}]: "
            f"{incompatible} de {parent_count} padres no tienen ninguna combinación compatible "
            f"en las FKs que comparten columnas, pero min={min_per} exige que cada padre reciba "
            f"al menos {min_per} hijo(s). Añade padres compatibles, baja 'min' o revisa las "
            "columnas compartidas de la relación."
        )
    try:
        positions = build_quota_assignment(quota_rng, len(usable), len(non_null), min_per, max_per)
    except QuotaInfeasibleError as exc:
        if incompatible > 0:
            raise GenerationError(
                f"tabla {compiled.spec.name}, {relation} con cuota [min={min_per}, max={max_per}]: "
                f"solo {len(usable)} de {parent_count} padres tienen combinación compatible en las "
                f"FKs compartidas y no alcanzan para {len(non_null)} hijos. Añade padres "
                "compatibles o ajusta la cuota."
            ) from exc
        raise
    quota = [-1] * total
    for row_index, position in zip(non_null, positions, strict=True):
        quota[row_index] = usable[position]
    return quota


@dataclass
class _PendingQuota:
    """Una FK de cuota compartida a la espera de coordinarse con las demás."""

    state: _FkSelectionState
    non_null: list[int]
    quota_rng: Random
    params: dict[str, Any]
    parent_count: int


def _shared_columns_set(compiled: _CompiledTable, deferred_relation: FkKey) -> set[str]:
    """Columnas locales compartidas por ≥2 FKs (contando la relación diferida)."""
    seen: dict[str, int] = {}
    for fk in compiled.spec.foreign_keys:
        for column in fk.columns:
            seen[column] = seen.get(column, 0) + 1
    shared = {column for column, count in seen.items() if count >= 2}
    return shared | set(deferred_relation)


def _assign_shared_quotas(
    compiled: _CompiledTable,
    pending: list[_PendingQuota],
    dataset: Dataset,
    total: int,
    config: Config,
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey],
) -> None:
    """Rellena ``state.quota`` de las FKs de cuota compartidas de una tabla.

    Con una sola FK de cuota compartida basta el reparto por padres utilizables.
    Con varias que comparten un discriminador hay que coordinarlas: cada padre de
    cada tabla debe respetar su ``min/max`` y los discriminadores de un mismo hijo
    deben coincidir, así que el reparto se hace por grupo compartido.
    """
    if not pending:
        return
    if len(pending) == 1:
        entry = pending[0]
        entry.state.quota = _build_shared_quota(
            compiled,
            entry.state.fk,
            dataset,
            entry.quota_rng,
            entry.non_null,
            total,
            entry.params,
            entry.parent_count,
            deferred_relation,
            unresolved_relations,
        )
        return
    _build_coordinated_shared_quotas(
        compiled, pending, dataset, total, config, deferred_relation, unresolved_relations
    )


def _build_coordinated_shared_quotas(
    compiled: _CompiledTable,
    pending: list[_PendingQuota],
    dataset: Dataset,
    total: int,
    config: Config,
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey],
) -> None:
    """Coordina varias cuotas compartidas por su discriminador común.

    Cada grupo (valor del discriminador compartido) recibe un número de hijos
    ``n_g`` factible **a la vez** para todas las cuotas —dentro de la intersección
    de ``[|padres_g|·min, |padres_g|·max]``— y luego cada cuota reparte esos
    ``n_g`` hijos entre sus padres del grupo. Así ambos lados cumplen ``min/max``
    y comparten tenant sin relajar nada. Si un padre queda en un grupo sin
    asignación conjunta posible con ``min>0``, o si los grupos no alojan las
    filas, se rechaza con un ``GenerationError`` que nombra tabla, relaciones,
    cuotas y causa (CLAUDE.md: las contradicciones se rechazan, no se relajan).
    """
    fks = [entry.state.fk for entry in pending]
    common = set(fks[0].columns)
    for fk in fks[1:]:
        common &= set(fk.columns)
    disc = sorted(common & _shared_columns_set(compiled, deferred_relation))
    if not disc:
        # Sin discriminador común no hay conflicto: cada cuota es independiente.
        for entry in pending:
            entry.state.quota = _build_shared_quota(
                compiled,
                entry.state.fk,
                dataset,
                entry.quota_rng,
                entry.non_null,
                total,
                entry.params,
                entry.parent_count,
                deferred_relation,
                unresolved_relations,
            )
        return

    rows_to_place = pending[0].non_null
    if any(entry.non_null != rows_to_place for entry in pending[1:]):
        raise GenerationError(
            f"tabla {compiled.spec.name}: no se pueden coordinar cuotas compartidas con "
            "patrones de NULL distintos. Usa el mismo null_ratio (o ninguno) en las FKs de "
            "cuota que comparten columnas."
        )

    group_parents = [_group_parents_by_disc(entry.state.fk, disc, dataset) for entry in pending]
    bounds = [(int(e.params["min"]), int(e.params["max"])) for e in pending]
    others = _other_shared_supports(
        compiled, fks, disc, dataset, deferred_relation, unresolved_relations
    )

    ranges: dict[tuple[Any, ...], tuple[int, int]] = {}
    all_groups = {group for parents in group_parents for group in parents}
    for group in sorted(all_groups):
        present_all = all(group in parents for parents in group_parents)
        supported = present_all and all(
            tuple(group[index] for index in sub_idx) in present for sub_idx, present in others
        )
        if supported:
            paired = list(zip(group_parents, bounds, strict=True))
            lo = max(len(parents[group]) * mn for parents, (mn, _) in paired)
            hi = min(len(parents[group]) * mx for parents, (_, mx) in paired)
            if lo <= hi:
                ranges[group] = (lo, hi)
                continue
        # El grupo no admite un reparto conjunto: ningún padre de min>0 puede quedar ahí.
        for entry, parents in zip(pending, group_parents, strict=True):
            if group in parents and int(entry.params["min"]) > 0:
                raise _coordinated_quota_error(compiled, pending, disc, group)

    needed = len(rows_to_place)
    order = sorted(ranges)
    total_lo = sum(lo for lo, _ in ranges.values())
    total_hi = sum(hi for _, hi in ranges.values())
    if not total_lo <= needed <= total_hi:
        raise GenerationError(
            f"tabla {compiled.spec.name}: las cuotas compartidas de "
            f"{_relations_label(pending)} no pueden alojar conjuntamente {needed} hijos; "
            f"el total factible entre los grupos compatibles está en [{total_lo}, {total_hi}]. "
            "Ajusta las cuotas, el número de padres o el de filas."
        )

    coord_rng = Random(seed_for_table(config.seed, f"{compiled.spec.name}:coordquota"))
    counts = _distribute_rows_over_groups(order, ranges, needed, coord_rng)
    labels: list[tuple[Any, ...]] = []
    for group in order:
        labels.extend([group] * counts[group])
    coord_rng.shuffle(labels)
    rows_by_group: dict[tuple[Any, ...], list[int]] = {group: [] for group in order}
    for row_index, group in zip(rows_to_place, labels, strict=True):
        rows_by_group[group].append(row_index)

    for entry in pending:
        entry.state.quota = [-1] * total
    for group in order:
        rows = rows_by_group[group]
        for entry, parents, (mn, mx) in zip(pending, group_parents, bounds, strict=True):
            positions = build_quota_assignment(
                entry.quota_rng, len(parents[group]), len(rows), mn, mx
            )
            quota = entry.state.quota
            assert quota is not None
            for row_index, position in zip(rows, positions, strict=True):
                quota[row_index] = parents[group][position]


def _group_parents_by_disc(
    fk: RelationshipSpec, disc: list[str], dataset: Dataset
) -> dict[tuple[Any, ...], list[int]]:
    """Índices de padres de ``fk`` agrupados por su proyección sobre ``disc``."""
    ref = dict(zip(fk.columns, fk.ref_columns, strict=True))
    parent_rows = dataset.tables[_CURRENT_TABLE_INDEX[fk.ref_table].name]
    by_disc: dict[tuple[Any, ...], list[int]] = {}
    for index, row in enumerate(parent_rows):
        projection = tuple(_marker(row.get(ref[column])) for column in disc)
        by_disc.setdefault(projection, []).append(index)
    return by_disc


def _other_shared_supports(
    compiled: _CompiledTable,
    quota_fks: list[RelationshipSpec],
    disc: list[str],
    dataset: Dataset,
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey],
) -> list[tuple[list[int], set[tuple[Any, ...]]]]:
    """Soporte por grupo de las FKs obligatorias compartidas que no son de cuota.

    Devuelve, por cada FK obligatoria que comparte parte del discriminador, los
    índices de ``disc`` que restringe y los valores presentes en sus padres, para
    descartar grupos que dejarían esa relación sin padre.
    """
    supports: list[tuple[list[int], set[tuple[Any, ...]]]] = []
    for fk in compiled.spec.foreign_keys:
        if fk in quota_fks or tuple(fk.columns) == deferred_relation:
            continue
        if _nullable_fk_columns(fk) or tuple(fk.columns) in unresolved_relations:
            continue
        sub = [column for column in disc if column in fk.columns]
        if not sub:
            continue
        sub_idx = [disc.index(column) for column in sub]
        ref = dict(zip(fk.columns, fk.ref_columns, strict=True))
        rows = dataset.tables[_CURRENT_TABLE_INDEX[fk.ref_table].name]
        present = {tuple(_marker(row.get(ref[column])) for column in sub) for row in rows}
        supports.append((sub_idx, present))
    return supports


def _distribute_rows_over_groups(
    order: list[tuple[Any, ...]],
    ranges: dict[tuple[Any, ...], tuple[int, int]],
    needed: int,
    rng: Random,
) -> dict[tuple[Any, ...], int]:
    """Reparte ``needed`` hijos entre grupos dentro de ``[lo, hi]`` cada uno."""
    counts = {group: ranges[group][0] for group in order}
    remaining = needed - sum(counts.values())
    available = [group for group in order if ranges[group][1] - ranges[group][0] > 0]
    while remaining > 0:
        pos = rng.randrange(len(available))
        group = available[pos]
        counts[group] += 1
        remaining -= 1
        if counts[group] == ranges[group][1]:
            available[pos] = available[-1]
            available.pop()
    return counts


def _relations_label(pending: list[_PendingQuota]) -> str:
    return "; ".join(
        f"({', '.join(entry.state.fk.columns)}) -> {entry.state.fk.ref_table} "
        f"[min={entry.params['min']}, max={entry.params['max']}]"
        for entry in pending
    )


def _coordinated_quota_error(
    compiled: _CompiledTable,
    pending: list[_PendingQuota],
    disc: list[str],
    group: tuple[Any, ...],
) -> GenerationError:
    discriminator = ", ".join(
        f"{column}={value!r}" for column, value in zip(disc, group, strict=True)
    )
    return GenerationError(
        f"tabla {compiled.spec.name}: las cuotas compartidas {_relations_label(pending)} no "
        f"tienen una asignación conjunta para el discriminador {discriminator}: algún padre con "
        "min>0 quedaría sin hijos porque la otra relación no tiene ningún padre compatible en ese "
        "grupo. Añade padres compatibles en todas las relaciones, baja 'min' o revisa las columnas "
        "compartidas."
    )


def _build_fk_selection_states(
    compiled: _CompiledTable,
    total: int,
    config: Config,
    dataset: Dataset,
    store: KeyStore,
    row_rngs: list[Random],
    deferred_relation: FkKey,
    unresolved_relations: frozenset[FkKey] = frozenset(),
) -> dict[FkKey, _FkSelectionState]:
    """Prepara los selectores y cuotas que comparten una tabla completa."""
    foreign_keys = [
        fk for fk in compiled.spec.foreign_keys if tuple(fk.columns) != deferred_relation
    ]
    states: dict[FkKey, _FkSelectionState] = {}
    pending_shared_quota: list[_PendingQuota] = []
    for fk in foreign_keys:
        key = tuple(fk.columns)
        parent_name = _CURRENT_TABLE_INDEX[fk.ref_table].name
        parent_rows = dataset.tables[parent_name]
        parent_count = store.count(parent_name)
        params = _fk_params(compiled, fk, config)
        strategy = str(params.get("strategy", "uniform"))
        shared_columns = any(
            fk is not other
            and set(fk.columns).intersection(other.columns)
            and tuple(other.columns) != deferred_relation
            for other in foreign_keys
        ) or bool(set(fk.columns).intersection(deferred_relation))
        state = _FkSelectionState(
            fk=fk,
            parent_rows=parent_rows,
            strategy=strategy,
            params=params,
            shared_columns=shared_columns,
            all_indices=tuple(range(len(parent_rows))),
        )
        if parent_count == 0:
            states[key] = state
            continue

        null_ratio = float(params.get("null_ratio", 0.0))
        if strategy == "quota":
            non_null = [i for i, rng in enumerate(row_rngs) if rng.random() >= null_ratio]
            quota_rng = Random(seed_for_table(config.seed, f"{compiled.spec.name}:{','.join(key)}"))
            if shared_columns:
                # Se difiere: varias cuotas compartidas deben coordinarse por sus
                # discriminadores comunes; prepararlas por separado y priorizar una
                # solo hace que la otra choque con el tenant que la primera fijó
                # (revisión de d86e249, bloqueante 1).
                pending_shared_quota.append(
                    _PendingQuota(state, non_null, quota_rng, dict(params), parent_count)
                )
            else:
                quota = build_quota_assignment(
                    quota_rng,
                    parent_count,
                    len(non_null),
                    int(params["min"]),
                    int(params["max"]),
                )
                state.quota = [-1] * total
                for row_index, parent_index in zip(non_null, quota, strict=True):
                    state.quota[row_index] = parent_index
        elif strategy == "zipf":
            state.selector = ZipfSelector(parent_count, float(params.get("s", 1.2)))
        elif strategy == "unique_subset":
            if shared_columns:
                state.unique_remaining = set(range(parent_count))
            else:
                state.selector = UniqueSubsetSelector(parent_count, total, compiled.spec.name)
        else:
            state.selector = UniformSelector(parent_count)
        if state.selector is not None and null_ratio:
            state.selector = NullRatioSelector(state.selector, null_ratio)
        states[key] = state
    _assign_shared_quotas(
        compiled,
        pending_shared_quota,
        dataset,
        total,
        config,
        deferred_relation,
        unresolved_relations,
    )
    return states


def _ordered_foreign_keys(
    compiled: _CompiledTable, deferred_relation: FkKey
) -> list[RelationshipSpec]:
    """Ordena FKs compartidas poniendo primero las que no pueden ser NULL."""
    foreign_keys = [
        fk for fk in compiled.spec.foreign_keys if tuple(fk.columns) != deferred_relation
    ]
    has_shared_columns = bool(deferred_relation) or any(
        set(fk.columns).intersection(other.columns)
        for index, fk in enumerate(foreign_keys)
        for other in foreign_keys[index + 1 :]
    )
    if not has_shared_columns:
        return foreign_keys
    return [
        fk
        for _, fk in sorted(
            enumerate(foreign_keys),
            key=lambda item: (
                bool(_nullable_fk_columns(item[1])),
                -len(item[1].columns),
                item[0],
            ),
        )
    ]


def _prioritize_shared_quota(
    ordered_fks: list[RelationshipSpec], states: dict[FkKey, _FkSelectionState]
) -> list[RelationshipSpec]:
    """Procesa primero una FK de cuota compartida dentro de una tabla compartida.

    Una cuota fija el padre de cada fila de antemano; si otra FK compartida eligiera
    antes el discriminador, la cuota chocaría con él. Poniéndola primero, su padre
    fija el discriminador y las demás FKs compartidas se ajustan. Es una reordenación
    estable acotada a las FKs de cuota compartidas: para cualquier tabla sin ellas el
    orden es idéntico al de `_ordered_foreign_keys`, así que no altera la selección de
    las tablas ya existentes.
    """
    return sorted(
        ordered_fks,
        key=lambda fk: (
            0
            if ((state := states[tuple(fk.columns)]).shared_columns and state.strategy == "quota")
            else 1
        ),
    )


def _nullable_fk_columns(fk: RelationshipSpec) -> tuple[str, ...]:
    """Devuelve las columnas que pueden anular una FK con su semántica MATCH."""
    if not fk.nullable_columns:
        return ()
    if fk.match_full and len(fk.nullable_columns) != len(fk.columns):
        return ()
    return tuple(fk.nullable_columns)


def _parent_indices_for_values(
    state: _FkSelectionState, local_values: dict[str, Any]
) -> Sequence[int]:
    """Obtiene padres compatibles mediante un índice de proyecciones locales."""
    constrained_columns = tuple(
        local
        for local in state.fk.columns
        if local in local_values and local_values[local] is not None
    )
    if not constrained_columns:
        return state.all_indices
    by_projection = state.parent_indices_by_projection.get(constrained_columns)
    if by_projection is None:
        refs = tuple(
            ref
            for local, ref in zip(state.fk.columns, state.fk.ref_columns, strict=True)
            if local in constrained_columns
        )
        by_projection = {}
        for index, parent in enumerate(state.parent_rows):
            key = tuple(_marker(parent.get(ref)) for ref in refs)
            by_projection.setdefault(key, []).append(index)
        state.parent_indices_by_projection[constrained_columns] = by_projection
    key = tuple(_marker(local_values[local]) for local in constrained_columns)
    return by_projection.get(key, ())


def _merge_parent_local_values(
    fk: RelationshipSpec, parent: dict[str, Any], local_values: dict[str, Any]
) -> dict[str, Any] | None:
    """Añade los valores locales de un padre o devuelve `None` si hay conflicto."""
    merged = dict(local_values)
    for local, ref in zip(fk.columns, fk.ref_columns, strict=True):
        value = parent.get(ref)
        if local in merged and merged[local] is not None and merged[local] != value:
            return None
        merged[local] = value
    return merged


def _filter_candidates_by_required_fks(
    candidates: Sequence[int],
    state: _FkSelectionState,
    local_values: dict[str, Any],
    remaining_fks: list[RelationshipSpec],
    states: dict[FkKey, _FkSelectionState],
    unresolved_relations: frozenset[FkKey],
    *,
    allow_missing_parents: bool,
) -> list[int]:
    """Descarta padres que dejan sin combinación a otra FK obligatoria compartida.

    La selección de una fila es un pequeño problema de compatibilidad: una FK
    puede fijar un discriminador que otra FK necesita. Mirar una fila de cada
    relación futura evita escoger al azar un padre que luego obligue a abortar,
    sin reordenar ni consumir el estado determinista de los selectores.

    La factibilidad de un candidato depende solo de los valores compartidos que
    fija (no de qué fila lo pide), así que el resultado se memoiza por el conjunto
    de valores locales ya fijados. En una tabla normal ese conjunto es el mismo
    para todas las filas, de modo que el filtro se calcula una vez por tabla en
    lugar de una vez por fila: el coste deja de ser filas × padres (revisión PR
    #45, hallazgo 3). La caché se omite cuando una FK obligatoria restante usa
    `unique_subset` compartido, cuyo soporte se agota fila a fila.
    """
    constraining = [
        other
        for other in remaining_fks
        if not _nullable_fk_columns(other) and tuple(other.columns) not in unresolved_relations
    ]
    cache_key: tuple[Any, ...] | None = None
    if all(states[tuple(other.columns)].unique_remaining is None for other in constraining):
        cache_key = tuple(
            sorted(
                (column, _marker(value))
                for column, value in local_values.items()
                if value is not None
            )
        )
        cached = state.filtered_candidates_cache.get(cache_key)
        if cached is not None:
            return cached
    supported: list[int] = []
    for candidate in candidates:
        _SELECTION_WORK.filter_scans += 1
        merged = _merge_parent_local_values(state.fk, state.parent_rows[candidate], local_values)
        if merged is None:
            continue
        feasible = True
        for other in constraining:
            other_state = states[tuple(other.columns)]
            if not other_state.parent_rows:
                if allow_missing_parents:
                    continue
                feasible = False
                break
            compatible = _parent_indices_for_values(other_state, merged)
            if other_state.unique_remaining is not None:
                compatible = [
                    index for index in compatible if index in other_state.unique_remaining
                ]
            if not compatible:
                feasible = False
                break
        if feasible:
            supported.append(candidate)
    if cache_key is not None:
        state.filtered_candidates_cache[cache_key] = supported
    return supported


def _pick_compatible_parent(
    state: _FkSelectionState,
    candidates: Sequence[int],
    rng: Random,
    row_index: int,
) -> int | None:
    """Selecciona un padre dentro de candidatos respetando la estrategia de la FK."""
    if not candidates:
        return None
    if state.strategy == "quota":
        if state.quota is None:
            raise AssertionError("FK quota sin asignación preparada")
        selected = state.quota[row_index]
        if selected < 0:
            return None
        if selected in candidates:
            return selected
        # La cuota es un contrato de tabla, no una preferencia: si el padre que le
        # tocó a esta fila es incompatible con un discriminador ya fijado por otra
        # FK compartida, reemplazarlo por uno aleatorio incumpliría min/max en
        # silencio (revisión PR #45, hallazgo 2). El reparto compatible se decide
        # antes, en `_build_shared_quota`; aquí un conflicto residual es un fallo
        # accionable, nunca una degradación muda.
        raise GenerationError(
            f"FK ({', '.join(state.fk.columns)}) -> {state.fk.ref_table}: la cuota asignó el "
            f"padre índice {selected}, pero las FKs que comparten columnas ya fijaron un "
            "discriminador incompatible para esta fila. La cuota no puede reasignarse en "
            "silencio; revisa el orden de las relaciones compartidas o su estrategia."
        )
    null_ratio = float(state.params.get("null_ratio", 0.0))
    if null_ratio and rng.random() < null_ratio:
        return None
    if state.strategy == "unique_subset":
        if state.unique_remaining is None:
            raise AssertionError("FK unique_subset sin estado de padres")
        available = [index for index in candidates if index in state.unique_remaining]
        if not available:
            return None
        selected = available[rng.randrange(len(available))]
        state.unique_remaining.remove(selected)
        return selected
    if state.strategy == "zipf":
        selector = ZipfSelector(len(candidates), float(state.params.get("s", 1.2)))
        position = selector.pick(rng)
        if position is None:
            raise AssertionError("ZipfSelector no devolvió un índice")
        return candidates[position]
    return candidates[rng.randrange(len(candidates))]


def _record_null_fk_values(fk: RelationshipSpec, local_values: dict[str, Any]) -> None:
    """Marca solo las columnas anulables de una FK sin borrar un discriminador fijado."""
    for local in _nullable_fk_columns(fk):
        local_values[local] = None


def _fk_selection_error(
    compiled: _CompiledTable,
    fk: RelationshipSpec,
    local_values: dict[str, Any],
) -> GenerationError:
    fixed = {
        local: local_values[local]
        for local in fk.columns
        if local in local_values and local_values[local] is not None
    }
    return GenerationError(
        f"tabla {compiled.spec.name}, FK ({', '.join(fk.columns)}) -> {fk.ref_table}: "
        f"no hay padre compatible con los valores locales fijados {fixed!r}. "
        f"Aumenta las filas de '{fk.ref_table}', revisa las FKs compartidas o haz "
        "anulables las columnas opcionales de esta relación."
    )


def _select_fk_assignments(
    compiled: _CompiledTable,
    row_index: int,
    rng: Random,
    states: dict[FkKey, _FkSelectionState],
    *,
    deferred_relation: FkKey,
    allow_missing_parents: bool,
    unresolved_relations: frozenset[FkKey] = frozenset(),
    initial_values: dict[str, Any] | None = None,
) -> dict[FkKey, int | None]:
    """Selecciona todas las FKs de una fila respetando los valores ya fijados."""
    assignments: dict[FkKey, int | None] = {}
    local_values = dict(initial_values or {})
    base_order = _ordered_foreign_keys(compiled, deferred_relation)
    ordered_fks = _prioritize_shared_quota(base_order, states)
    null_locked: set[str] = set()
    if deferred_relation:
        deferred_fk = next(
            fk for fk in compiled.spec.foreign_keys if tuple(fk.columns) == deferred_relation
        )
        if deferred_fk.match_full and len(_nullable_fk_columns(deferred_fk)) == len(
            deferred_fk.columns
        ):
            null_locked.update(deferred_fk.columns)
    for position, fk in enumerate(ordered_fks):
        key = tuple(fk.columns)
        state = states[key]
        candidates: Sequence[int] | None = None
        if null_locked.intersection(fk.columns):
            selected = None
        elif state.parent_rows and state.shared_columns:
            candidates = _parent_indices_for_values(state, local_values)
            candidates = _filter_candidates_by_required_fks(
                candidates,
                state,
                local_values,
                ordered_fks[position + 1 :],
                states,
                unresolved_relations,
                allow_missing_parents=allow_missing_parents,
            )
            selected = _pick_compatible_parent(state, candidates, rng, row_index)
        elif state.quota is not None:
            selected = state.quota[row_index]
            selected = None if selected < 0 else selected
        elif state.selector is not None:
            selected = state.selector.pick(rng)
        else:
            selected = None

        if selected is None:
            if key in unresolved_relations or (not state.parent_rows and allow_missing_parents):
                assignments[key] = None
                continue
            if not _nullable_fk_columns(fk):
                raise _fk_selection_error(compiled, fk, local_values)
            if fk.match_full and any(
                local in local_values and local_values[local] is not None for local in fk.columns
            ):
                raise _fk_selection_error(compiled, fk, local_values)
            assignments[key] = None
            _record_null_fk_values(fk, local_values)
            if fk.match_full:
                null_locked.update(fk.columns)
            continue

        parent = state.parent_rows[selected]
        if any(
            local in local_values
            and local_values[local] is not None
            and local_values[local] != parent.get(ref)
            for local, ref in zip(fk.columns, fk.ref_columns, strict=True)
        ):
            raise _fk_selection_error(compiled, fk, local_values)
        assignments[key] = selected
        for local, ref in zip(fk.columns, fk.ref_columns, strict=True):
            local_values[local] = parent.get(ref)
    for fk in compiled.spec.foreign_keys:
        key = tuple(fk.columns)
        if key == deferred_relation:
            assignments[key] = None
    return assignments


def _deduplicate_bridge_assignments(
    compiled: _CompiledTable,
    assignments: list[dict[FkKey, int | None]],
    dataset: Dataset,
) -> None:
    """Hace únicos los pares de una tabla puente sin recorrer el producto cartesiano.

    Cuando dos FKs comparten columnas (un puente multi-tenant), un par duplicado
    debe reconsiderarse completo: una combinación compatible por los valores
    compartidos y todavía sin usar (el nuevo padre no puede pertenecer a otro
    discriminador y romper el valor compartido, revisión PR #45, hallazgo 1).
    Cuando no comparten columnas, cualquier izquierda vale con cualquier derecha.
    En ambos casos la deduplicación consume UNA enumeración perezosa de
    combinaciones válidas con un cursor incremental: cada combinación se inspecciona
    a lo sumo una vez en toda la tabla, así que el coste es lineal en filas y no en
    colisiones × pares (revisión de d86e249, bloqueante 2). El cursor solo avanza lo
    necesario, sin materializar el producto completo cuando se piden pocas filas.
    """
    if compiled.spec.kind != "bridge" or len(compiled.spec.foreign_keys) < 2:
        return
    first, second = compiled.spec.foreign_keys[:2]
    first_key, second_key = tuple(first.columns), tuple(second.columns)
    shared = [column for column in first.columns if column in second.columns]
    left_count = len(dataset.tables[_CURRENT_TABLE_INDEX[first.ref_table].name])
    right_count = len(dataset.tables[_CURRENT_TABLE_INDEX[second.ref_table].name])
    if shared:
        left_rows = dataset.tables[_CURRENT_TABLE_INDEX[first.ref_table].name]
        right_rows = dataset.tables[_CURRENT_TABLE_INDEX[second.ref_table].name]
        groups = _bridge_compatible_pairs(first, second, shared, left_rows, right_rows)
    else:
        # Sin columnas compartidas cualquier izquierda vale con cualquier derecha:
        # un único grupo con todos los índices, sin materializar el producto.
        groups = {(): (list(range(left_count)), list(range(right_count)))}
    available = sum(len(lefts) * len(rights) for lefts, rights in groups.values())
    free_pairs = _iter_group_pairs(groups)
    seen: set[tuple[int, int]] = set()
    for assignment in assignments:
        left, right = assignment.get(first_key), assignment.get(second_key)
        if left is None or right is None:
            continue
        pair = (left, right)
        if pair not in seen:
            seen.add(pair)
            continue
        replacement = _next_free_bridge_pair(free_pairs, seen)
        if replacement is None:
            raise _bridge_exhausted_error(compiled.spec, shared, available, len(assignments))
        assignment[first_key], assignment[second_key] = replacement
        seen.add(replacement)


def _iter_group_pairs(
    groups: dict[tuple[Any, ...], tuple[list[int], list[int]]],
) -> Iterator[tuple[int, int]]:
    """Enumera perezosamente los pares (izquierda, derecha) válidos, grupo a grupo.

    Es un generador: solo produce combinaciones a medida que la deduplicación las
    pide, de modo que un puente con muchos padres pero pocas filas nunca materializa
    el producto cartesiano completo.
    """
    for lefts, rights in groups.values():
        for left in lefts:
            for right in rights:
                yield (left, right)


def _next_free_bridge_pair(
    free_pairs: Iterator[tuple[int, int]], seen: set[tuple[int, int]]
) -> tuple[int, int] | None:
    """Avanza el cursor de pares válidos hasta uno sin usar (``None`` si se agotan).

    El cursor es monótono y solo salta pares ya usados, de los que hay como mucho
    tantos como filas colocadas; por eso el total de pares inspeccionados en toda
    la tabla es lineal aunque haya muchas colisiones (no se reconstruye la lista de
    libres por colisión).
    """
    for pair in free_pairs:
        _SELECTION_WORK.bridge_pairs_examined += 1
        if pair not in seen:
            return pair
    return None


def _bridge_exhausted_error(
    table: TableSpec, shared: list[str], available: int, requested: int
) -> GenerationError:
    detail = (
        f"compatibles por las columnas compartidas ({', '.join(shared)})"
        if shared
        else "de FK distintas"
    )
    return GenerationError(
        f"tabla puente {table.name}: se piden {requested} filas pero solo existen "
        f"{available} combinaciones {detail} sin repetir. Reduce la cardinalidad o "
        "añade filas padre compatibles."
    )


def _bridge_compatible_pairs(
    first: RelationshipSpec,
    second: RelationshipSpec,
    shared: list[str],
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
) -> dict[tuple[Any, ...], tuple[list[int], list[int]]]:
    """Agrupa índices de padres izquierdos y derechos por sus valores compartidos.

    Solo los grupos presentes en AMBOS lados producen pares válidos; dentro de un
    grupo cualquier (izquierda, derecha) es compatible en las columnas compartidas.
    """
    first_ref = dict(zip(first.columns, first.ref_columns, strict=True))
    second_ref = dict(zip(second.columns, second.ref_columns, strict=True))
    lefts_by: dict[tuple[Any, ...], list[int]] = {}
    for index, row in enumerate(left_rows):
        lefts_by.setdefault(
            tuple(_marker(row.get(first_ref[column])) for column in shared), []
        ).append(index)
    rights_by: dict[tuple[Any, ...], list[int]] = {}
    for index, row in enumerate(right_rows):
        rights_by.setdefault(
            tuple(_marker(row.get(second_ref[column])) for column in shared), []
        ).append(index)
    return {
        group: (lefts, rights_by[group]) for group, lefts in lefts_by.items() if group in rights_by
    }


def _fk_params(compiled: _CompiledTable, fk: RelationshipSpec, config: Config) -> dict[str, Any]:
    table_config = config.tables.get(compiled.spec.name)
    if table_config is not None:
        for column_name in fk.columns:
            strategy = table_config.fk.get(column_name)
            if strategy is not None:
                return strategy.model_dump(exclude_none=True)
    if fk.cardinality_hint == "one_to_one":
        return {"strategy": "unique_subset"}
    return {"strategy": "uniform"}


def _generate_row(
    compiled: _CompiledTable,
    row_number: int,
    rng: Random,
    assignments: dict[FkKey, int | None],
    config: Config,
    *,
    initial_row: dict[str, Any] | None = None,
    initial_parents: dict[str, dict[str, Any] | None] | None = None,
    null_columns_by_fk: dict[FkKey, frozenset[str]] | None = None,
    allow_unresolved: bool = False,
    unresolved_fks: frozenset[FkKey] = frozenset(),
) -> tuple[dict[str, Any], dict[str, dict[str, Any] | None], dict[str, RandomState]]:
    row = dict(initial_row or {})
    parents = dict(initial_parents or {})
    states: dict[str, RandomState] = {}

    # The database would assign SERIAL values. The in-memory MVP mirrors the
    # deterministic values PostgreSQL would produce so they can enter KeyStore.
    for column in compiled.spec.columns:
        if column.type.autoincrement:
            row.setdefault(column.name, row_number + 1)

    dataset = _CURRENT_DATASET
    if dataset is None:
        raise AssertionError("dataset context not installed")
    _apply_fk_assignments(
        compiled,
        assignments,
        row,
        parents,
        dataset,
        null_columns_by_fk=null_columns_by_fk or {},
        allow_unresolved=allow_unresolved,
        unresolved_fks=unresolved_fks,
    )

    for column_name in compiled.order:
        if column_name in row:
            continue
        item = compiled.columns[column_name]
        if item.spec is None or (item.generator is None and item.derivation is None):
            continue
        ctx = RowContext(
            rng=rng,
            column=item.column,
            table=compiled.spec.name,
            row=row,
            refs=config.refs,
            resolve_parent=mapping_resolver(parents),
        )
        states[column_name] = rng.getstate()
        value = _generate_value(item, ctx)
        row[column_name] = value
    _ensure_compound_unique(compiled, row, parents, states, rng, config)
    return row, parents, states


def _apply_fk_assignments(
    compiled: _CompiledTable,
    assignments: dict[FkKey, int | None],
    row: dict[str, Any],
    parents: dict[str, dict[str, Any] | None],
    dataset: Dataset,
    *,
    null_columns_by_fk: dict[FkKey, frozenset[str]],
    allow_unresolved: bool,
    unresolved_fks: frozenset[FkKey],
) -> None:
    """Escribe FKs sin sobrescribir silenciosamente valores locales compartidos."""
    for fk in _ordered_foreign_keys(compiled, ()):
        key = tuple(fk.columns)
        parent_index = assignments.get(key)
        if parent_index is None:
            if key in null_columns_by_fk:
                null_columns = null_columns_by_fk[key]
            elif key in unresolved_fks or allow_unresolved:
                null_columns = frozenset(fk.columns)
            else:
                null_columns = frozenset(_nullable_fk_columns(fk))
            if (
                fk.match_full
                and not (key in unresolved_fks or allow_unresolved)
                and any(row.get(local_column) is not None for local_column in fk.columns)
            ):
                raise GenerationError(
                    f"tabla {compiled.spec.name}, FK ({', '.join(fk.columns)}): "
                    "MATCH FULL no permite anular solo una parte de la relación."
                )
            for local_column in null_columns:
                if row.get(local_column) is None:
                    row[local_column] = None
                    parents[local_column] = None
            continue
        parent_table = _CURRENT_TABLE_INDEX[fk.ref_table].name
        parent_row = dataset.tables[parent_table][parent_index]
        for local_column, ref_column in zip(fk.columns, fk.ref_columns, strict=True):
            value = parent_row[ref_column]
            if local_column in row and row[local_column] is not None:
                if row[local_column] != value:
                    raise GenerationError(
                        f"tabla {compiled.spec.name}, FK ({', '.join(fk.columns)}): "
                        f"la columna compartida '{local_column}' ya vale "
                        f"{row[local_column]!r}, pero el padre seleccionado exige {value!r}. "
                        "Revisa las relaciones que comparten esa columna."
                    )
            else:
                row[local_column] = value
            parents[local_column] = parent_row


_CURRENT_DATASET: Dataset | None = None
_CURRENT_SPEC: SchemaSpec
_CURRENT_TABLE_INDEX: dict[str, TableSpec]


def _generate_value(item: _CompiledColumn, ctx: RowContext) -> Any:
    if item.spec is None:
        return None
    if item.column.nullable and item.spec.null_ratio and ctx.rng.random() < item.spec.null_ratio:
        return None
    attempts = 50 if item.spec.unique else 1
    seen = _CURRENT_UNIQUE.setdefault((ctx.table, item.column.name), set())
    for _ in range(attempts):
        if item.column.type.is_array:
            length = ctx.rng.randint(0, 5)
            value = [_generate_scalar(item, ctx) for _ in range(length)]
            marker: Any = tuple(value)
        else:
            value = _generate_scalar(item, ctx)
            marker = value
        if not item.spec.unique or marker not in seen:
            if item.spec.unique:
                seen.add(marker)
            return value
    raise GenerationError(
        f"no se pudo generar un valor único para {ctx.table}.{item.column.name} "
        f"tras {attempts} intentos"
    )


_CURRENT_UNIQUE: dict[tuple[str, str], set[Any]] = {}
_CURRENT_COMPOSITE: dict[tuple[str, tuple[str, ...]], set[tuple[Any, ...]]] = {}


def _generate_scalar(item: _CompiledColumn, ctx: RowContext) -> Any:
    if item.derivation is not None:
        return evaluate(item.derivation.expr, ctx)
    generator = item.generator
    if item.bounds:
        dynamic = _bounded_spec(item, ctx)
        generator = resolve(dynamic.model_copy(update={"unique": False}))
    if generator is None:
        raise PlanError(
            f"tabla {ctx.table}, columna {item.column.name}: no hay generador compilado"
        )
    return generator.generate(ctx)


def _ensure_compound_unique(
    compiled: _CompiledTable,
    row: dict[str, Any],
    parents: dict[str, dict[str, Any] | None],
    states: dict[str, RandomState],
    rng: Random,
    config: Config,
) -> None:
    groups = (
        [compiled.spec.primary_key] if compiled.spec.primary_key else []
    ) + compiled.spec.uniques
    fk_columns = {column for fk in compiled.spec.foreign_keys for column in fk.columns}
    for raw_group in groups:
        if len(raw_group) < 2:
            continue
        group = tuple(raw_group)
        values = tuple(_marker(row.get(column)) for column in group)
        if any(value is None for value in values) and list(raw_group) != compiled.spec.primary_key:
            continue
        seen = _CURRENT_COMPOSITE.setdefault((compiled.spec.name, group), set())
        if values not in seen:
            seen.add(values)
            continue
        candidate_name = next(
            (
                name
                for name in reversed(raw_group)
                if name not in fk_columns
                and compiled.columns[name].spec is not None
                and not compiled.columns[name].column.type.autoincrement
            ),
            None,
        )
        if candidate_name is None:
            raise GenerationError(
                f"tabla {compiled.spec.name}: la clave compuesta {group} produjo "
                f"el duplicado {values!r} y no hay una columna regenerable."
            )
        item = compiled.columns[candidate_name]
        ctx = RowContext(
            rng=rng,
            column=item.column,
            table=compiled.spec.name,
            row=row,
            refs=config.refs,
            resolve_parent=mapping_resolver(parents),
        )
        for _ in range(50):
            states[candidate_name] = rng.getstate()
            row[candidate_name] = _generate_value(item, ctx)
            values = tuple(_marker(row.get(column)) for column in group)
            if values not in seen:
                seen.add(values)
                break
        else:
            raise GenerationError(
                f"tabla {compiled.spec.name}: no se pudo obtener una clave compuesta "
                f"única para {group} tras 50 intentos."
            )


def _marker(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_marker(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _marker(item)) for key, item in value.items()))
    return value


def _bounded_spec(item: _CompiledColumn, ctx: RowContext) -> GeneratorSpec:
    if item.spec is None:
        raise AssertionError("bounded column without generator spec")
    if item.spec.type not in {"numeric_range", "datetime_range"}:
        raise PlanError(
            f"tabla {ctx.table}, columna {item.column.name}: una regla bound requiere "
            f"numeric_range o datetime_range, no '{item.spec.type}'."
        )
    params = dict(item.spec.params)
    for bound in item.bounds:
        value = evaluate(bound.expr, ctx)
        name = "min" if bound.side == "lower" else "max"
        current = params.get(name)
        if (
            current is None
            or (name == "min" and value > current)
            or (name == "max" and value < current)
        ):
            params[name] = value
            if item.spec.type == "numeric_range" and bound.exclusive:
                params[f"{name}_exclusive"] = True
    return item.spec.model_copy(update={"params": params})


def _accept_batch(
    compiled: _CompiledTable,
    batch: list[dict[str, Any]],
    config: Config,
    dataset: Dataset,
    store: KeyStore,
    *,
    defer_validation: bool = False,
) -> None:
    ok: list[dict[str, Any]]
    bad: list[ValidationIssue]
    if defer_validation:
        ok, bad = batch, []
    else:
        ok, bad = validate_batch(batch, compiled.plan, _CURRENT_SPEC, dataset)
    if bad and config.output.on_error == "abort":
        _, columns, reason = bad[0]
        raise GenerationError(
            f"tabla {compiled.spec.name}, columnas {', '.join(columns)}: {reason}"
        )
    if bad:
        dataset.quarantine.setdefault(compiled.spec.name, []).extend(bad)
    dataset.tables[compiled.spec.name].extend(ok)
    keys = [tuple(row[column] for column in compiled.spec.primary_key) for row in ok]
    if compiled.spec.primary_key:
        store.add(compiled.spec.name, keys)
        dataset._key_sets.setdefault(compiled.spec.name, set()).update(keys)


def _generate_leveled_table(
    compiled: _CompiledTable,
    phase: InsertLeveledPhase,
    config: Config,
    dataset: Dataset,
    store: KeyStore,
) -> None:
    total = _row_count(compiled.spec.name, config)
    hierarchy = next(
        (
            config.hierarchy.get(f"{compiled.spec.name}.{column}")
            for column in phase.self_fk_columns
            if config.hierarchy.get(f"{compiled.spec.name}.{column}") is not None
        ),
        None,
    )
    branching = hierarchy.branching if hierarchy is not None else 5
    max_depth = hierarchy.max_depth if hierarchy is not None else max(total, 1)
    levels = _level_numbers(total, branching, max_depth)
    dataset.levels[compiled.spec.name] = []
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    row_rngs = [rng_for_row(table_seed, row_number) for row_number in range(total)]
    rows: list[dict[str, Any]] = []
    indices_by_level: dict[int, list[int]] = {}
    self_fk = _self_fk(compiled.spec, phase.self_fk_columns, _CURRENT_TABLE_INDEX)
    self_fk_key = tuple(phase.self_fk_columns)
    unresolved_relations = _quarantined_empty_parent_relations(compiled, config, dataset)
    selection_states = _build_fk_selection_states(
        compiled,
        total,
        config,
        dataset,
        store,
        row_rngs,
        self_fk_key,
        unresolved_relations,
    )
    for start in range(0, total, config.output.batch_size):
        batch: list[dict[str, Any]] = []
        for row_number in range(start, min(total, start + config.output.batch_size)):
            rng = row_rngs[row_number]
            level = levels[row_number]
            parent: dict[str, Any] | None
            initial_row: dict[str, Any] = {}
            initial_parents: dict[str, dict[str, Any] | None]
            if level == 0:
                parent = None
                initial_parents = dict.fromkeys(phase.self_fk_columns, None)
                if not phase.roots_point_to_self:
                    for local in _nullable_fk_columns(self_fk):
                        initial_row[local] = None
                else:
                    for local in phase.self_fk_columns:
                        item = compiled.columns[local]
                        if item.generator is None and item.derivation is None:
                            initial_row[local] = None
            else:
                previous = indices_by_level[level - 1]
                parent = rows[previous[rng.randrange(len(previous))]]
                for local, ref in zip(self_fk.columns, self_fk.ref_columns, strict=True):
                    initial_row[local] = parent[ref]
                initial_parents = dict.fromkeys(phase.self_fk_columns, parent)
            assignments = _select_fk_assignments(
                compiled,
                row_number,
                rng,
                selection_states,
                deferred_relation=self_fk_key,
                allow_missing_parents=False,
                unresolved_relations=unresolved_relations,
                initial_values=initial_row,
            )
            row, parents, states = _generate_row(
                compiled,
                row_number,
                rng,
                assignments,
                config,
                initial_row=initial_row,
                initial_parents=initial_parents,
                unresolved_fks=unresolved_relations,
            )
            if level == 0 and phase.roots_point_to_self:
                for local, ref in zip(
                    phase.self_fk_columns,
                    self_fk.ref_columns,
                    strict=True,
                ):
                    if ref not in row:
                        raise GenerationError(
                            f"tabla {compiled.spec.name}, autorreferencia "
                            f"({', '.join(phase.self_fk_columns)}): no se pudo generar "
                            f"la columna referenciada '{ref}' antes de construir la raíz. "
                            "Asigna un generador a la clave primaria y al discriminador."
                        )
                    value = row[ref]
                    if local in row and row[local] is not None and row[local] != value:
                        raise GenerationError(
                            f"tabla {compiled.spec.name}, autorreferencia "
                            f"({', '.join(phase.self_fk_columns)}): la columna compartida "
                            f"'{local}' ya vale {row[local]!r}, pero la raíz necesita "
                            f"{value!r}. Revisa las FKs que fijan ese valor."
                        )
                    row[local] = value
                    parents[local] = row
            batch.append(row)
            rows.append(row)
            indices_by_level.setdefault(level, []).append(row_number)
            dataset._parents.setdefault(compiled.spec.name, {})[id(row)] = parents
            dataset._row_numbers.setdefault(compiled.spec.name, {})[id(row)] = row_number
            dataset._rng_states.setdefault(compiled.spec.name, {})[id(row)] = states
        complete_batch(batch)
        before = len(dataset.tables[compiled.spec.name])
        _accept_batch(compiled, batch, config, dataset, store)
        accepted = dataset.tables[compiled.spec.name][before:]
        # Alinea cada nivel con SU fila aceptada por número de fila: si el lote
        # cuarentena una fila intermedia, los niveles no se desplazan.
        dataset.levels[compiled.spec.name].extend(
            levels[dataset._row_numbers[compiled.spec.name][id(row)]] for row in accepted
        )


def _level_numbers(total: int, branching: int, max_depth: int) -> list[int]:
    levels: list[int] = []
    capacity = 1
    for depth in range(max_depth + 1):
        take = min(capacity, total - len(levels))
        levels.extend([depth] * take)
        if len(levels) == total:
            return levels
        capacity *= branching
    raise PlanError(
        f"jerarquía imposible: branching={branching} y max_depth={max_depth} solo "
        f"alojan {len(levels)} de {total} filas. Aumenta alguno de los dos valores."
    )


def _self_fk(
    table: TableSpec, columns: list[str], by_name: dict[str, TableSpec]
) -> RelationshipSpec:
    return next(
        fk
        for fk in table.foreign_keys
        if fk.columns == columns and by_name.get(fk.ref_table) is table
    )


def _fill_missing_foreign_keys(
    table_name: str,
    columns: tuple[str, ...],
    config: Config,
    dataset: Dataset,
    store: KeyStore,
) -> None:
    compiled = dataset._compiled[table_name]
    table_seed = seed_for_table(config.seed, table_name)
    for fk in compiled.spec.foreign_keys:
        if columns and not set(columns).intersection(fk.columns):
            continue
        parent_rows = dataset.tables[_CURRENT_TABLE_INDEX[fk.ref_table].name]
        for position, row in enumerate(dataset.tables[table_name]):
            relevant_columns = columns or tuple(fk.columns)
            if all(row.get(column) is not None for column in relevant_columns):
                continue
            candidates = [
                parent
                for parent in parent_rows
                if all(
                    local in columns
                    or (not columns and row.get(local) is None)
                    or row.get(local) == parent.get(ref)
                    for local, ref in zip(fk.columns, fk.ref_columns, strict=True)
                )
            ]
            if not candidates:
                nullable_columns = set(_nullable_fk_columns(fk))
                requested_columns = set(columns) if columns else nullable_columns
                if nullable_columns and requested_columns <= nullable_columns:
                    continue
                raise GenerationError(
                    f"tabla {table_name}: no hay fila padre compatible para completar "
                    f"la FK ({', '.join(fk.columns)})."
                )
            row_number = dataset._row_numbers[table_name][id(row)]
            rng = rng_for_row(table_seed, row_number)
            parent = candidates[rng.randrange(len(candidates))]
            changed: dict[str, Any] = {}
            for local, ref in zip(fk.columns, fk.ref_columns, strict=True):
                if not columns or local in columns:
                    row[local] = parent[ref]
                    changed[local] = parent[ref]
                dataset._parents[table_name][id(row)][local] = parent
            if changed:
                dataset.updates.append(DatasetUpdate(table_name, position, changed))
                dataset._update_origins.append(row_number)


def _enforce_referential_integrity(spec: SchemaSpec, dataset: Dataset, store: KeyStore) -> None:
    """Postcondición de `generate_dataset`: ninguna FK no nula queda colgando.

    Durante la generación diferida (`DeferredPhase`) y por niveles
    (`InsertLeveledPhase`) una fila padre puede acabar en cuarentena después de que
    sus hijos ya se aceptaran contra una clave que aún vivía (en el lote, el
    ``KeyStore`` o ``_key_sets``). Aquí, con todas las fases ya ejecutadas, se
    recorre el dataset hasta un punto fijo apartando toda fila aceptada cuya FK no
    nula apunte a una fila que no está aceptada; al cuarentenar un padre, sus
    dependientes caen también, transitivamente. No se inventan ni reasignan valores
    (la reparación es H4): aquí solo se aísla.

    La comparación usa los valores reales de ``RelationshipSpec.ref_columns`` de
    cada FK, nunca `parent.primary_key`: una FK puede referenciar una UNIQUE
    distinta de la PK, o listar la PK compuesta en otro orden (revisión sesión
    E, hallazgo 3).

    ``Dataset.levels`` se mantiene alineado 1:1 con las filas aceptadas y, al
    terminar, el ``KeyStore`` y ``_key_sets`` reflejan únicamente filas aceptadas.
    Para un dataset ya íntegro (el caso normal) es un no-op: cero cuarentena nueva.
    """
    by_name = index_tables(spec)
    changed = True
    while changed:
        changed = False
        accepted = _accepted_ref_value_sets(spec, dataset)
        for table in spec.tables:
            rows = dataset.tables.get(table.name)
            if not rows or not table.foreign_keys:
                continue
            keep: list[bool] = []
            bad: list[ValidationIssue] = []
            for row in rows:
                columns = _dangling_fk_columns(row, table, by_name, accepted)
                keep.append(not columns)
                if columns:
                    bad.append(
                        (
                            row,
                            columns,
                            f"FK ({', '.join(columns)}) apunta a una fila que quedó en "
                            f"cuarentena; se aísla para no dejar una referencia colgante",
                        )
                    )
            if bad:
                changed = True
                _drop_unkept_rows(dataset, table.name, keep)
                dataset.quarantine.setdefault(table.name, []).extend(bad)
    _resync_key_stores(spec, dataset, store)
    _resync_updates(dataset)


def _accepted_ref_value_sets(
    spec: SchemaSpec, dataset: Dataset
) -> dict[tuple[str, tuple[str, ...]], set[tuple[Any, ...]]]:
    """Valores de las filas ACTUALMENTE aceptadas, por (tabla padre, columnas referenciadas).

    Se computa una entrada por cada combinación `(tabla canónica referenciada,
    fk.ref_columns)` que aparece en alguna FK del esquema —incluidas las
    autorreferencias—, nunca asumiendo que `ref_columns` es `table.primary_key`
    (hallazgo 3).
    """
    by_name = index_tables(spec)
    targets = {
        (parent.name, tuple(fk.ref_columns))
        for table in spec.tables
        for fk in table.foreign_keys
        if (parent := by_name.get(fk.ref_table)) is not None
    }
    return {
        (table_name, ref_columns): {
            tuple(row.get(column) for column in ref_columns)
            for row in dataset.tables.get(table_name, [])
        }
        for table_name, ref_columns in targets
    }


def _dangling_fk_columns(
    row: dict[str, Any],
    table: TableSpec,
    by_name: dict[str, TableSpec],
    accepted: dict[tuple[str, tuple[str, ...]], set[tuple[Any, ...]]],
) -> tuple[str, ...]:
    """Columnas de las FK no nulas de `row` cuyo padre no figura entre los aceptados.

    Refleja la misma semántica que ``validation.structural._foreign_key_errors``:
    una FK con algún NULL se salta (su nulabilidad ya se validó) y la clave del
    hijo se compara contra los valores de ``fk.ref_columns`` de las filas padre
    aceptadas, en su orden exacto (hallazgo 3).
    """
    dangling: list[str] = []
    for fk in table.foreign_keys:
        values = tuple(row.get(column) for column in fk.columns)
        if any(value is None for value in values):
            continue
        parent = by_name.get(fk.ref_table)
        if parent is None:
            continue
        parent_values = accepted.get((parent.name, tuple(fk.ref_columns)))
        if parent_values is None:
            continue
        if values not in parent_values:
            dangling.extend(fk.columns)
    return tuple(dict.fromkeys(dangling))


def _drop_unkept_rows(dataset: Dataset, table_name: str, keep: list[bool]) -> None:
    """Filtra filas y, si la tabla es por niveles, sus niveles con la misma máscara."""
    rows = dataset.tables[table_name]
    dataset.tables[table_name] = [row for row, ok in zip(rows, keep, strict=True) if ok]
    levels = dataset.levels.get(table_name)
    if levels is not None:
        dataset.levels[table_name] = [level for level, ok in zip(levels, keep, strict=True) if ok]


def _resync_key_stores(spec: SchemaSpec, dataset: Dataset, store: KeyStore) -> None:
    """Deja `KeyStore` y ``_key_sets`` reflejando únicamente filas aceptadas."""
    for table in spec.tables:
        if not table.primary_key:
            continue
        keys = [
            tuple(row[column] for column in table.primary_key)
            for row in dataset.tables.get(table.name, [])
        ]
        dataset._key_sets[table.name] = set(keys)
        store.replace(table.name, keys)


def _resync_updates(dataset: Dataset) -> None:
    """Realinea `Dataset.updates` con las posiciones tras el cierre referencial.

    `_fill_missing_foreign_keys` registra `row_index` como la posición de la
    fila en `dataset.tables[table]` EN ESE MOMENTO. Si `_enforce_referential_integrity`
    cuarentena filas después, esas posiciones quedan desplazadas o pasan a
    apuntar a otra fila (hallazgo 4). Se descartan las actualizaciones cuya
    fila acabó en cuarentena y se recalcula `row_index` de las supervivientes
    contra `dataset._row_numbers` (el número de fila original, estable, que
    nunca se reasigna aunque la lista se filtre y desplace).
    """
    kept_updates: list[DatasetUpdate] = []
    kept_origins: list[int] = []
    position_by_row_number: dict[str, dict[int, int]] = {}
    for update, row_number in zip(dataset.updates, dataset._update_origins, strict=True):
        by_number = position_by_row_number.get(update.table)
        if by_number is None:
            row_numbers = dataset._row_numbers.get(update.table, {})
            by_number = {
                row_numbers[id(row)]: index
                for index, row in enumerate(dataset.tables.get(update.table, []))
            }
            position_by_row_number[update.table] = by_number
        new_index = by_number.get(row_number)
        if new_index is None:
            continue  # la fila de esta actualización quedó en cuarentena
        kept_updates.append(
            update if new_index == update.row_index else replace(update, row_index=new_index)
        )
        kept_origins.append(row_number)
    dataset.updates = kept_updates
    dataset._update_origins = kept_origins


def _install_run_context(spec: SchemaSpec, dataset: Dataset) -> None:
    global _CURRENT_DATASET, _CURRENT_SPEC, _CURRENT_TABLE_INDEX
    global _CURRENT_UNIQUE, _CURRENT_COMPOSITE
    _CURRENT_DATASET = dataset
    _CURRENT_SPEC = spec
    _CURRENT_TABLE_INDEX = index_tables(spec)
    _CURRENT_UNIQUE = {}
    _CURRENT_COMPOSITE = {}
    _SELECTION_WORK.reset()
