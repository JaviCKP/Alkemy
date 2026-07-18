"""El fusor: combina usuario, IR y heurísticas en un plan por columna (T2.6).

Implementa la cadena de prioridad de la especificación §7.1, en este orden
exacto y sin excepciones, dejando rastro (`source`) en cada decisión:

1. **Usuario (YAML).** Manda siempre, PERO se valida contra la IR: un `choice`
   con valores fuera del enum/CHECK, o unas cotas de usuario fuera de las del
   CHECK, se rechazan con `PlanError` (ni el usuario puede pedir datos que la BD
   rechazará). Las cotas que el usuario no fija las completa la IR (abajo).
2. **IR (la BD como fuente de verdad).** `enum_values`/`CHECK IN` ⇒ `choice`;
   `autoincrement`/`GENERATED` ⇒ la BD asigna el valor y la columna se excluye
   de los INSERT; `bounds_derived` recorta las cotas del generador elegido por
   CUALQUIER fuente (intersección); un `UNIQUE`/PK de una sola columna fuerza
   `unique=True`.
3. **LLM.** Reservado (`source="llm"` existe en `ColumnPlan`) pero sin camino en
   el H2: entra en el fusor en el H3 con la confianza *efectiva* de ADR-002.
4. **Heurísticas** (`semantic/heuristics.py`), si superan `min_confidence`.
5. **Fallback seguro** por tipo, siempre con aviso por columna.

Determinismo (CLAUDE.md): recorre tablas y columnas en el orden de la IR y no
itera sobre estructuras sin orden definido, así que mismo `spec` + misma
`Config` ⇒ mismo `TablePlans` byte a byte (`canonical_json`).

Fuera de alcance de esta sesión, señalado con avisos para que nada quede en
silencio: el **selector de claves foráneas** (sesión C, T2.8) — las columnas de
una FK reciben aquí un generador provisional y un aviso; y las **reglas** del
mini-DSL (sesión D, T2.9) — se guardan sin interpretar en la config.
"""

from __future__ import annotations

from typing import Any

from synthdb.config.models import ColumnConfig, Config, Defaults, TableConfig
from synthdb.ir.plans import ColumnPlan, PlanSource, TablePlan, TablePlans
from synthdb.ir.schema import ColumnSpec, GeneratorSpec, SchemaSpec, TableSpec
from synthdb.semantic.heuristics import infer_column

_RANGE_GENERATORS = frozenset({"numeric_range", "datetime_range"})


class PlanError(ValueError):
    """Contradicción irresoluble entre la configuración del usuario y la IR.

    Se lanza cuando el usuario pide algo que la base de datos rechazaría (valores
    fuera de un enum/CHECK, cotas fuera de las del CHECK, `null_ratio` sobre una
    columna `NOT NULL`). El mensaje nombra la tabla y la columna y las dos partes
    en conflicto, para que el usuario sepa qué corregir en el YAML o en el DDL.
    """


def build_plan(spec: SchemaSpec, config: Config) -> TablePlans:
    """Funde configuración, IR y heurísticas en un plan de generación por columna.

    Args:
        spec: La IR ya parseada y con los CHECK interpretados
            (`constraints/check_interp.py`): el fusor lee `enum_values`,
            `bounds_derived`, `autoincrement`, PK/UNIQUE y FKs, pero no reinterpreta
            el esquema.
        config: La configuración del usuario ya validada (`config/loader.py`).

    Returns:
        Un `TablePlans` con un `ColumnPlan` por columna, en el orden de la IR.

    Raises:
        PlanError: Si la configuración del usuario contradice una restricción de
            la IR (ver la clase).
    """
    tables = [_plan_table(table, config) for table in spec.tables]
    return TablePlans(tables=tables)


def _plan_table(table: TableSpec, config: Config) -> TablePlan:
    tconf = config.tables.get(table.name)
    columns = [_plan_column(table, column, tconf, config) for column in table.columns]
    return TablePlan(table=table.name, columns=columns)


