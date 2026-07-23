"""Invariantes comunes entre generadores semánticos y la IR estructural.

La propuesta LLM y el artefacto resuelto viven en fronteras distintas, pero
ambos deben rechazar decisiones que nunca podrían producir un valor válido para
la columna descrita por ``SchemaSpec``. Este módulo concentra esa comprobación
para evitar que las dos fronteras diverjan.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, TypeAlias

from synthdb.generation.numeric_bounds import fits, has_quantized_value
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec

TableIdentity: TypeAlias = tuple[str | None, str]

_TEXT_KINDS = frozenset({"text", "varchar", "char"})
_GENERATOR_KINDS: dict[str, frozenset[str]] = {
    "faker": _TEXT_KINDS,
    "template": _TEXT_KINDS,
    "numeric_range": frozenset({"integer", "numeric"}),
    "sequence": frozenset({"integer"}),
    "datetime_range": frozenset({"date", "timestamp"}),
    "uuid": frozenset({"uuid"}),
}


def table_identity(table: TableSpec) -> TableIdentity:
    """Devuelve la identidad contractual inequívoca de una tabla."""
    return table.schema_, table.name


def identity_text(identity: TableIdentity) -> str:
    """Representación legible de una identidad de tabla."""
    schema_name, table_name = identity
    return f"{schema_name}.{table_name}" if schema_name else table_name


def index_tables(schema: SchemaSpec) -> dict[TableIdentity, TableSpec]:
    """Indexa tablas sin colisionar nombres iguales de namespaces distintos."""
    return {table_identity(table): table for table in schema.tables}


def resolve_referenced_table(
    schema: SchemaSpec,
    owner: TableSpec,
    reference: str,
) -> TableSpec | None:
    """Resuelve el nombre de una FK respetando namespaces y ambigüedad."""
    if "." in reference:
        schema_name, table_name = reference.split(".", 1)
        return index_tables(schema).get((schema_name, table_name))

    same_namespace = [
        table
        for table in schema.tables
        if table.schema_ == owner.schema_ and table.name == reference
    ]
    if len(same_namespace) == 1:
        return same_namespace[0]
    matches = [table for table in schema.tables if table.name == reference]
    return matches[0] if len(matches) == 1 else None


def validate_generator_compatibility(
    *,
    schema: SchemaSpec,
    table: TableSpec,
    column: ColumnSpec,
    generator_type: str | None,
    params: Mapping[str, Any] | None = None,
    null_ratio: float = 0.0,
    unique: bool = False,
    enforce_unique: bool = True,
    context: str | None = None,
) -> None:
    """Rechaza una decisión que contradiga tipo, dominio o restricciones IR.

    La función no reescribe ni recorta el plan. Un artefacto resuelto debe
    contener una decisión válida tal cual; una propuesta incompatible tampoco
    debe avanzar hasta el fusor.
    """
    ctx = context or f"{identity_text(table_identity(table))}.{column.name}"
    managed = column.type.autoincrement or column.generated
    fk_relationships = [
        relationship for relationship in table.foreign_keys if column.name in relationship.columns
    ]
    fk_column = bool(fk_relationships)
    raw_params = params or {}
    strategy_null_ratio = raw_params.get("null_ratio")
    effective_null_ratio = max(
        null_ratio,
        float(strategy_null_ratio) if strategy_null_ratio is not None else 0.0,
    )

    if generator_type is None:
        if not managed:
            raise ValueError(
                f"{ctx}: generator=None solo es válido para una columna "
                "autoincremental o GENERATED."
            )
        return
    if managed:
        raise ValueError(f"{ctx}: una columna autoincremental o GENERATED no admite generador.")
    if column.type.is_array:
        raise ValueError(f"{ctx}: el catálogo H3-R1 no genera valores de array.")
    if effective_null_ratio > 0.0 and not column.nullable:
        raise ValueError(
            f"{ctx}: null_ratio={effective_null_ratio} contradice la restricción NOT NULL."
        )

    if fk_column:
        if generator_type != "fk":
            raise ValueError(
                f"{ctx}: una columna FK exige el generador estructural 'fk', no {generator_type!r}."
            )
        if len(fk_relationships) != 1:
            raise ValueError(
                f"{ctx}: la columna participa en varias FKs y el contrato v1 "
                "no puede identificar una de forma inequívoca."
            )
        relationship = fk_relationships[0]
        parent = resolve_referenced_table(schema, table, relationship.ref_table)
        if parent is None:
            raise ValueError(
                f"{ctx}: la FK referencia una tabla inexistente o ambigua "
                f"{relationship.ref_table!r}."
            )
        position = relationship.columns.index(column.name)
        parent_columns = relationship.ref_columns or parent.primary_key
        if position >= len(parent_columns) or parent_columns[position] not in {
            parent_column.name for parent_column in parent.columns
        }:
            raise ValueError(
                f"{ctx}: la FK no resuelve una columna padre real en "
                f"{identity_text(table_identity(parent))}."
            )
        return
    if generator_type == "fk":
        raise ValueError(f"{ctx}: el generador 'fk' exige una FK existente en la IR.")

    if enforce_unique and _is_single_column_unique(table, column) and not unique:
        raise ValueError(
            f"{ctx}: la IR declara la columna UNIQUE/PK y el plan debe fijar unique=true."
        )

    if generator_type == "choice":
        _validate_choice(ctx, table, column, raw_params)
        return
    if generator_type == "fallback":
        _validate_fallback(ctx, column)
        return
    if generator_type == "derived":
        raise ValueError(
            f"{ctx}: 'derived' no es autocontenido en ResolvedPlanArtifact v1; "
            "su regla y refs viven fuera del artefacto."
        )

    allowed = _GENERATOR_KINDS.get(generator_type)
    if allowed is None or column.type.kind not in allowed:
        raise ValueError(
            f"{ctx}: el generador {generator_type!r} es incompatible con el "
            f"tipo IR {column.type.kind!r}."
        )
    if generator_type in {"faker", "template"}:
        _validate_unbounded_text_generator(ctx, column)
    elif generator_type == "numeric_range":
        _validate_numeric_range(ctx, table, column, raw_params)
    elif generator_type == "datetime_range":
        _validate_datetime_range(ctx, table, column, raw_params)


def _is_single_column_unique(table: TableSpec, column: ColumnSpec) -> bool:
    return table.primary_key == [column.name] or any(
        unique_columns == [column.name] for unique_columns in table.uniques
    )


def _validate_unbounded_text_generator(ctx: str, column: ColumnSpec) -> None:
    if column.type.kind in {"varchar", "char"} and column.type.length is not None:
        raise ValueError(
            f"{ctx}: el generador no garantiza la longitud máxima "
            f"{column.type.length} de {column.type.kind}."
        )


def _validate_fallback(ctx: str, column: ColumnSpec) -> None:
    if column.type.kind == "enum" and not column.enum_values:
        raise ValueError(f"{ctx}: un enum sin enum_values no tiene fallback seguro.")


def _validate_choice(
    ctx: str,
    table: TableSpec,
    column: ColumnSpec,
    params: Mapping[str, Any],
) -> None:
    values = params.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError(f"{ctx}: choice exige una lista no vacía de valores.")
    if not all(_value_matches_type(value, column) for value in values):
        raise ValueError(
            f"{ctx}: choice contiene valores incompatibles con el tipo {column.type.kind!r}."
        )

    domain = _closed_domain(table, column)
    if domain is not None:
        invalid = [value for value in values if value not in domain]
        if invalid:
            raise ValueError(
                f"{ctx}: choice contiene valores {invalid!r} fuera del dominio "
                f"cerrado {domain!r} de la IR."
            )
    invalid_bounds = [
        value for value in values if not _value_satisfies_checks(value, table, column)
    ]
    if invalid_bounds:
        raise ValueError(
            f"{ctx}: choice contiene valores {invalid_bounds!r} que violan constraints de la IR."
        )


def _value_matches_type(value: Any, column: ColumnSpec) -> bool:
    kind = column.type.kind
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        bits = column.type.bits or 32
        return bool(-(2 ** (bits - 1)) <= value <= 2 ** (bits - 1) - 1)
    if kind == "numeric":
        if not isinstance(value, int | float) or isinstance(value, bool):
            return False
        precision = column.type.precision
        return precision is None or fits(value, precision, column.type.scale)
    if kind in _TEXT_KINDS | {"enum"}:
        if not isinstance(value, str):
            return False
        length = column.type.length
        return length is None or len(value) <= length
    return False


def _checks(table: TableSpec, column: ColumnSpec) -> list[Mapping[str, Any]]:
    checks: list[Mapping[str, Any]] = [
        check.bounds_derived for check in column.checks if check.bounds_derived is not None
    ]
    checks.extend(
        check.bounds_derived
        for check in table.checks
        if check.bounds_derived is not None and column.name in check.columns_involved
    )
    return checks


def _closed_domain(table: TableSpec, column: ColumnSpec) -> list[Any] | None:
    domains: list[list[Any]] = []
    if column.enum_values is not None:
        domains.append(list(column.enum_values))
    for bounds in _checks(table, column):
        if "equals" in bounds:
            domains.append([bounds["equals"]])
        if "values" in bounds:
            domains.append(list(bounds["values"]))
    if not domains:
        return None
    first = domains[0]
    return [value for value in first if all(value in domain for domain in domains[1:])]


def _value_satisfies_checks(value: Any, table: TableSpec, column: ColumnSpec) -> bool:
    for bounds in _checks(table, column):
        if value in bounds.get("excluded_values", []):
            return False
        if "equals" in bounds and value != bounds["equals"]:
            return False
        if "values" in bounds and value not in bounds["values"]:
            return False
        try:
            if "min" in bounds and (
                value < bounds["min"]
                or (value == bounds["min"] and bounds.get("min_exclusive", False))
            ):
                return False
            if "max" in bounds and (
                value > bounds["max"]
                or (value == bounds["max"] and bounds.get("max_exclusive", False))
            ):
                return False
        except TypeError:
            return False
    return True


def _validate_numeric_range(
    ctx: str,
    table: TableSpec,
    column: ColumnSpec,
    params: Mapping[str, Any],
) -> None:
    low = params.get("min")
    high = params.get("max")
    min_exclusive = bool(params.get("min_exclusive", False))
    max_exclusive = bool(params.get("max_exclusive", False))
    if (
        low is not None
        and high is not None
        and (low > high or (low == high and (min_exclusive or max_exclusive)))
    ):
        raise ValueError(f"{ctx}: numeric_range describe un rango vacío.")

    domain = _closed_domain(table, column)
    excluded = [
        value for bounds in _checks(table, column) for value in bounds.get("excluded_values", [])
    ]
    if domain is not None or excluded:
        raise ValueError(
            f"{ctx}: numeric_range no puede garantizar un dominio cerrado o con "
            "valores excluidos; usa choice."
        )

    constraint_low, constraint_low_exclusive = _tightest_bound(table, column, name="min")
    constraint_high, constraint_high_exclusive = _tightest_bound(table, column, name="max")
    if (
        low is not None
        and constraint_low is not None
        and (
            low < constraint_low
            or (low == constraint_low and constraint_low_exclusive and not min_exclusive)
        )
    ):
        raise ValueError(f"{ctx}: el mínimo amplía las cotas de la IR.")
    if (
        high is not None
        and constraint_high is not None
        and (
            high > constraint_high
            or (high == constraint_high and constraint_high_exclusive and not max_exclusive)
        )
    ):
        raise ValueError(f"{ctx}: el máximo amplía las cotas de la IR.")

    effective_low, effective_low_exclusive = _combine_lower(
        low, min_exclusive, constraint_low, constraint_low_exclusive
    )
    effective_high, effective_high_exclusive = _combine_upper(
        high, max_exclusive, constraint_high, constraint_high_exclusive
    )
    if column.type.kind == "integer":
        if not _integer_interval_has_value(
            effective_low,
            effective_high,
            effective_low_exclusive,
            effective_high_exclusive,
            column.type.bits,
        ):
            raise ValueError(f"{ctx}: numeric_range no contiene ningún entero válido.")
    elif column.type.precision is not None and not has_quantized_value(
        column.type.precision,
        column.type.scale,
        low=effective_low,
        high=effective_high,
        min_exclusive=effective_low_exclusive,
        max_exclusive=effective_high_exclusive,
    ):
        raise ValueError(f"{ctx}: numeric_range no contiene ningún valor representable por la IR.")


def _tightest_bound(
    table: TableSpec,
    column: ColumnSpec,
    *,
    name: str,
) -> tuple[Any, bool]:
    candidates: list[tuple[Any, bool]] = []
    for bounds in _checks(table, column):
        if "equals" in bounds:
            candidates.append((bounds["equals"], False))
        if name in bounds:
            candidates.append((bounds[name], bool(bounds.get(f"{name}_exclusive", False))))
    if not candidates:
        return None, False
    if name == "min":
        target = max(value for value, _exclusive in candidates)
    else:
        target = min(value for value, _exclusive in candidates)
    return target, any(exclusive for value, exclusive in candidates if value == target)


def _combine_lower(
    first: Any,
    first_exclusive: bool,
    second: Any,
    second_exclusive: bool,
) -> tuple[Any, bool]:
    if first is None:
        return second, second_exclusive
    if second is None:
        return first, first_exclusive
    if first == second:
        return first, first_exclusive or second_exclusive
    return (first, first_exclusive) if first > second else (second, second_exclusive)


def _combine_upper(
    first: Any,
    first_exclusive: bool,
    second: Any,
    second_exclusive: bool,
) -> tuple[Any, bool]:
    if first is None:
        return second, second_exclusive
    if second is None:
        return first, first_exclusive
    if first == second:
        return first, first_exclusive or second_exclusive
    return (first, first_exclusive) if first < second else (second, second_exclusive)


def _integer_interval_has_value(
    low: Any,
    high: Any,
    min_exclusive: bool,
    max_exclusive: bool,
    bits: int | None,
) -> bool:
    bit_count = bits or 32
    type_low = -(2 ** (bit_count - 1))
    type_high = 2 ** (bit_count - 1) - 1
    low_decimal = Decimal(str(low)) if low is not None else Decimal(type_low)
    high_decimal = Decimal(str(high)) if high is not None else Decimal(type_high)
    first = int(low_decimal.to_integral_value(rounding=ROUND_CEILING))
    if min_exclusive and Decimal(first) == low_decimal:
        first += 1
    last = int(high_decimal.to_integral_value(rounding=ROUND_FLOOR))
    if max_exclusive and Decimal(last) == high_decimal:
        last -= 1
    return bool(max(first, type_low) <= min(last, type_high))


def _validate_datetime_range(
    ctx: str,
    table: TableSpec,
    column: ColumnSpec,
    params: Mapping[str, Any],
) -> None:
    low = _as_datetime(params.get("min"))
    high = _as_datetime(params.get("max"))
    if low is not None and high is not None:
        try:
            if high < low:
                raise ValueError(f"{ctx}: datetime_range describe un rango invertido.")
        except TypeError as exc:
            raise ValueError(
                f"{ctx}: las cotas temporales mezclan valores con y sin zona horaria."
            ) from exc

    constraint_low, constraint_low_exclusive = _tightest_bound(table, column, name="min")
    constraint_high, constraint_high_exclusive = _tightest_bound(table, column, name="max")
    if constraint_low_exclusive or constraint_high_exclusive:
        raise ValueError(f"{ctx}: datetime_range no expresa cotas temporales exclusivas de la IR.")
    parsed_constraint_low = _as_datetime(constraint_low)
    parsed_constraint_high = _as_datetime(constraint_high)
    try:
        if low is not None and parsed_constraint_low is not None and low < parsed_constraint_low:
            raise ValueError(f"{ctx}: el mínimo temporal amplía las cotas de la IR.")
        if (
            high is not None
            and parsed_constraint_high is not None
            and high > parsed_constraint_high
        ):
            raise ValueError(f"{ctx}: el máximo temporal amplía las cotas de la IR.")
    except TypeError as exc:
        raise ValueError(f"{ctx}: las cotas temporales de plan e IR no son comparables.") from exc


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        if "T" in value or " " in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    raise ValueError(f"valor temporal inválido: {value!r}.")
