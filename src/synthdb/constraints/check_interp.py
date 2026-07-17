"""Interpretación de `CHECK` a cotas de generación (T1.4, especificacion.md §7.5).

`parsing/ddl.py` (T1.3) conserva el texto de cada `CHECK` en `CheckSpec.
sql_text` junto con `columns_involved`, pero deja siempre `ast_supported=
False` y `bounds_derived=None`: interpretar el predicado no es su trabajo.
Ese es el de este módulo: `interpret_checks()` recorre la `SchemaSpec` ya
parseada, re-parsea `sql_text` con el parser de expresiones de sqlglot (nunca
con regex sobre el texto) y, para el subconjunto que reconoce, rellena
`ast_supported=True` y `bounds_derived` con las cotas que el generador (H2)
podrá usar directamente.

Subconjunto soportado en esta entrega, siempre restringido a un `CheckSpec`
con exactamente **una** columna en `columns_involved`:

- comparaciones `col <op> literal` (y su forma invertida `literal <op> col`,
  normalizada): `>`, `>=`, `<`, `<=`, `=`, `<>`/`!=`;
- `BETWEEN a AND b` (cotas inclusivas);
- `IN (...)` (lista cerrada de valores) y `NOT IN (...)` (lista de valores
  excluidos);
- `AND` de cualquier combinación de lo anterior, intersecando las cotas. Si
  la intersección resulta vacía (p. ej. `x > 5 AND x < 3`), la restricción
  sigue siendo `ast_supported=True` — es una restricción perfectamente
  válida para PostgreSQL — pero se registra un aviso: ninguna fila podrá
  cumplirla nunca, y es mejor que el usuario lo sepa antes de generar que
  después de ver el generador atascado;
- `NOT` sobre una comparación o un `IN` (invierte el operador o vuelca sus
  valores a `excluded_values`); `NOT` sobre cualquier otra cosa no se
  interpreta.

Deliberadamente fuera de esta entrega, sin que ello genere un aviso nuevo
(es el estado normal para un `CHECK` que cae fuera del subconjunto, igual
que ya lo era en T1.3): `OR` (ni siquiera de una sola columna: `x < 3 OR x >
9` no es una cota simple), cualquier predicado multi-columna (se queda para
el mini-DSL de T2.9, que lo tratará como aserción, no como cota), funciones
(`length(x) > 3`, `upper(...)`), casts, subconsultas, y `LIKE`. `LIKE` en
particular se pospone entero — no solo los patrones con comodines al
principio, que sí tendrían un `min`/`max` de rango razonable — porque como
cota de generación aporta poco frente a la complejidad de tratarlo bien
(prefijo puro vs. patrón con `%`/`_` intercalados, escapes...); si hace
falta, se declara vía YAML como regla del mini-DSL.

No hace falta resolver qué columna concreta nombra cada `exp.Column` que
aparece en el AST re-parseado: el filtro por `len(columns_involved) == 1` ya
garantiza que solo hay una en todo el predicado, así que cualquier nodo
`Column` que se encuentre *es* esa columna, sin comparar nombres (y sin
duplicar aquí el plegado de identificadores de PostgreSQL de `parsing/
ddl.py`, que ya vive en `columns_involved`).

`ast_supported` y `bounds_derived` están excluidos de `ir/hashing.py`
(decisión de T1.5): son metadatos derivados por este módulo, no estructura
del DDL, así que interpretar los checks de una `SchemaSpec` nunca cambia su
hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from synthdb.ir.schema import CheckSpec, ColumnSpec, SchemaSpec, TableSpec

# Los nodos de sqlglot se anotan aquí como `exp.Expr`, no como el más
# habitual `exp.Expression`: en los propios stubs de sqlglot, `Expression` y
# `Condition` son subclases hermanas de `Expr` (no una de la otra), y
# `sqlglot.parse_one` devuelve `Expr`. Usar `Expr` de forma consistente en
# todo el módulo evita mezclar los dos y encadenar errores de mypy.
_COMPARISON_TYPES: tuple[type[exp.Expr], ...] = (
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.EQ,
    exp.NEQ,
)
"""Nodos de comparación binaria que este módulo sabe convertir en cotas."""

_FLIP_OP: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
}
"""Operador equivalente al normalizar `literal op columna` a `columna op literal`.

