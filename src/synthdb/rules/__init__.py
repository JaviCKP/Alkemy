"""Mini-DSL de reglas: parser con AST propio e intérprete de lista blanca (T2.9).

`dsl.py` parsea (reutilizando el parser de expresiones de sqlglot y traduciendo a
un AST propio) y clasifica; `eval.py` evalúa y comprueba con un intérprete seguro,
sin `eval`/`exec` (CLAUDE.md). Ver `docs/dsl.md` para la gramática.
"""

from synthdb.rules.dsl import (
    Bound,
    Derivation,
    Rule,
    RuleKind,
    RuleParseError,
    as_bound,
    as_derivation,
    clasify_rule,
    parse_rule,
    referenced_columns,
    rule_dependencies,
)
from synthdb.rules.eval import FUNCTIONS, RuleEvalError, check, evaluate

__all__ = [
    "FUNCTIONS",
    "Bound",
    "Derivation",
    "Rule",
    "RuleEvalError",
    "RuleKind",
    "RuleParseError",
    "as_bound",
    "as_derivation",
    "check",
    "clasify_rule",
    "evaluate",
    "parse_rule",
    "referenced_columns",
    "rule_dependencies",
]
