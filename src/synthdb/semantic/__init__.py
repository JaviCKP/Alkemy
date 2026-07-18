"""Motor semántico: heurísticas deterministas y el fusor de fuentes (T2.4/T2.6)."""

from synthdb.semantic.heuristics import HeuristicResult, infer_column, patterns
from synthdb.semantic.merge import PlanError, build_plan

__all__ = [
    "HeuristicResult",
    "PlanError",
    "build_plan",
    "infer_column",
    "patterns",
]