def _plan_column(
    table: TableSpec, column: ColumnSpec, tconf: TableConfig | None, config: Config
) -> ColumnPlan:
    """Aplica la cadena §7.1 a una columna y devuelve su `ColumnPlan`."""
    ctx = f"{table.name}.{column.name}"
    warnings: list[str] = []
    bounds = _column_bounds(column)
    enum_set = _enum_set(column, bounds)
    unique_ir = _is_unique_ir(table, column)
    fk_ref = _fk_reference(table, column)
    cconf = tconf.columns.get(column.name) if tconf is not None else None

    # (2, IR) La base de datos asigna el valor: se excluye de la generación.
    if column.type.autoincrement or column.generated:
        return _db_managed_plan(column)

    # --- selección de fuente (orden §7.1) ---
    source: PlanSource
    confidence: float
    role: str | None
    if cconf is not None and cconf.generator is not None:
        generator = GeneratorSpec(type=cconf.generator, params=dict(cconf.params))
        _validate_user_generator(ctx, generator, enum_set, bounds, warnings)
        source, confidence, role = "user", 1.0, None
    elif enum_set is not None:
        generator = GeneratorSpec(type="choice", params={"values": list(enum_set)})
        source, confidence, role = "ir", 1.0, "enum"
    else:
        heuristic = infer_column(table, column)
        min_confidence = config.llm.min_confidence
        if heuristic is not None and heuristic.confidence >= min_confidence:
            generator = heuristic.generator
            source, confidence, role = "heuristic", heuristic.confidence, heuristic.role
        else:
            generator = GeneratorSpec(type="fallback")
            source, confidence, role = "fallback", 0.0, (heuristic.role if heuristic else None)
            warnings.append(
                f"{ctx}: ninguna fuente semántica supera el umbral "
                f"(min_confidence={config.llm.min_confidence}); se usa el generador 'fallback' "
                f"seguro por tipo. Revisa el plan o fija un generador en el YAML si necesitas "
                f"datos con dominio."
            )

    # --- (2, IR) recorte de cotas: se aplica a cualquier fuente ---
    if generator.type in _RANGE_GENERATORS:
        generator = _clip_to_bounds(generator, bounds, warnings, ctx)

    # --- (2, IR) unicidad y null_ratio ---
    user_unique = cconf.unique if cconf is not None else None
    if unique_ir and user_unique is False:
        warnings.append(
            f"{ctx}: la IR declara la columna UNIQUE; se fuerza unique=True pese a "
            f"'unique: false' en el YAML (las restricciones de la BD mandan)."
        )
    null_ratio = _resolve_null_ratio(ctx, column, cconf, config.defaults)
    updates: dict[str, Any] = {}
    if (unique_ir or bool(user_unique)) and not generator.unique:
        updates["unique"] = True
    if null_ratio != generator.null_ratio:
        updates["null_ratio"] = null_ratio
    if updates:
        generator = generator.model_copy(update=updates)

    if fk_ref is not None:
        warnings.append(
            f"{ctx}: forma parte de una clave foránea (→ {fk_ref}); el selector de claves "
            f"foráneas es de la sesión C (T2.8). El generador asignado aquí es provisional y "
            f"no garantiza referencias válidas."
        )

    return ColumnPlan(
        column=column.name,
        generator=generator,
        source=source,
        confidence=confidence,
        role=role,
        warnings=warnings,
    )


def _db_managed_plan(column: ColumnSpec) -> ColumnPlan:
    """`ColumnPlan` de una columna que asigna la propia base de datos."""
    if column.type.autoincrement:
        why = "autoincremental (SERIAL): la base de datos asigna el valor"
        role: str | None = "identificador"
    else:
        why = "GENERATED ALWAYS AS: la base de datos calcula el valor"
        role = None
    return ColumnPlan(
        column=column.name,
        generator=None,
        source="ir",
        confidence=1.0,
        role=role,
        warnings=[f"{why}; se excluye de la generación y de los INSERT."],
    )


# --- Lectura de restricciones de la IR ----------------------------------------


def _enum_set(column: ColumnSpec, bounds: dict[str, Any]) -> list[Any] | None:
    """Conjunto cerrado de valores de la columna: `enum_values` o `CHECK IN`.

    `enum_values` (tipo enum de PostgreSQL) tiene prioridad sobre los valores de
    un `CHECK ... IN (...)`; si además hay `excluded_values` (de un `<>`/`NOT IN`),
    se descuentan. `None` si la columna no tiene un dominio cerrado.
    """
    values = column.enum_values if column.enum_values is not None else bounds.get("values")
    if values is None:
        return None
    excluded = set(bounds.get("excluded_values", []))
    return [v for v in values if v not in excluded]


def _is_unique_ir(table: TableSpec, column: ColumnSpec) -> bool:
    """`True` si la IR obliga a que la columna sea única por sí sola.

    Una PK o un UNIQUE de UNA sola columna fuerzan unicidad; los compuestos
    (varias columnas) no obligan a que cada columna sea única por separado.
    """
    single = [column.name]
    return table.primary_key == single or single in table.uniques


def _fk_reference(table: TableSpec, column: ColumnSpec) -> str | None:
    """`"tabla(col)"` si la columna participa en alguna FK, si no `None`."""
    for fk in table.foreign_keys:
        if column.name in fk.columns:
            ref_cols = ", ".join(fk.ref_columns) if fk.ref_columns else "?"
            return f"{fk.ref_table}({ref_cols})"
    return None