`5 > x` se lee "5 es mayor que x", es decir "x es menor que 5": de ahí que
`>` se convierta en `<` (y viceversa) al mover la columna al lado izquierdo.
`=`/`<>` son simétricos y no cambian.
"""

_NEGATE_OP: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.LTE,
    exp.GTE: exp.LT,
    exp.LT: exp.GTE,
    exp.LTE: exp.GT,
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
}
"""Operador que resulta de anteponer `NOT` a una comparación ya normalizada."""


@dataclass(frozen=True)
class _Bounds:
    """Cotas intermedias mientras se interpreta un predicado.

    Representación puramente interna de este módulo; `_bounds_to_dict` es lo
    único que la traduce al formato público de `CheckSpec.bounds_derived`.
    `=` se modela como un intervalo cerrado de un único punto (`min == max`,
    ambos inclusivos) en vez de con un campo `equals` aparte: así, una
    igualdad que contradice otra cota (`x = 5 AND x > 10`) la detecta la
    misma comparación `min > max` que ya hace falta para `x > 5 AND x < 3`,
    sin duplicar la lógica de conflicto para cada combinación posible.
    """

    min: Any = None
    min_exclusive: bool = False
    max: Any = None
    max_exclusive: bool = False
    values: list[Any] | None = None
    excluded_values: list[Any] = field(default_factory=list)


def interpret_checks(spec: SchemaSpec) -> SchemaSpec:
    """Interpreta todos los `CheckSpec` de columna y de tabla de `spec`.

    Para cada `CheckSpec` cuyo predicado caiga en el subconjunto soportado
    (ver el docstring del módulo), rellena `ast_supported=True` y
    `bounds_derived`. Los que no, quedan exactamente como llegaron (de
    T1.3: `ast_supported=False`, `bounds_derived=None`). Una restricción
    interpretada pero insatisfacible (intersección vacía) se registra como
    aviso en el `SchemaSpec` devuelto.

    Args:
        spec: Esquema ya parseado por `parsing/ddl.py`. No se modifica: se
            devuelve una copia (`SchemaSpec` y sus modelos anidados son
            inmutables en la práctica del proyecto, ver CLAUDE.md).

    Returns:
        Copia de `spec` con los `CheckSpec` interpretables ya resueltos.
    """
    warnings = list(spec.warnings)
    tables = [_interpret_table(table, spec.dialect, warnings) for table in spec.tables]
    return spec.model_copy(update={"tables": tables, "warnings": warnings})


def _interpret_table(table: TableSpec, dialect: str, warnings: list[str]) -> TableSpec:
    """`TableSpec` con sus columnas y sus checks de tabla ya interpretados."""
    columns = [_interpret_column(table.name, column, dialect, warnings) for column in table.columns]
    checks = [_interpret_check(table.name, check, dialect, warnings) for check in table.checks]
    return table.model_copy(update={"columns": columns, "checks": checks})


def _interpret_column(
    table_name: str, column: ColumnSpec, dialect: str, warnings: list[str]
) -> ColumnSpec:
    """`ColumnSpec` con sus propios checks (los que solo la afectan a ella) interpretados."""
    checks = [_interpret_check(table_name, check, dialect, warnings) for check in column.checks]
    return column.model_copy(update={"checks": checks})


def _interpret_check(
    table_name: str, check: CheckSpec, dialect: str, warnings: list[str]
) -> CheckSpec:
    """Interpreta un único `CheckSpec`, o lo devuelve intacto si no se sabe.

    El filtro `len(columns_involved) != 1` cubre de un plumazo tanto los
    predicados multi-columna (`fin >= inicio`) como los que no referencian
    ninguna columna: ninguno de los dos entra en el subconjunto de esta
    entrega (especificación de la tarea T1.4).
    """
    if len(check.columns_involved) != 1:
        return check

    node = sqlglot.parse_one(check.sql_text, read=dialect)
    bounds = _interpret_predicate(node)
    if bounds is None:
        return check

    if not _is_satisfiable(bounds):
        column_name = check.columns_involved[0]
        warnings.append(
            f"tabla {table_name}, columna {column_name}: el CHECK ({check.sql_text}) "
            "combina cotas incompatibles; PostgreSQL lo acepta pero ninguna fila podrá "
            "cumplirlo nunca. Revisa el DDL: probablemente el predicado tiene un error"
        )

    return check.model_copy(
        update={"ast_supported": True, "bounds_derived": _bounds_to_dict(bounds)}
    )


def _interpret_predicate(node: exp.Expr) -> _Bounds | None:
    """Cotas de un predicado completo, o `None` si cae fuera del subconjunto.

    Un `AND` intersecta las cotas de sus operandos, con la condición de que
    *todos* sean a su vez interpretables; basta con que uno no lo sea (una
    rama `OR`, una función, una comparación multi-columna...) para que el
    `AND` entero quede sin interpretar, tal como pide la especificación de
    la tarea.
    """
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        merged: _Bounds | None = None
        for clause in _flatten_and(node):
            clause_bounds = _interpret_atomic(clause)
            if clause_bounds is None:
                return None
            merged = clause_bounds if merged is None else _intersect(merged, clause_bounds)
        return merged
    return _interpret_atomic(node)


def _interpret_atomic(node: exp.Expr) -> _Bounds | None:
    """Cotas de una cláusula "atómica": comparación, `BETWEEN`, `IN` o su `NOT`."""
    node = _unwrap_paren(node)
    if isinstance(node, exp.Not):
        return _interpret_not(_unwrap_paren(node.this))
    if isinstance(node, exp.Between):
        return _interpret_between(node)
    if isinstance(node, exp.In):
        return _interpret_in(node)
    if isinstance(node, _COMPARISON_TYPES):
        normalized = _normalize_comparison(node)
        return _bounds_from_op(*normalized) if normalized is not None else None
    return None


def _interpret_not(node: exp.Expr) -> _Bounds | None:
    """Cotas de `NOT <node>`, solo para `node` una comparación o un `IN`."""
    if isinstance(node, exp.In):
        inner = _interpret_in(node)
        return _Bounds(excluded_values=inner.values or []) if inner is not None else None
    if isinstance(node, _COMPARISON_TYPES):
        normalized = _normalize_comparison(node)
        if normalized is None:
            return None
        op, value = normalized
        return _bounds_from_op(_NEGATE_OP[op], value)
    return None


def _interpret_between(node: exp.Between) -> _Bounds | None:
    """Cotas de `col BETWEEN low AND high` (inclusivas en ambos extremos)."""
    if not isinstance(node.this, exp.Column):
        return None
    low_ok, low = _literal_value(node.args["low"])
    high_ok, high = _literal_value(node.args["high"])
    if not (low_ok and high_ok):
        return None
    return _Bounds(min=low, min_exclusive=False, max=high, max_exclusive=False)


def _interpret_in(node: exp.In) -> _Bounds | None:
    """Cotas de `col IN (...)`: la lista cerrada de valores, en orden y sin duplicados."""
    if not isinstance(node.this, exp.Column):
        return None
    values: list[Any] = []
    for item in node.expressions:
        ok, value = _literal_value(item)
        if not ok:
            return None
        values.append(value)
    return _Bounds(values=list(dict.fromkeys(values)))


def _normalize_comparison(node: exp.Expr) -> tuple[type[exp.Expr], Any] | None:
    """Normaliza `col op literal` / `literal op col` a `(operador, literal)`.

    `None` si ninguno de los dos lados es una columna, si ambos lo son
    (`col > col`), o si el lado que no es columna no es un literal
    reconocible (subconsulta, función...).
    """
    left, right = node.this, node.expression
    if isinstance(left, exp.Column) and not isinstance(right, exp.Column):
        literal_side, op = right, type(node)
    elif isinstance(right, exp.Column) and not isinstance(left, exp.Column):
        literal_side, op = left, _FLIP_OP[type(node)]
    else:
        return None

    ok, value = _literal_value(literal_side)
    return (op, value) if ok else None


def _bounds_from_op(op: type[exp.Expr], value: Any) -> _Bounds:
    """`_Bounds` de un único operador ya normalizado (`columna <op> value`)."""
    if op is exp.GT:
        return _Bounds(min=value, min_exclusive=True)
    if op is exp.GTE:
        return _Bounds(min=value, min_exclusive=False)
    if op is exp.LT:
        return _Bounds(max=value, max_exclusive=True)
    if op is exp.LTE:
        return _Bounds(max=value, max_exclusive=False)
    if op is exp.EQ:
        return _Bounds(min=value, min_exclusive=False, max=value, max_exclusive=False)
    return _Bounds(excluded_values=[value])  # exp.NEQ: el único caso que queda en _COMPARISON_TYPES


def _literal_value(node: exp.Expr) -> tuple[bool, Any]:
    """`(True, valor_python)` si `node` es un literal reconocible; si no, `(False, None)`.

    Cubre booleanos, cadenas y números (incluidos negativos: sqlglot nunca
    pliega el signo dentro del propio `Literal`, así que un `-100` llega
    como `Neg(Literal(100))`, no como un `Literal` con `this="-100"`).
    """
    if isinstance(node, exp.Boolean):
        return True, bool(node.this)
    if isinstance(node, exp.Literal):
        return True, str(node.this) if node.is_string else _numeric_literal(str(node.this))
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return True, -_numeric_literal(str(node.this.this))
    return False, None


def _numeric_literal(text: str) -> int | float:
    """Valor Python de un literal numérico de sqlglot (`Literal.this`, siempre `str`)."""
    if "." in text or "e" in text.lower():
        return float(text)
    return int(text)


def _flatten_and(node: exp.Expr) -> list[exp.Expr]:
    """Aplana una cadena de `AND` anidados (asociativos a izquierda) a sus cláusulas."""
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        return [*_flatten_and(node.this), *_flatten_and(node.expression)]
    return [node]


def _unwrap_paren(node: exp.Expr) -> exp.Expr:
    """Quita el envoltorio `(...)` (posiblemente repetido) de un nodo."""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _intersect(a: _Bounds, b: _Bounds) -> _Bounds:
    """Cotas resultantes de exigir `a` y `b` a la vez (semántica de `AND`)."""
    min_value, min_exclusive = _tighter(
        a.min, a.min_exclusive, b.min, b.min_exclusive, pick_larger=True
    )
    max_value, max_exclusive = _tighter(
        a.max, a.max_exclusive, b.max, b.max_exclusive, pick_larger=False
    )

    if a.values is None:
        values = b.values
    elif b.values is None:
        values = a.values
    else:
        allowed = set(b.values)
        values = [v for v in a.values if v in allowed]

    excluded_values = list(dict.fromkeys([*a.excluded_values, *b.excluded_values]))

    return _Bounds(
        min=min_value,
        min_exclusive=min_exclusive,
        max=max_value,
        max_exclusive=max_exclusive,
        values=values,
        excluded_values=excluded_values,
    )


def _tighter(
    a_value: Any, a_exclusive: bool, b_value: Any, b_exclusive: bool, *, pick_larger: bool
) -> tuple[Any, bool]:
    """La cota más estricta entre `a` y `b` (la mayor para `min`, la menor para `max`).

    Un mismo valor límite en ambas es más estricto si cualquiera de las dos
    lo marcaba como exclusivo (`x > 5 AND x >= 5` excluye 5 igual que solo
    `x > 5`).
    """
    if a_value is None:
        return b_value, b_exclusive
    if b_value is None:
        return a_value, a_exclusive
    if a_value == b_value:
        return a_value, a_exclusive or b_exclusive
    a_wins = a_value > b_value if pick_larger else a_value < b_value
    return (a_value, a_exclusive) if a_wins else (b_value, b_exclusive)


def _is_satisfiable(bounds: _Bounds) -> bool:
    """`False` si ningún valor puede cumplir a la vez todas las cotas acumuladas."""
    if bounds.values is not None and not bounds.values:
        return False
    if bounds.min is not None and bounds.max is not None:
        if bounds.min > bounds.max:
            return False
        if bounds.min == bounds.max and (bounds.min_exclusive or bounds.max_exclusive):
            return False
    return True


def _bounds_to_dict(bounds: _Bounds) -> dict[str, Any]:
    """`_Bounds` al formato de `CheckSpec.bounds_derived`: solo las claves presentes."""
    result: dict[str, Any] = {}
    is_exact_point = (
        bounds.min is not None
        and bounds.max is not None
        and bounds.min == bounds.max
        and not bounds.min_exclusive
        and not bounds.max_exclusive
    )
    if is_exact_point:
        result["equals"] = bounds.min
    else:
        if bounds.min is not None:
            result["min"] = bounds.min
            result["min_exclusive"] = bounds.min_exclusive
        if bounds.max is not None:
            result["max"] = bounds.max
            result["max_exclusive"] = bounds.max_exclusive
    if bounds.values is not None:
        result["values"] = bounds.values
    if bounds.excluded_values:
        result["excluded_values"] = bounds.excluded_values
    return result
