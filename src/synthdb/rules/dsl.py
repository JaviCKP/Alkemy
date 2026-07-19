"""Mini-DSL de reglas: parser y AST propio (T2.9, especificacion.md §7.2).

Las `rules` del YAML llegan como cadenas crudas (decisión de la sesión B). Este
módulo las convierte en un AST **propio y tipado** (`Rule`) que el intérprete de
`rules/eval.py` sabe evaluar sin `eval`/`exec`. La gramática es **cerrada**: se
documenta entera en `docs/dsl.md` y cualquier construcción fuera de ella se
rechaza con `RuleParseError`.

Estrategia de parseo (§7.2, plan T2.9): se reutiliza el parser de EXPRESIONES de
sqlglot (dialecto postgres) para no reimplementar precedencias ni tokenización, y
acto seguido se **traduce** su AST al nuestro. La traducción es una lista blanca
por tipo de nodo: un nodo de sqlglot que no esté contemplado aquí ⇒
`RuleParseError` con el fragmento culpable. Nunca se guarda ni se evalúa un nodo
de sqlglot tal cual: el AST de sqlglot es rico (subconsultas, funciones
arbitrarias, casts, `LIKE`...) y dejar pasar cualquier nodo sin traducir sería la
puerta trasera que CLAUDE.md prohíbe. Esta sesión es donde esa regla se juega el
proyecto, así que el traductor es de lista blanca CERRADA: enumera lo permitido y
rechaza todo lo demás por defecto.

La lista blanca de **funciones** (`date`, `date_add`, `years_between`, `noise`,
`round`, `len`) vive en un único dict de `rules/eval.py`; aquí solo se consulta
(nombres y aridad) para rechazar en tiempo de parseo lo que no esté en ella. Ese
import se hace de forma perezosa dentro de `_make_call` para no crear un ciclo con
`eval.py` (que sí importa el AST de este módulo).

`parent(<col_fk>)` y `ref('<nombre>')` NO son funciones de esa lista: son
producciones propias de la gramática (`ParentCol`, `Ref`), porque no calculan
sobre valores sino que resuelven una referencia estructural (la fila padre) o una
constante con nombre del bloque `refs`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as _SqlglotParseError

RuleKind = Literal["bound", "derivation", "assertion"]
"""Clasificación de una regla según su uso principal en el motor (§7.2).

- ``"bound"``: desigualdad con una columna local despejada a un lado y una
  expresión evaluable sin esa columna al otro; el motor la usa como cota del
  generador de esa columna.
- ``"derivation"``: ``col = expresión``; el generador `derived` de esa columna
  calcula su valor evaluando la expresión.
- ``"assertion"``: cualquier otra cosa evaluable; solo se comprueba tras generar.