def _column_bounds(column: ColumnSpec) -> dict[str, Any]:
    """Funde las cotas de todos los CHECK de la columna en un único diccionario.

    Normaliza `equals` a `min == max` inclusivos e intersecta las cotas de varios
    CHECK sobre la misma columna (semántica de AND). El resultado usa siempre las
    claves `min`/`min_exclusive`/`max`/`max_exclusive`/`values`/`excluded_values`.
    """
    agg = _empty_bounds()
    for check in column.checks:
        if check.bounds_derived:
            agg = _intersect_bounds(agg, _normalize_bounds(check.bounds_derived))
    return agg


def _empty_bounds() -> dict[str, Any]:
    return {
        "min": None,
        "min_exclusive": False,
        "max": None,
        "max_exclusive": False,
        "values": None,
        "excluded_values": [],
    }


def _normalize_bounds(bd: dict[str, Any]) -> dict[str, Any]:
    """Un `CheckSpec.bounds_derived` en la forma normalizada (`equals` ⇒ min==max)."""
    b = _empty_bounds()
    if "equals" in bd:
        b["min"] = b["max"] = bd["equals"]
    else:
        if "min" in bd:
            b["min"] = bd["min"]
            b["min_exclusive"] = bd.get("min_exclusive", False)
        if "max" in bd:
            b["max"] = bd["max"]
            b["max_exclusive"] = bd.get("max_exclusive", False)
    if "values" in bd:
        b["values"] = list(bd["values"])
    if "excluded_values" in bd:
        b["excluded_values"] = list(bd["excluded_values"])
    return b


