"""Intérprete seguro del mini-DSL: `evaluate` y `check` (T2.9, especificacion.md §7.2).

Este es el intérprete de **lista blanca cerrada** que CLAUDE.md exige: recorre el
AST propio de `rules/dsl.py` y evalúa cada nodo con operaciones nativas de Python
elegidas a mano. No hay `eval`, `exec`, `compile`, ni `getattr` dinámico sobre
ningún nombre venido del usuario o del modelo: un nombre de función solo puede
resolver a una entrada de `FUNCTIONS`, y un nombre de columna/constante solo se usa
como clave de un `dict` de datos. No hay ninguna vía por la que el texto de una
regla ejecute código Python arbitrario.

`FUNCTIONS` es el **único** dict de la lista blanca de funciones (la gramática lo
consulta en tiempo de parseo para rechazar lo que no esté aquí). Cada entrada
declara su aridad y una implementación que opera sobre los VALORES ya evaluados de
sus argumentos y el `RowContext` (necesario para `noise`, que usa el RNG de la
fila). Añadir una función al DSL es añadir una entrada aquí con sus tests, nunca
abrir una puerta genérica.

Doble uso (§7.2): `evaluate` produce el valor de una expresión (para derivaciones y
para el valor de una cota); `check` la evalúa como predicado booleano (para las
aserciones que se comprueban tras generar). Toda regla se re-evalúa siempre como
aserción final, sea cual sea su `RuleKind`.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from synthdb.rules.dsl import (
    Arith,
    BoolOp,
    Call,
    Col,
    Compare,
    Const,
    Neg,
    Node,
    Not,
    ParentCol,
    Ref,
    Rule,
)

if TYPE_CHECKING:
    from synthdb.generation.context import RowContext


class RuleEvalError(ValueError):
    """Error al evaluar una regla sobre una fila concreta (§7.2, T2.9).

    Cubre columna inexistente en la fila, `ref` desconocida, fila padre ausente,
    división entre cero, y cualquier incompatibilidad de tipos. El mensaje incluye
    la regla y un extracto de la fila para que el fallo sea localizable, en línea
    con CLAUDE.md (mensajes de error orientados a acción). Se lanza en ejecución,
    no en compilación: los errores de gramática son `RuleParseError` (dsl.py).
    """

    def __init__(self, message: str, *, rule: Rule, row: dict[str, Any]) -> None:
        self.rule = rule
        self.row = row
        super().__init__(f"{message} — regla: {rule.text!r}; fila: {row!r}")


@dataclass(frozen=True, slots=True)
class _FnSpec:
    """Entrada de la lista blanca de funciones: aridad e implementación."""

    min_args: int
    max_args: int
    impl: Callable[[list[Any], RowContext], Any]


def _fn_date(args: list[Any], _ctx: RowContext) -> _dt.date:
    """`date(anio, mes, dia)` → un `datetime.date`."""
    year, month, day = (_as_int(a, "date") for a in args)
    return _dt.date(year, month, day)


def _fn_date_add(args: list[Any], _ctx: RowContext) -> _dt.date | _dt.datetime:
    """`date_add(fecha, dias)` → la fecha desplazada `dias` días."""
    base, days = args
    if not isinstance(base, _dt.date | _dt.datetime):
        raise TypeError(
            f"date_add: el primer argumento debe ser una fecha, no {type(base).__name__}"
        )
    return base + _dt.timedelta(days=_as_int(days, "date_add"))


def _fn_years_between(args: list[Any], _ctx: RowContext) -> int:
    """`years_between(a, b)` → años completos de `b` a `a` (edad, entero con signo)."""
    a, b = (_as_date(x, "years_between") for x in args)
    return a.year - b.year - ((a.month, a.day) < (b.month, b.day))


def _fn_noise(args: list[Any], ctx: RowContext) -> float:
    """`noise(sigma)` → multiplicador `1 + N(0, sigma)` con el RNG de la fila.

    Multiplicativo y centrado en 1: `x * noise(0.2)` perturba `x` en ±20 % (1σ). Es
    determinista porque toda su aleatoriedad sale de `ctx.rng` (nunca de `random`
    global): misma fila ⇒ mismo ruido (CLAUDE.md).
    """
    (sigma,) = args
    if not isinstance(sigma, int | float) or isinstance(sigma, bool):
        raise TypeError(f"noise: sigma debe ser un número, no {type(sigma).__name__}")
    if sigma < 0:
        raise ValueError(f"noise: sigma no puede ser negativo ({sigma})")
    return 1.0 + ctx.rng.gauss(0.0, float(sigma))


def _fn_round(args: list[Any], _ctx: RowContext) -> float:
    """`round(x, ndigits=0)` → `x` redondeado a `ndigits` decimales."""
    if len(args) == 1:
        x, ndigits = args[0], 0
    else:
        x, ndigits = args[0], _as_int(args[1], "round")
    if not isinstance(x, int | float) or isinstance(x, bool):
        raise TypeError(f"round: el primer argumento debe ser un número, no {type(x).__name__}")
    return round(float(x), ndigits)


def _fn_len(args: list[Any], _ctx: RowContext) -> int:
    """`len(texto)` → longitud de la cadena."""
    (text,) = args
    if not isinstance(text, str):
        raise TypeError(f"len: el argumento debe ser una cadena, no {type(text).__name__}")
    return len(text)


def _as_int(value: Any, func: str) -> int:
    """Coacciona a `int` un valor que debe ser entero (rechaza booleanos y floats no enteros)."""
    if isinstance(value, bool):
        raise TypeError(f"{func}: se esperaba un entero, no un booleano")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise TypeError(f"{func}: se esperaba un entero, no {value!r}")


def _as_date(value: Any, func: str) -> _dt.date:
    """Exige que `value` sea una fecha (o `datetime`, del que se toma la parte fecha)."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    raise TypeError(f"{func}: se esperaba una fecha, no {type(value).__name__}")


