"""Per-batch structural validation for generated rows (T2.13)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeAlias

from synthdb.generation.context import RowContext, mapping_resolver
from synthdb.generation.numeric_bounds import effective_scale, fits, representable_limit
from synthdb.generation.seeding import rng_for_row, seed_for_table
from synthdb.graph.dependency import index_tables
from synthdb.ir.plans import TablePlan
from synthdb.ir.schema import CheckSpec, ColumnSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.rules.dsl import as_bound, as_derivation
from synthdb.rules.eval import RuleEvalError, check

if TYPE_CHECKING:
    from synthdb.generation.engine import Dataset

ValidationIssue: TypeAlias = tuple[dict[str, Any], tuple[str, ...], str]
"""Rejected row, implicated columns and a readable reason."""


def validate_batch(
    rows: list[dict[str, Any]],
    table_plan: TablePlan,
    spec: SchemaSpec,
    dataset: Dataset,
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """Validate a generated batch against the IR and all compiled YAML rules.

    Uniqueness sets live in ``dataset`` so constraints span batch boundaries.
    Derivations are checked with the RNG state captured immediately before the
    derived column was generated; equality is exact by policy.
    """
    table = next(table for table in spec.tables if table.name == table_plan.table)
    compiled = dataset._compiled.get(table.name)
    unique_sets = dataset._validation_unique.setdefault(table.name, {})
    self_parent_values = _self_referenced_values(table, rows, dataset)
    ok: list[dict[str, Any]] = []
    bad: list[ValidationIssue] = []

    for row in rows:
        issues: list[tuple[tuple[str, ...], str]] = []
        for column in table.columns:
            value = row.get(column.name)
            if value is None:
                if not column.nullable and not column.generated:
                    issues.append(((column.name,), "valor NULL en columna NOT NULL"))
                continue
            reason = _type_error(value, column.type)
            if reason is not None:
                issues.append(((column.name,), reason))
                continue
            reason = _type_constraint_error(value, column)
            if reason is not None:
                issues.append(((column.name,), reason))
            if column.type.kind in {"varchar", "char"} and column.type.length is not None:
                values = value if column.type.is_array else [value]
                if any(len(item) > column.type.length for item in values):
                    issues.append(
                        (
                            (column.name,),
                            f"longitud mayor que {column.type.length} para {column.type.kind}",
                        )
                    )
            for constraint in column.checks:
                reason = _bounds_error(value, column, constraint)
                if reason is not None:
                    issues.append(((column.name,), reason))

        for constraint in table.checks:
            if len(constraint.columns_involved) == 1:
                column_name = constraint.columns_involved[0]
                column = next(c for c in table.columns if c.name == column_name)
                value = row.get(column_name)
                if value is not None:
                    reason = _bounds_error(value, column, constraint)
                    if reason is not None:
                        issues.append(((column_name,), reason))

        issues.extend(_unique_errors(row, table, unique_sets))
        issues.extend(_foreign_key_errors(row, table, spec, dataset, self_parent_values))

        if compiled is not None:
            parents = dataset._parents.get(table.name, {}).get(id(row), {})
            row_number = dataset._row_numbers.get(table.name, {}).get(id(row), 0)
            for rule in compiled.rules:
                rng = rng_for_row(
                    seed_for_table(dataset._config.seed if dataset._config else 0, table.name),
                    row_number,
                )
                derivation = as_derivation(rule)
                bound = as_bound(rule)
                target_column = (
                    derivation.column
                    if derivation is not None
                    else bound.column
                    if bound is not None
                    else None
                )
                if target_column is not None:
                    state = (
                        dataset._rng_states.get(table.name, {}).get(id(row), {}).get(target_column)
                    )
                    if state is not None:
                        rng.setstate(state)
                ctx = RowContext(
                    rng=rng,
                    column=table.columns[0],
                    table=table.name,
                    row=row,
                    refs=dataset._config.refs if dataset._config else {},
                    resolve_parent=mapping_resolver(parents),
                )
                try:
                    valid = check(rule, ctx)
                except RuleEvalError as exc:
                    issues.append((_rule_columns(rule, table), f"regla {rule.text!r}: {exc}"))
                else:
                    if not valid:
                        issues.append(
                            (_rule_columns(rule, table), f"regla incumplida: {rule.text}")
                        )

        if issues:
            columns = tuple(dict.fromkeys(column for group, _ in issues for column in group))
            reason = "; ".join(message for _, message in issues)
            bad.append((row, columns, reason))
        else:
            ok.append(row)
            _remember_unique(row, table, unique_sets)
    return ok, bad


def _type_error(value: Any, type_spec: TypeSpec) -> str | None:
    values = value
    if type_spec.is_array:
        if not isinstance(value, list):
            return f"se esperaba una lista para el tipo array; se recibió {type(value).__name__}"
        values = value
    else:
        values = [value]
    for element in values:
        if not _scalar_matches(element, type_spec):
            return (
                f"tipo inválido: se esperaba {type_spec.kind}"
                f"{'[]' if type_spec.is_array else ''}; se recibió {type(element).__name__}"
            )
    return None


def _scalar_matches(value: Any, type_spec: TypeSpec) -> bool:
    kind = type_spec.kind
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "numeric":
        return isinstance(value, int | float | Decimal) and not isinstance(value, bool)
    if kind in {"text", "varchar", "char", "enum"}:
        return isinstance(value, str)
    if kind == "date":
        return isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    if kind == "timestamp":
        return isinstance(value, dt.datetime)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "uuid":
        return isinstance(value, uuid.UUID)
    if kind == "json":
        return value is None or isinstance(value, str | int | float | bool | list | dict)
    if kind == "bytea":
        return isinstance(value, bytes | bytearray)
    return False


def _type_constraint_error(value: Any, column: ColumnSpec) -> str | None:
    """Validate width, enum domain and numeric precision encoded by TypeSpec."""
    values = value if column.type.is_array else [value]
    for element in values:
        if column.type.kind == "integer" and column.type.bits is not None:
            low = -(2 ** (column.type.bits - 1))
            high = 2 ** (column.type.bits - 1) - 1
            if not low <= element <= high:
                return f"entero fuera del rango de {column.type.bits} bits"
        if (
            column.type.kind == "enum"
            and column.enum_values is not None
            and element not in column.enum_values
        ):
            return f"valor {element!r} fuera del enum {column.enum_values!r}"
        if (
            column.type.kind == "numeric"
            and column.type.precision is not None
            and not fits(element, column.type.precision, column.type.scale)
        ):
            scale = effective_scale(column.type.scale)
            limit = representable_limit(column.type.precision, column.type.scale)
            return (
                f"valor {element!r} no representable en NUMERIC({column.type.precision}, "
                f"{scale}): el máximo representable es ±{limit}"
            )
        if (
            column.type.kind == "timestamp"
            and column.type.with_timezone is True
            and element.tzinfo is None
        ):
            return "timestamp with time zone sin tzinfo"
    return None


def _bounds_error(value: Any, column: ColumnSpec, constraint: CheckSpec) -> str | None:
    bounds = constraint.bounds_derived
    if not constraint.ast_supported or not bounds:
        return None
    values = value if column.type.is_array else [value]
    for element in values:
        if "equals" in bounds and element != bounds["equals"]:
            return f"CHECK ({constraint.sql_text}) exige {bounds['equals']!r}"
        if "values" in bounds and element not in bounds["values"]:
            return f"CHECK ({constraint.sql_text}) admite solo {bounds['values']!r}"
        if element in bounds.get("excluded_values", []):
            return f"CHECK ({constraint.sql_text}) excluye {element!r}"
        low = bounds.get("min")
        if low is not None and (element < low or (bounds.get("min_exclusive") and element == low)):
            return f"CHECK ({constraint.sql_text}) incumple la cota mínima {low!r}"
        high = bounds.get("max")
        if high is not None and (
            element > high or (bounds.get("max_exclusive") and element == high)
        ):
            return f"CHECK ({constraint.sql_text}) incumple la cota máxima {high!r}"
    return None


def _unique_errors(
    row: dict[str, Any],
    table: TableSpec,
    unique_sets: dict[tuple[str, ...], set[tuple[Any, ...]]],
) -> list[tuple[tuple[str, ...], str]]:
    errors: list[tuple[tuple[str, ...], str]] = []
    groups = ([table.primary_key] if table.primary_key else []) + table.uniques
    for raw_group in groups:
        group = tuple(raw_group)
        key = tuple(_hashable(row.get(column)) for column in group)
        if any(value is None for value in key) and list(raw_group) != table.primary_key:
            continue
        if key in unique_sets.setdefault(group, set()):
            errors.append((group, f"valor duplicado para UNIQUE/PRIMARY KEY {group}: {key!r}"))
    return errors


def _remember_unique(
    row: dict[str, Any],
    table: TableSpec,
    unique_sets: dict[tuple[str, ...], set[tuple[Any, ...]]],
) -> None:
    groups = ([table.primary_key] if table.primary_key else []) + table.uniques
    for raw_group in groups:
        group = tuple(raw_group)
        key = tuple(_hashable(row.get(column)) for column in group)
        if any(value is None for value in key) and list(raw_group) != table.primary_key:
            continue
        unique_sets.setdefault(group, set()).add(key)


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    return value


def _foreign_key_errors(
    row: dict[str, Any],
    table: TableSpec,
    spec: SchemaSpec,
    dataset: Dataset,
    self_parent_values: dict[tuple[str, ...], set[tuple[Any, ...]]],
) -> list[tuple[tuple[str, ...], str]]:
    """Comprueba cada FK contra los valores REALES de `fk.ref_columns` del padre.

    `RelationshipSpec.ref_columns` puede apuntar a cualquier PK o UNIQUE del
    padre, no necesariamente a su `primary_key` (revisión sesión E, hallazgo
    3): comparar contra `parent.primary_key` rechazaría en falso toda FK que
    referencie una UNIQUE distinta, o una PK compuesta listada en otro orden.
    """
    errors: list[tuple[tuple[str, ...], str]] = []
    by_name = index_tables(spec)
    for fk in table.foreign_keys:
        values = tuple(row.get(column) for column in fk.columns)
        nulls = sum(value is None for value in values)
        if nulls:
            if fk.match_full and nulls != len(values):
                errors.append((tuple(fk.columns), "FK MATCH FULL contiene NULL parcial"))
            continue
        parent = by_name.get(fk.ref_table)
        if parent is None:
            errors.append((tuple(fk.columns), f"la tabla padre {fk.ref_table!r} no existe"))
            continue
        ref_columns = tuple(fk.ref_columns)
        parent_values = (
            self_parent_values.get(ref_columns, set())
            if parent.name == table.name
            else _accepted_ref_values(dataset, parent.name, ref_columns)
        )
        if values not in parent_values:
            errors.append(
                (
                    tuple(fk.columns),
                    f"FK {values!r} no existe entre los valores aceptados de "
                    f"{parent.name}{list(ref_columns)!r}",
                )
            )
    return errors


def _self_referenced_values(
    table: TableSpec, rows: list[dict[str, Any]], dataset: Dataset
) -> dict[tuple[str, ...], set[tuple[Any, ...]]]:
    """Valores de cada `ref_columns` autorreferenciado vistos hasta ahora en `table`.

    Nunca se cachea a largo plazo (a diferencia de `_accepted_ref_values`):
    una autorreferencia se valida lote a lote dentro de la MISMA tabla
    (`InsertLeveledPhase`), así que `rows` —el lote que se está validando
    ahora, todavía sin aceptar— debe sumarse en cada llamada a lo ya aceptado
    en `dataset.tables[table.name]`.
    """
    result: dict[tuple[str, ...], set[tuple[Any, ...]]] = {}
    for fk in table.foreign_keys:
        if fk.ref_table != table.name:
            continue
        ref_columns = tuple(fk.ref_columns)
        if ref_columns in result:
            continue
        values = {
            tuple(existing.get(column) for column in ref_columns)
            for existing in dataset.tables.get(table.name, [])
        }
        values.update(tuple(row.get(column) for column in ref_columns) for row in rows)
        result[ref_columns] = values
    return result


def _accepted_ref_values(
    dataset: Dataset, table_name: str, ref_columns: tuple[str, ...]
) -> set[tuple[Any, ...]]:
    """Valores de `ref_columns` de las filas ya aceptadas de `table_name`, cacheados.

    Cuando `table_name` no es la propia tabla que se valida, el orden de fases
    garantiza que ya terminó de generarse (padre antes que hijo; dentro de una
    `DeferredPhase`, todas las tablas de la fase están completas antes de que
    arranque la validación de cualquiera de ellas), así que el conjunto se
    construye una única vez por combinación (tabla, columnas referenciadas) y
    se reutiliza para el resto de lotes del hijo.
    """
    key = (table_name, ref_columns)
    cached = dataset._ref_value_sets.get(key)
    if cached is None:
        cached = {
            tuple(row.get(column) for column in ref_columns)
            for row in dataset.tables.get(table_name, [])
        }
        dataset._ref_value_sets[key] = cached
    return cached


def _rule_columns(rule: Any, table: TableSpec) -> tuple[str, ...]:
    derivation = as_derivation(rule)
    if derivation is not None:
        return (derivation.column,)
    involved = tuple(column for column in (c.name for c in table.columns) if column in rule.text)
    return involved or ("<regla>",)