Sea cual sea el kind, **toda** regla se re-evalúa siempre como aserción final
(doble uso de §7.2): la clasificación elige el uso *adicional* como cota o como
derivación, no exime de la validación.
"""

_DIALECT = "postgres"
"""Dialecto con el que sqlglot tokeniza y parsea la expresión de origen."""


class RuleParseError(ValueError):
    """Una regla usa una construcción fuera de la gramática cerrada del mini-DSL.

    El mensaje nombra la regla completa y el fragmento concreto que no se supo
    traducir, para que el usuario sepa qué reescribir en el YAML. Se lanza en
    tiempo de compilación del plan (nunca a mitad de generación) y jamás llega a
    ejecutarse nada del fragmento rechazado: es la barrera que garantiza que el
    intérprete solo ve nodos de la lista blanca.
    """


# --- AST propio -----------------------------------------------------------
#
# Nodos inmutables (`frozen=True`): un `Rule` parseado es un valor compartible y
# reutilizable entre filas sin riesgo de mutación. `slots=True` los hace además
# compactos, porque el motor puede tener miles de reglas vivas.


@dataclass(frozen=True, slots=True)
class Const:
    """Literal: número (`int`/`float`), cadena, booleano o `None` (NULL)."""

    value: object


@dataclass(frozen=True, slots=True)
class Col:
    """Referencia a una columna de la fila en curso (`ctx.row[name]`)."""

    name: str


@dataclass(frozen=True, slots=True)
class ParentCol:
    """`parent(<fk>).<columna>`: una columna de la fila padre elegida por la FK."""

    fk: str
    column: str


@dataclass(frozen=True, slots=True)
class Ref:
    """`ref('<nombre>')`: una constante con nombre del bloque `refs` del YAML."""

    name: str


@dataclass(frozen=True, slots=True)
class Call:
    """Llamada a una función de la lista blanca (`func`), con sus argumentos."""

    func: str
    args: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Compare:
    """Comparación binaria (`=`, `<>`, `<`, `<=`, `>`, `>=`); evalúa a `bool`."""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Arith:
    """Operación aritmética binaria (`+`, `-`, `*`, `/`)."""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class BoolOp:
    """Conector booleano binario (`and`, `or`), con cortocircuito en evaluación."""

    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Not:
    """Negación booleana (`not`)."""

    operand: Node


@dataclass(frozen=True, slots=True)
class Neg:
    """Negación aritmética unaria (`-x`) que no se pudo plegar a un literal."""

    operand: Node


Node = Const | Col | ParentCol | Ref | Call | Compare | Arith | BoolOp | Not | Neg
"""Cualquier nodo del AST del mini-DSL."""


@dataclass(frozen=True, slots=True)
class Rule:
    """Una regla ya parseada: su AST (`root`) y el texto original (`text`).

    `text` se conserva solo para los mensajes de error de parseo y evaluación; la
    semántica vive entera en `root`.
    """

    text: str
    root: Node


_COMPARE_OPS: dict[type[exp.Expr], str] = {
    exp.EQ: "=",
    exp.NEQ: "<>",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
"""Nodos de comparación de sqlglot que la gramática admite, a su operador."""

_ARITH_OPS: dict[type[exp.Expr], str] = {
    exp.Add: "+",
    exp.Sub: "-",
    exp.Mul: "*",
    exp.Div: "/",
}
"""Nodos aritméticos admitidos. `Pow` (`^`), `Mod` (`%`) y demás quedan fuera."""

_FLIP_COMPARE: dict[str, str] = {
    "=": "=",
    "<>": "<>",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}
"""Operador equivalente al mover la columna del lado derecho al izquierdo.