def _intersect_bounds(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Intersección de dos cotas normalizadas (min mayor, max menor, valores en común)."""
    result = _empty_bounds()
    result["min"], result["min_exclusive"] = _tighter(
        a["min"], a["min_exclusive"], b["min"], b["min_exclusive"], pick_larger=True
    )
    result["max"], result["max_exclusive"] = _tighter(
        a["max"], a["max_exclusive"], b["max"], b["max_exclusive"], pick_larger=False
    )
    if a["values"] is None:
        result["values"] = b["values"]
    elif b["values"] is None:
        result["values"] = a["values"]
    else:
        allowed = set(b["values"])
        result["values"] = [v for v in a["values"] if v in allowed]
    result["excluded_values"] = list(dict.fromkeys([*a["excluded_values"], *b["excluded_values"]]))
    return result


def _tighter(
    a_value: Any, a_exclusive: bool, b_value: Any, b_exclusive: bool, *, pick_larger: bool
) -> tuple[Any, bool]:
    """La cota más estricta entre `a` y `b` (mayor para `min`, menor para `max`)."""
    if a_value is None:
        return b_value, b_exclusive
    if b_value is None:
        return a_value, a_exclusive
    if a_value == b_value:
        return a_value, a_exclusive or b_exclusive
    a_wins = a_value > b_value if pick_larger else a_value < b_value
    return (a_value, a_exclusive) if a_wins else (b_value, b_exclusive)


# --- Recorte de generadores contra las cotas de la IR -------------------------


def _clip_to_bounds(
    generator: GeneratorSpec, bounds: dict[str, Any], warnings: list[str], ctx: str
) -> GeneratorSpec:
    """Interseca las cotas del generador de rango con las de la IR (§7.1, punto 2).

    Rellena las cotas que el generador no fija con las del CHECK y estrecha las
    que sí, de modo que ninguna fuente pueda proponer valores fuera de lo que la
    BD acepta. `excluded_values` (de un `<>`/`NOT IN`) no se puede expresar como
    un rango, así que se avisa en vez de silenciarlo.
    """
    params = dict(generator.params)
    new_min, new_min_excl = _tighter(
        params.get("min"),
        bool(params.get("min_exclusive", False)),
        bounds["min"],
        bounds["min_exclusive"],
        pick_larger=True,
    )
    new_max, new_max_excl = _tighter(
        params.get("max"),
        bool(params.get("max_exclusive", False)),
        bounds["max"],
        bounds["max_exclusive"],
        pick_larger=False,
    )
    if new_min is not None:
        params["min"] = new_min
        if new_min_excl:
            params["min_exclusive"] = True
    if new_max is not None:
        params["max"] = new_max
        if new_max_excl:
            params["max_exclusive"] = True

    if bounds["excluded_values"]:
        warnings.append(
            f"{ctx}: el CHECK excluye {bounds['excluded_values']}, algo que el generador "
            f"'{generator.type}' no puede evitar por rango; podría producir alguno de esos "
            f"valores. Si es crítico, añádelo como regla YAML (sesión D, T2.9)."
        )
    if params == generator.params:
        return generator
    return generator.model_copy(update={"params": params})


# --- Validación de la configuración del usuario contra la IR ------------------


def _validate_user_generator(
    ctx: str,
    generator: GeneratorSpec,
    enum_set: list[Any] | None,
    bounds: dict[str, Any],
    warnings: list[str],
) -> None:
    """Rechaza (o avisa de) una configuración de usuario que contradice la IR."""
    if generator.type == "choice":
        _validate_user_choice(ctx, generator, enum_set, bounds)
    elif generator.type == "numeric_range":
        _validate_user_range(ctx, generator, bounds)
    elif enum_set is not None:
        warnings.append(
            f"{ctx}: la IR restringe la columna a un dominio cerrado {enum_set}, pero el YAML "
            f"fija el generador '{generator.type}', que no lo garantiza. Asegúrate de que solo "
            f"produce esos valores o usa 'choice'."
        )


def _validate_user_choice(
    ctx: str, generator: GeneratorSpec, enum_set: list[Any] | None, bounds: dict[str, Any]
) -> None:
    values = generator.params.get("values")
    if not isinstance(values, list):
        return  # la validación fina de params es de `generators.resolve`, no de aquí
    if enum_set is not None:
        invalid = [v for v in values if v not in enum_set]
        if invalid:
            raise PlanError(
                f"{ctx}: el YAML fija un 'choice' con valores {invalid} que la IR no permite "
                f"(dominio cerrado {enum_set}, de un enum o un CHECK IN). Ni el usuario puede "
                f"pedir datos que la BD rechazará: corrige los valores del YAML o el DDL."
            )
    for value in values:
        if not _value_satisfies(value, bounds):
            raise PlanError(
                f"{ctx}: el valor {value!r} del 'choice' del YAML viola las cotas del CHECK "
                f"({_describe_bounds(bounds)}). Corrige el YAML o el DDL."
            )


def _validate_user_range(ctx: str, generator: GeneratorSpec, bounds: dict[str, Any]) -> None:
    params = generator.params
    user_min = params.get("min")
    user_max = params.get("max")
    if bounds["min"] is not None and user_min is not None and _below_min(user_min, bounds):
        raise PlanError(
            f"{ctx}: el YAML pide min={user_min}, por debajo de la cota del CHECK "
            f"({_describe_bounds(bounds)}). Ni el usuario puede ampliar el rango que la BD "
            f"acepta: sube el min del YAML o cambia el DDL."
        )
    if bounds["max"] is not None and user_max is not None and _above_max(user_max, bounds):
        raise PlanError(
            f"{ctx}: el YAML pide max={user_max}, por encima de la cota del CHECK "
            f"({_describe_bounds(bounds)}). Baja el max del YAML o cambia el DDL."
        )


def _below_min(value: Any, bounds: dict[str, Any]) -> bool:
    low = bounds["min"]
    if low is None or not _comparable(value, low):
        return False
    return bool(value < low or (value == low and bounds["min_exclusive"]))


def _above_max(value: Any, bounds: dict[str, Any]) -> bool:
    high = bounds["max"]
    if high is None or not _comparable(value, high):
        return False
    return bool(value > high or (value == high and bounds["max_exclusive"]))


def _value_satisfies(value: Any, bounds: dict[str, Any]) -> bool:
    """`True` si `value` cumple todas las cotas (para validar valores de un choice)."""
    if bounds["values"] is not None and value not in bounds["values"]:
        return False
    if value in bounds["excluded_values"]:
        return False
    return not (_below_min(value, bounds) or _above_max(value, bounds))


def _comparable(a: Any, b: Any) -> bool:
    """`True` si `a` y `b` admiten comparación de orden sin excepción (números o mismo tipo)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return True
    return type(a) is type(b)


def _describe_bounds(bounds: dict[str, Any]) -> str:
    parts: list[str] = []
    if bounds["min"] is not None:
        parts.append(f"min{'>' if bounds['min_exclusive'] else '>='}{bounds['min']}")
    if bounds["max"] is not None:
        parts.append(f"max{'<' if bounds['max_exclusive'] else '<='}{bounds['max']}")
    if bounds["values"] is not None:
        parts.append(f"valores∈{bounds['values']}")
    if bounds["excluded_values"]:
        parts.append(f"excluye {bounds['excluded_values']}")
    return ", ".join(parts) or "sin cotas"


# --- null_ratio ---------------------------------------------------------------


def _resolve_null_ratio(
    ctx: str, column: ColumnSpec, cconf: ColumnConfig | None, defaults: Defaults
) -> float:
    """Resuelve el `null_ratio` efectivo; error si se pide NULL en una columna NOT NULL."""
    explicit = cconf.null_ratio if cconf is not None else None
    if not column.nullable:
        if explicit is not None and explicit > 0:
            raise PlanError(
                f"{ctx}: el YAML pide null_ratio={explicit} sobre una columna NOT NULL; una "
                f"columna no anulable no puede contener NULL. Quita null_ratio del YAML o haz "
                f"la columna anulable en el DDL."
            )
        return 0.0
    if explicit is not None:
        return explicit
    return defaults.null_ratio
