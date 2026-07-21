"""Deterministic in-memory generation engine (T2.11 and T2.12).

The engine compiles every table before producing a row, then executes the
structural phases with one RNG per row.  ``Dataset`` is deliberately the MVP
store: it keeps generated parent rows, applied deferred updates and quarantine
records in memory so later emitters can choose their physical representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from random import Random
from typing import Any, TypeAlias

from synthdb.config.models import Config
from synthdb.constraints.check_interp import interpret_checks
from synthdb.generation.context import RowContext, build_column_order, mapping_resolver
from synthdb.generation.fk import (
    NullRatioSelector,
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
    assignments, rng_states = _prepare_fk_assignments(
        compiled,
        total,
        config,
        dataset,
        store,
        deferred_relation=deferred_relation,
        deferred_null_columns=deferred_null_columns,
    )
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    batch_size = config.output.batch_size
    for start in range(0, total, batch_size):
        batch: list[dict[str, Any]] = []
        for row_number in range(start, min(total, start + batch_size)):
            rng = rng_for_row(table_seed, row_number)
            rng.setstate(rng_states[row_number])
            row, parents, states = _generate_row(
                compiled, row_number, rng, assignments[row_number], config
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
) -> tuple[list[dict[FkKey, int | None]], list[RandomState]]:
    assignments: list[dict[FkKey, int | None]] = [{} for _ in range(total)]
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    row_rngs = [rng_for_row(table_seed, index) for index in range(total)]
    selectors: dict[FkKey, Any] = {}
    quotas: dict[FkKey, list[int]] = {}

    for fk in compiled.spec.foreign_keys:
        key = tuple(fk.columns)
        if key == deferred_relation:
            continue
        parent_name = _CURRENT_TABLE_INDEX[fk.ref_table].name
        parent_count = store.count(parent_name)
        if parent_count == 0:
            continue
        params = _fk_params(compiled, fk, config)
        strategy = str(params.get("strategy", "uniform"))
        null_ratio = float(params.get("null_ratio", 0.0))
        if strategy == "quota":
            non_null = [i for i, rng in enumerate(row_rngs) if rng.random() >= null_ratio]
            quota_rng = Random(seed_for_table(config.seed, f"{compiled.spec.name}:{','.join(key)}"))
            quota = build_quota_assignment(
                quota_rng,
                parent_count,
                len(non_null),
                int(params["min"]),
                int(params["max"]),
            )
            quotas[key] = [-1] * total
            for row_index, parent_index in zip(non_null, quota, strict=True):
                quotas[key][row_index] = parent_index
            continue
        selector: Any
        if strategy == "zipf":
            selector = ZipfSelector(parent_count, float(params.get("s", 1.2)))
        elif strategy == "unique_subset":
            selector = UniqueSubsetSelector(parent_count, total, compiled.spec.name)
        else:
            selector = UniformSelector(parent_count)
        if null_ratio:
            selector = NullRatioSelector(selector, null_ratio)
        selectors[key] = selector

    seen_bridge_pairs: set[tuple[Any, ...]] = set()
    for row_index, rng in enumerate(row_rngs):
        for fk in compiled.spec.foreign_keys:
            key = tuple(fk.columns)
            if key == deferred_relation:
                assignments[row_index][key] = None
                continue
            if key in quotas:
                selected = quotas[key][row_index]
                assignments[row_index][key] = None if selected < 0 else selected
            elif key in selectors:
                assignments[row_index][key] = selectors[key].pick(rng)
        if compiled.spec.kind == "bridge":
            _deduplicate_bridge_pair(
                compiled.spec, assignments[row_index], rng, store, seen_bridge_pairs
            )
    return assignments, [rng.getstate() for rng in row_rngs]


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


def _deduplicate_bridge_pair(
    table: TableSpec,
    assignment: dict[FkKey, int | None],
    rng: Random,
    store: KeyStore,
    seen: set[tuple[Any, ...]],
) -> None:
    if len(table.foreign_keys) < 2:
        return
    first, second = table.foreign_keys[:2]
    first_key, second_key = tuple(first.columns), tuple(second.columns)
    left, right = assignment.get(first_key), assignment.get(second_key)
    if left is None or right is None:
        return
    pair = (left, right)
    if pair not in seen:
        seen.add(pair)
        return
    parent_name = _CURRENT_TABLE_INDEX[second.ref_table].name
    candidates = [index for index in range(store.count(parent_name)) if (left, index) not in seen]
    if not candidates:
        raise GenerationError(
            f"tabla puente {table.name}: no quedan pares FK únicos para las "
            "filas solicitadas; reduce la cardinalidad o aumenta las tablas padre."
        )
    right = candidates[rng.randrange(len(candidates))]
    assignment[second_key] = right
    seen.add((left, right))


def _generate_row(
    compiled: _CompiledTable,
    row_number: int,
    rng: Random,
    assignments: dict[FkKey, int | None],
    config: Config,
) -> tuple[dict[str, Any], dict[str, dict[str, Any] | None], dict[str, RandomState]]:
    row: dict[str, Any] = {}
    parents: dict[str, dict[str, Any] | None] = {}
    states: dict[str, RandomState] = {}

    # The database would assign SERIAL values. The in-memory MVP mirrors the
    # deterministic values PostgreSQL would produce so they can enter KeyStore.
    for column in compiled.spec.columns:
        if column.type.autoincrement:
            row[column.name] = row_number + 1

    dataset = _CURRENT_DATASET
    if dataset is None:
        raise AssertionError("dataset context not installed")
    for fk in compiled.spec.foreign_keys:
        key = tuple(fk.columns)
        parent_index = assignments.get(key)
        if parent_index is None:
            for local_column in fk.columns:
                row.setdefault(local_column, None)
                parents[local_column] = None
            continue
        parent_table = _CURRENT_TABLE_INDEX[fk.ref_table].name
        parent_row = dataset.tables[parent_table][parent_index]
        for local_column, ref_column in zip(fk.columns, fk.ref_columns, strict=True):
            row[local_column] = parent_row[ref_column]
            parents[local_column] = parent_row

    for column_name in compiled.order:
        if column_name in row:
            continue
        item = compiled.columns[column_name]
        if item.spec is None:
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
    hierarchy_key = f"{compiled.spec.name}.{phase.self_fk_columns[0]}"
    hierarchy = config.hierarchy.get(hierarchy_key)
    branching = hierarchy.branching if hierarchy is not None else 5
    max_depth = hierarchy.max_depth if hierarchy is not None else max(total, 1)
    levels = _level_numbers(total, branching, max_depth)
    dataset.levels[compiled.spec.name] = []
    table_seed = seed_for_table(config.seed, compiled.spec.name)
    rows: list[dict[str, Any]] = []
    indices_by_level: dict[int, list[int]] = {}
    for start in range(0, total, config.output.batch_size):
        batch: list[dict[str, Any]] = []
        for row_number in range(start, min(total, start + config.output.batch_size)):
            rng = rng_for_row(table_seed, row_number)
            row: dict[str, Any] = {}
            for column in compiled.spec.columns:
                if column.type.autoincrement:
                    row[column.name] = row_number + 1
            level = levels[row_number]
            parent: dict[str, Any] | None
            if level == 0:
                if phase.roots_point_to_self:
                    for local, ref in zip(
                        phase.self_fk_columns,
                        _self_fk(compiled.spec, phase.self_fk_columns).ref_columns,
                        strict=True,
                    ):
                        row[local] = row[ref]
                else:
                    for local in phase.self_fk_columns:
                        row[local] = None
                parent = row if phase.roots_point_to_self else None
            else:
                previous = indices_by_level[level - 1]
                parent = rows[previous[rng.randrange(len(previous))]]
                fk = _self_fk(compiled.spec, phase.self_fk_columns)
                for local, ref in zip(fk.columns, fk.ref_columns, strict=True):
                    row[local] = parent[ref]
            parents: dict[str, dict[str, Any] | None] = dict.fromkeys(phase.self_fk_columns, parent)
            states: dict[str, RandomState] = {}
            for column_name in compiled.order:
                if column_name in row:
                    continue
                item = compiled.columns[column_name]
                if item.spec is None:
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
                row[column_name] = _generate_value(item, ctx)
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


def _self_fk(table: TableSpec, columns: list[str]) -> RelationshipSpec:
    return next(
        fk for fk in table.foreign_keys if fk.columns == columns and fk.ref_table == table.name
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

    Se computa una entrada por cada combinación `(fk.ref_table, fk.ref_columns)`
    que aparece en alguna FK del esquema —incluidas las autorreferencias—, nunca
    asumiendo que `ref_columns` es `table.primary_key` (hallazgo 3).
    """
    targets = {
        (fk.ref_table, tuple(fk.ref_columns)) for table in spec.tables for fk in table.foreign_keys
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