`5 < x` ("5 menor que x") es `x > 5`: por eso `<` se voltea a `>`. `=`/`<>` son
simétricos. Se usa en la clasificación para normalizar `expr op col` a
`col op' expr`.
"""


def parse_rule(text: str) -> Rule:
    """Parsea una regla del mini-DSL a un `Rule` con AST propio.

    Args:
        text: La expresión de la regla, tal como llega del YAML (cadena cruda).

    Returns:
        La regla parseada y validada contra la gramática cerrada.

    Raises:
        RuleParseError: Si el texto no parsea como una sola expresión de sqlglot,
            o si contiene cualquier construcción fuera de la gramática (funciones
            no permitidas, subconsultas, subíndices, atributos arbitrarios,
            comentarios, `;`, agregados de grupo, etc.).
    """
    stripped = text.strip()
    if not stripped:
        raise RuleParseError("regla vacía: no hay ninguna expresión que parsear.")
    try:
        parsed = sqlglot.parse(stripped, read=_DIALECT)
    except _SqlglotParseError as err:
        raise RuleParseError(f"la regla {text!r} no es una expresión válida: {err}") from err
    # `parse` (no `parse_one`) para detectar varias sentencias separadas por `;`:
    # `parse_one` se quedaría con la primera en silencio.
    non_empty = [node for node in parsed if node is not None]
    if len(non_empty) != 1:
        raise RuleParseError(
            f"la regla {text!r} debe ser UNA sola expresión; se encontraron "
            f"{len(non_empty)} sentencias (¿sobra un ';'?)."
        )
    node = non_empty[0]
    if _has_comment(node):
        raise RuleParseError(
            f"la regla {text!r} contiene un comentario SQL ('--' o '/* */'); "
            "el mini-DSL no admite comentarios."
        )
    return Rule(text=text, root=_translate(node, text))


def _has_comment(node: exp.Expr) -> bool:
    """`True` si algún nodo del árbol de sqlglot lleva un comentario adjunto."""
    return any(sub.comments for sub in node.walk())


def _translate(node: exp.Expr, text: str) -> Node:
    """Traduce un nodo de sqlglot al AST propio, o lanza `RuleParseError`.

    Lista blanca por tipo de nodo: todo lo que no se reconozca explícitamente cae
    en el `raise` final. Esa es la garantía de seguridad del módulo.
    """
    if isinstance(node, exp.Paren):
        return _translate(node.this, text)
    if isinstance(node, exp.Column):
        return _translate_column(node, text)
    if isinstance(node, exp.Boolean):
        return Const(bool(node.this))
    if isinstance(node, exp.Null):
        return Const(None)
    if isinstance(node, exp.Literal):
        return Const(_literal_value(node))
    if isinstance(node, exp.Neg):
        return _translate_neg(node, text)
    if isinstance(node, exp.Not):
        return Not(_translate(node.this, text))
    if isinstance(node, exp.And):
        return BoolOp("and", _translate(node.this, text), _translate(node.expression, text))
    if isinstance(node, exp.Or):
        return BoolOp("or", _translate(node.this, text), _translate(node.expression, text))
    comparison = _COMPARE_OPS.get(type(node))
    if comparison is not None:
        return Compare(comparison, _translate(node.this, text), _translate(node.expression, text))
    arithmetic = _ARITH_OPS.get(type(node))
    if arithmetic is not None:
        return Arith(arithmetic, _translate(node.this, text), _translate(node.expression, text))
    if isinstance(node, exp.Dot):
        return _translate_parent(node, text)
    if isinstance(node, exp.AggFunc):
        raise RuleParseError(
            f"la regla {text!r} usa un agregado de grupo ({node.sql(dialect=_DIALECT)!r}); "
            "las reglas sobre grupos de filas (sum/count/avg... sobre los hijos de un "
            "padre) son de la v1.0 (`sum_over_group`), no del MVP. Reescríbela como una "
            "regla por fila o espera a la v1.0."
        )
    call = _translate_known_function(node, text)
    if call is not None:
        return call
    if isinstance(node, exp.Anonymous):
        return _translate_anonymous(node, text)
    raise RuleParseError(
        f"la regla {text!r} usa una construcción no soportada por el mini-DSL: "
        f"{node.sql(dialect=_DIALECT)!r} ({type(node).__name__}). Consulta docs/dsl.md "
        "para la gramática admitida."
    )


def _translate_column(node: exp.Column, text: str) -> Col:
    """Traduce una `exp.Column`, rechazando cualquier cualificación (`t.c`, `a.b.c`)."""
    if node.args.get("table") or node.args.get("db") or node.args.get("catalog"):
        raise RuleParseError(
            f"la regla {text!r} referencia {node.sql(dialect=_DIALECT)!r}: el mini-DSL "
            "solo admite columnas de la fila en curso por su nombre desnudo (sin "
            "'tabla.columna' ni accesos encadenados). Para una columna del padre usa "
            "parent(<fk>).<columna>."
        )
    return Col(node.name)


def _translate_neg(node: exp.Neg, text: str) -> Node:
    """`-x`: pliega a un literal negativo cuando el operando es numérico."""
    inner = _translate(node.this, text)
    if (
        isinstance(inner, Const)
        and isinstance(inner.value, int | float)
        and not isinstance(inner.value, bool)
    ):
        return Const(-inner.value)
    return Neg(inner)


def _literal_value(node: exp.Literal) -> object:
    """Valor Python de un `exp.Literal`: cadena tal cual, o `int`/`float`."""
    if node.is_string:
        return str(node.this)
    text = str(node.this)
    try:
        return int(text)
    except ValueError:
        return float(text)


def _translate_parent(node: exp.Dot, text: str) -> ParentCol:
    """Traduce `parent(<fk>).<columna>` a `ParentCol`, o rechaza el `Dot`."""
    anon = node.this
    if not (isinstance(anon, exp.Anonymous) and anon.name.lower() == "parent"):
        raise RuleParseError(
            f"la regla {text!r} usa el acceso {node.sql(dialect=_DIALECT)!r}, que no es "
            "válido: el único acceso con punto admitido es parent(<fk>).<columna>."
        )
    if len(anon.expressions) != 1:
        raise RuleParseError(
            f"la regla {text!r}: parent() toma exactamente un argumento (la columna FK); "
            f"se recibieron {len(anon.expressions)}."
        )
    arg = anon.expressions[0]
    if not (isinstance(arg, exp.Column) and not _is_qualified(arg)):
        raise RuleParseError(
            f"la regla {text!r}: el argumento de parent() debe ser el nombre desnudo de "
            f"una columna FK, no {arg.sql(dialect=_DIALECT)!r}."
        )
    column = node.expression
    if not isinstance(column, exp.Identifier):
        raise RuleParseError(
            f"la regla {text!r}: parent(<fk>) debe ir seguido de '.<columna>', no de "
            f"{column.sql(dialect=_DIALECT)!r}."
        )
    return ParentCol(fk=arg.name, column=column.name)


def _is_qualified(column: exp.Column) -> bool:
    """`True` si la columna lleva prefijo de tabla/esquema/catálogo."""
    return bool(column.args.get("table") or column.args.get("db") or column.args.get("catalog"))


_KNOWN_FUNCTION_SLOTS: dict[type[exp.Expr], tuple[str, frozenset[str]]] = {
    exp.Date: ("date", frozenset({"this", "zone", "expressions"})),
    exp.DateAdd: ("date_add", frozenset({"this", "expression"})),
    exp.Round: ("round", frozenset({"this", "decimals"})),
    exp.Length: ("len", frozenset({"this"})),
}
"""Funciones que sqlglot tipa como nodo propio, con los slots de argumento que la
gramática admite. Cualquier otro slot con valor (p. ej. `round`→`truncate`,
`len`→`binary`, `date_add`→`unit`) es una sintaxis extra que se rechaza: sqlglot no
descarta esos argumentos, los guarda en un slot con nombre, y dejarlos pasar sería
ignorar en silencio parte de la regla (CLAUDE.md)."""


def _translate_known_function(node: exp.Expr, text: str) -> Call | None:
    """Traduce las funciones que sqlglot tipa como nodos propios (date, round...).

    sqlglot no deja estas funciones como `Anonymous`, sino que las reconoce y les
    da un nodo con nombre (`exp.Date`, `exp.DateAdd`, `exp.Round`, `exp.Length`) con
    slots de argumento fijos. Se normalizan al mismo `Call` que las anónimas para
    que la aridad y la pertenencia a la lista blanca se validen en un único sitio
    (`_make_call`), tras rechazar cualquier slot de argumento ajeno a la gramática.
    Devuelve `None` si el nodo no es una de estas funciones conocidas.
    """
    entry = _KNOWN_FUNCTION_SLOTS.get(type(node))
    if entry is None:
        return None
    name, allowed = entry
    _reject_extra_slots(node, allowed, name, text)
    args = [node.args.get("this"), node.args.get("zone"), *node.args.get("expressions", [])]
    if isinstance(node, exp.DateAdd):
        args = [node.args.get("this"), node.args.get("expression")]
    elif isinstance(node, exp.Round):
        decimals = node.args.get("decimals")
        args = [node.this] if decimals is None else [node.this, decimals]
    elif isinstance(node, exp.Length):
        args = [node.this]
    return _make_call(name, [a for a in args if a is not None], text)


def _reject_extra_slots(node: exp.Expr, allowed: frozenset[str], name: str, text: str) -> None:
    """Rechaza la regla si el nodo de función lleva un slot de argumento no admitido."""
    extra = sorted(key for key, value in node.args.items() if key not in allowed and value)
    if extra:
        raise RuleParseError(
            f"la regla {text!r}: la función '{name}' recibió sintaxis extra no soportada "
            f"({', '.join(extra)}); revisa el número y la forma de sus argumentos en "
            "docs/dsl.md."
        )


def _translate_anonymous(node: exp.Anonymous, text: str) -> Node:
    """Traduce una función anónima de sqlglot (`ref`, `noise`, `years_between`...)."""
    name = node.name.lower()
    if name == "parent":
        raise RuleParseError(
            f"la regla {text!r}: parent() solo tiene sentido accediendo a una columna "
            "del padre, como parent(<fk>).<columna>."
        )
    if name == "ref":
        return _translate_ref(node, text)
    return _make_call(name, list(node.expressions), text)


def _translate_ref(node: exp.Anonymous, text: str) -> Ref:
    """Traduce `ref('<nombre>')` a `Ref`, exigiendo un único argumento de cadena."""
    args = node.expressions
    if len(args) != 1 or not (isinstance(args[0], exp.Literal) and args[0].is_string):
        raise RuleParseError(
            f"la regla {text!r}: ref() toma exactamente una constante de cadena con el "
            "nombre de la referencia, p. ej. ref('precio_m2_base')."
        )
    return Ref(str(args[0].this))


def _make_call(name: str, arg_nodes: list[exp.Expr], text: str) -> Call:
    """Valida nombre y aridad contra la lista blanca y construye el `Call`.

    La lista blanca (`FUNCTIONS`) se importa aquí, de forma perezosa, para romper
    el ciclo de importación con `eval.py` (que importa el AST de este módulo).
    """
    from synthdb.rules.eval import FUNCTIONS

    spec = FUNCTIONS.get(name)
    if spec is None:
        allowed = ", ".join(sorted(FUNCTIONS))
        raise RuleParseError(
            f"la regla {text!r} llama a la función '{name}(...)', que no está en la lista "
            f"blanca del mini-DSL. Funciones permitidas: {allowed}."
        )
    if not spec.min_args <= len(arg_nodes) <= spec.max_args:
        expected = (
            f"{spec.min_args}"
            if spec.min_args == spec.max_args
            else f"entre {spec.min_args} y {spec.max_args}"
        )
        raise RuleParseError(
            f"la regla {text!r}: la función '{name}' espera {expected} argumento(s), "
            f"se recibieron {len(arg_nodes)}."
        )
    return Call(name, tuple(_translate(arg, text) for arg in arg_nodes))


# --- Clasificación y dependencias -----------------------------------------


def clasify_rule(rule: Rule) -> RuleKind:
    """Clasifica una regla en `bound`, `derivation` o `assertion` (§7.2).

    Solo mira la forma del AST; no evalúa nada. La clasificación determina el uso
    *adicional* de la regla (cota del generador o derivación de la columna); la
    validación como aserción se aplica siempre, con independencia del kind.

    Args:
        rule: La regla ya parseada.

    Returns:
        El `RuleKind` de la regla.
    """
    isolated = _isolated_column(rule.root)
    if isolated is None:
        return "assertion"
    _, op, _ = isolated
    if op == "=":
        return "derivation"
    if op in ("<", "<=", ">", ">="):
        return "bound"
    return "assertion"


def _isolated_column(node: Node) -> tuple[str, str, Node] | None:
    """Si `node` es `col <op> expr` con la columna despejada, devuelve el desglose.

    "Despejada" significa que uno de los dos lados es una columna local desnuda y
    el otro lado es una expresión que NO la referencia (así el motor puede evaluar
    esa expresión antes de generar la columna). Normaliza el resultado a la forma
    `col op expr`, volteando el operador si la columna estaba a la derecha.

    Returns:
        `(columna, operador_normalizado, expresión_del_otro_lado)`, o `None` si la
        regla no tiene esa forma (p. ej. ambos lados son expresiones compuestas, o
        la columna aparece en los dos lados).
    """
    if not isinstance(node, Compare):
        return None
    left, right = node.left, node.right
    if isinstance(left, Col) and left.name not in referenced_columns(right):
        return (left.name, node.op, right)
    if isinstance(right, Col) and right.name not in referenced_columns(left):
        return (right.name, _FLIP_COMPARE[node.op], left)
    return None


def referenced_columns(node: Node) -> frozenset[str]:
    """Columnas LOCALES de la fila que `node` lee (ignora `parent()` y `ref()`).

    Se usa para el grafo de dependencias intra-fila: una columna derivada o acotada
    debe generarse después de todas las columnas locales que su expresión lee. Las
    referencias al padre (`ParentCol`) y las constantes (`Ref`) no son columnas de
    la fila, así que no cuentan como dependencias de orden.
    """
    if isinstance(node, Col):
        return frozenset({node.name})
    if isinstance(node, Const | ParentCol | Ref):
        return frozenset()
    if isinstance(node, Call):
        columns: frozenset[str] = frozenset()
        for arg in node.args:
            columns |= referenced_columns(arg)
        return columns
    if isinstance(node, Compare | Arith | BoolOp):
        return referenced_columns(node.left) | referenced_columns(node.right)
    if isinstance(node, Not | Neg):
        return referenced_columns(node.operand)
    return frozenset()


def rule_dependencies(rule: Rule) -> tuple[str, frozenset[str]] | None:
    """Dependencia de orden que una regla impone: `(columna_destino, columnas_leídas)`.

    Solo las reglas `bound` y `derivation` imponen orden: la columna que acotan o
    derivan debe generarse después de las columnas locales que su expresión lee.
    Las aserciones se comprueban tras generar toda la fila, así que no imponen
    orden y devuelven `None`.

    Returns:
        `(destino, leídas)` para `bound`/`derivation`; `None` para `assertion`.
    """
    isolated = _isolated_column(rule.root)
    if isolated is None:
        return None
    column, op, expr = isolated
    if op == "=" or op in ("<", "<=", ">", ">="):
        return (column, referenced_columns(expr))
    return None


@dataclass(frozen=True, slots=True)
class Bound:
    """Uso de una regla `bound` como cota de un generador (§7.2).

    `expr` es la expresión del otro lado de la desigualdad, envuelta en su propio
    `Rule` para que el motor la evalúe con `eval.evaluate(bound.expr, ctx)` sobre la
    fila en curso y obtenga el valor de la cota.
    """

    column: str
    side: Literal["lower", "upper"]
    exclusive: bool
    expr: Rule


@dataclass(frozen=True, slots=True)
class Derivation:
    """Uso de una regla `derivation`: la columna `column` se calcula evaluando `expr`."""

    column: str
    expr: Rule


_BOUND_SIDE: dict[str, tuple[Literal["lower", "upper"], bool]] = {
    ">=": ("lower", False),
    ">": ("lower", True),
    "<=": ("upper", False),
    "<": ("upper", True),
}
"""Operador normalizado (`col op expr`) a `(lado de la cota, exclusiva)`.

`col >= expr` dice que `expr` es la cota INFERIOR (inclusiva) de la columna; `col <
expr`, la cota SUPERIOR exclusiva; etc.
"""


def as_bound(rule: Rule) -> Bound | None:
    """Interpreta `rule` como cota de generador, o `None` si no es un `bound`."""
    isolated = _isolated_column(rule.root)
    if isolated is None:
        return None
    column, op, expr = isolated
    side = _BOUND_SIDE.get(op)
    if side is None:
        return None
    kind, exclusive = side
    return Bound(
        column=column, side=kind, exclusive=exclusive, expr=Rule(text=rule.text, root=expr)
    )


def as_derivation(rule: Rule) -> Derivation | None:
    """Interpreta `rule` como derivación `col = expr`, o `None` si no lo es."""
    isolated = _isolated_column(rule.root)
    if isolated is None or isolated[1] != "=":
        return None
    column, _, expr = isolated
    return Derivation(column=column, expr=Rule(text=rule.text, root=expr))
