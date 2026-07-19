"""Generador `derived`: calcula una columna a partir de una regla del mini-DSL (T2.10).

Es el generador que materializa una regla de *derivación* (§7.2): su parámetro
`expression` es el lado derecho de un `col = expresión` (p. ej. `superficie *
ref('m2') * noise(0.2)`), y en cada fila devuelve el valor de esa expresión
evaluada sobre el `RowContext`. Por eso necesita un `RowContext` (con `row`,
`parent()` y `refs`), no el `GenContext` plano que basta a los generadores base:
lo estrecha con un `isinstance` y, si el motor no se lo diera, falla con un mensaje
claro en vez de producir un valor incorrecto en silencio.

La expresión se parsea UNA vez al construir el generador (errores de gramática ⇒
`RuleParseError` al compilar el plan, no a mitad de generación) y se reevalúa por
fila. Se registra en el catálogo como uno más: `resolve(GeneratorSpec(type=
"derived", params={"expression": ...}))`.
"""

from __future__ import annotations

from typing import Any

from synthdb.generation.generators.base import GenContext, GeneratorParams, register
from synthdb.rules.dsl import parse_rule
from synthdb.rules.eval import evaluate


class DerivedParams(GeneratorParams):
    """Parámetros de `derived`: la expresión (lado derecho de la derivación)."""

    expression: str


class DerivedGenerator:
    """Evalúa la expresión de derivación de una columna, fila a fila (T2.10)."""

    def __init__(self, params: DerivedParams) -> None:
        """Parsea la expresión una vez; un error de gramática aflora aquí."""
        self._rule = parse_rule(params.expression)

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve el valor derivado para la fila en curso.

        Args:
            ctx: Debe ser un `RowContext` (el motor lo construye con los valores ya
                generados de la fila, el padre y las constantes). Un `GenContext`
                plano no basta: la expresión puede leer `row`/`parent`/`refs`.

        Returns:
            El valor de la expresión evaluada sobre la fila.

        Raises:
            TypeError: Si `ctx` no es un `RowContext` (error del motor, no del usuario).
            RuleEvalError: Si la expresión falla al evaluar (columna ausente, `ref`
                desconocida, división entre cero, tipos incompatibles).
        """
        # Import perezoso: `context` importa el paquete de generadores, así que un
        # import en cabecera cerraría un ciclo (generators → derived → context →
        # generators). En tiempo de generación todos los módulos ya están cargados.
        from synthdb.generation.context import RowContext

        if not isinstance(ctx, RowContext):
            raise TypeError(
                "el generador 'derived' requiere un RowContext (con row/parent/refs); "
                f"recibió un {type(ctx).__name__}. El motor debe construir el RowContext "
                "de la fila antes de generar una columna derivada."
            )
        return evaluate(self._rule, ctx)


register("derived", DerivedParams, DerivedGenerator)