FUNCTIONS: dict[str, _FnSpec] = {
    "date": _FnSpec(3, 3, _fn_date),
    "date_add": _FnSpec(2, 2, _fn_date_add),
    "years_between": _FnSpec(2, 2, _fn_years_between),
    "noise": _FnSpec(1, 1, _fn_noise),
    "round": _FnSpec(1, 2, _fn_round),
    "len": _FnSpec(1, 1, _fn_len),
}
"""La ÚNICA lista blanca de funciones del mini-DSL (§7.2, CLAUDE.md).

`dsl.py` la consulta en tiempo de parseo (nombres + aridad) para rechazar cualquier
otra función con `RuleParseError`; `evaluate` la consulta en ejecución para
resolver un `Call`. No hay ninguna otra vía para invocar una función.
"""

_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": lambda a, b: bool(a == b),
    "<>": lambda a, b: bool(a != b),
    "<": lambda a, b: bool(a < b),
    "<=": lambda a, b: bool(a <= b),
    ">": lambda a, b: bool(a > b),
    ">=": lambda a, b: bool(a >= b),
}
"""Operadores de comparación del DSL a su función Python (resultado `bool`)."""

_ARITH: dict[str, Callable[[Any, Any], Any]] = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
}
"""Operadores aritméticos del DSL. `/` es división real (float), como en Python."""


def evaluate(rule: Rule, ctx: RowContext) -> Any:
    """Evalúa una regla (o subexpresión) sobre una fila y devuelve su valor.

    Args:
        rule: La regla parseada (`dsl.parse_rule`, o una subexpresión envuelta en
            `Rule`, como `Bound.expr`).
        ctx: El contexto de la fila (`row`, `parent()`, `refs`, `rng`).

    Returns:
        El valor de la expresión; su tipo Python depende de la regla (número,
        fecha, cadena, booleano, `None`).

    Raises:
        RuleEvalError: Ante columna inexistente, `ref` desconocida, fila padre
            ausente, división entre cero o incompatibilidad de tipos.
    """
    try:
        return _eval(rule.root, ctx, rule)
    except RuleEvalError:
        raise
    except (KeyError, ValueError, TypeError, ZeroDivisionError, IndexError, OverflowError) as err:
        raise RuleEvalError(f"error al evaluar la regla: {err}", rule=rule, row=ctx.row) from err


def check(rule: Rule, ctx: RowContext) -> bool:
    """Evalúa una regla como aserción booleana sobre una fila (§7.2).

    Es el segundo uso de toda regla: tras generar la fila se re-comprueba que la
    regla se cumple. Exige que la regla evalúe a un booleano (una comparación, un
    conector `and/or/not`, o `col = expr` leído como igualdad); una expresión que
    no dé un `bool` (p. ej. una aritmética suelta) no es una aserción válida.

    Raises:
        RuleEvalError: Si la regla falla al evaluar, o si su valor no es un booleano.
    """
    value = evaluate(rule, ctx)
    if not isinstance(value, bool):
        raise RuleEvalError(
            f"la regla no evalúa a un booleano (dio {value!r}: {type(value).__name__}); "
            "una aserción debe ser una comparación o una expresión booleana",
            rule=rule,
            row=ctx.row,
        )
    return value


def _eval(node: Node, ctx: RowContext, rule: Rule) -> Any:
    """Evalúa un nodo del AST. Dispatch cerrado por tipo de nodo."""
    if isinstance(node, Const):
        return node.value
    if isinstance(node, Col):
        return _eval_column(node, ctx, rule)
    if isinstance(node, ParentCol):
        return _eval_parent(node, ctx, rule)
    if isinstance(node, Ref):
        return _eval_ref(node, ctx, rule)
    if isinstance(node, Call):
        spec = FUNCTIONS[node.func]
        args = [_eval(arg, ctx, rule) for arg in node.args]
        return spec.impl(args, ctx)
    if isinstance(node, Compare):
        left = _eval(node.left, ctx, rule)
        right = _eval(node.right, ctx, rule)
        return _COMPARATORS[node.op](left, right)
    if isinstance(node, Arith):
        left = _eval(node.left, ctx, rule)
        right = _eval(node.right, ctx, rule)
        return _ARITH[node.op](left, right)
    if isinstance(node, BoolOp):
        return _eval_boolop(node, ctx, rule)
    if isinstance(node, Not):
        return not _as_bool(_eval(node.operand, ctx, rule), rule, ctx)
    if isinstance(node, Neg):
        return -_eval(node.operand, ctx, rule)
    raise AssertionError(f"nodo del AST no contemplado: {type(node).__name__}")  # inalcanzable


def _eval_column(node: Col, ctx: RowContext, rule: Rule) -> Any:
    """Lee una columna local de la fila, o error si aún no está generada/no existe."""
    try:
        return ctx.row[node.name]
    except KeyError:
        raise RuleEvalError(
            f"la columna '{node.name}' no está disponible en la fila (¿no existe, o "
            "se genera después de esta regla?)",
            rule=rule,
            row=ctx.row,
        ) from None


def _eval_parent(node: ParentCol, ctx: RowContext, rule: Rule) -> Any:
    """Resuelve `parent(<fk>).<columna>` contra la fila padre inyectada en el contexto."""
    parent = ctx.parent(node.fk)
    if parent is None:
        raise RuleEvalError(
            f"parent({node.fk}) no tiene fila padre en este contexto (la FK es NULL o no "
            "se inyectó su padre)",
            rule=rule,
            row=ctx.row,
        )
    try:
        return parent[node.column]
    except KeyError:
        raise RuleEvalError(
            f"la fila padre de '{node.fk}' no tiene la columna '{node.column}'",
            rule=rule,
            row=ctx.row,
        ) from None


def _eval_ref(node: Ref, ctx: RowContext, rule: Rule) -> Any:
    """Resuelve `ref('<nombre>')` contra el bloque `refs` del contexto."""
    try:
        return ctx.refs[node.name]
    except KeyError:
        raise RuleEvalError(
            f"ref('{node.name}') no está definida en el bloque 'refs' de la configuración",
            rule=rule,
            row=ctx.row,
        ) from None


def _eval_boolop(node: BoolOp, ctx: RowContext, rule: Rule) -> bool:
    """Evalúa `and`/`or` con cortocircuito, exigiendo operandos booleanos."""
    left = _as_bool(_eval(node.left, ctx, rule), rule, ctx)
    if node.op == "and" and not left:
        return False
    if node.op == "or" and left:
        return True
    return _as_bool(_eval(node.right, ctx, rule), rule, ctx)


def _as_bool(value: Any, rule: Rule, ctx: RowContext) -> bool:
    """Exige que `value` sea un booleano; `and/or/not` no operan sobre no-booleanos."""
    if not isinstance(value, bool):
        raise RuleEvalError(
            f"se esperaba un booleano en un operador lógico, se obtuvo {value!r} "
            f"({type(value).__name__})",
            rule=rule,
            row=ctx.row,
        )
    return value
