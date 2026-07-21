"""Semántica de `NUMERIC(precision, scale)`: rango representable y cuantización.

PostgreSQL almacena un `NUMERIC(p, s)` con como mucho `p` dígitos
significativos y exactamente `s` decimales: **redondea a la escala** al insertar
y **rechaza** («numeric field overflow») cuando la parte entera no cabe. Este
módulo reproduce esa semántica con aritmética EXACTA de `Decimal`/entero —nunca
floats para contar dígitos decimales, que arrastran errores de representación
binaria (CLAUDE.md)— y la comparten el motor (generación y compilación) y la
validación estructural, de modo que ambas usen el mismo criterio.

El motor sigue produciendo valores numéricos como `float`; estas funciones solo
deciden cotas y redondeo, no cambian el tipo Python del valor generado.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any


def effective_scale(scale: int | None) -> int:
    """Escala efectiva: `NUMERIC(p)` sin escala equivale a `NUMERIC(p, 0)`."""
    return scale if scale is not None else 0


def representable_limit(precision: int, scale: int | None) -> Decimal:
    """Mayor magnitud representable en `NUMERIC(precision, scale)`.

    Es ``(10**precision - 1) / 10**scale``: `NUMERIC(3, 2)` ⇒ ``9.99``,
    `NUMERIC(5, 0)` ⇒ ``99999``. El menor representable es su negado.

    Args:
        precision: Dígitos significativos totales declarados por el tipo.
        scale: Decimales declarados; `None` se interpreta como ``0``.

    Returns:
        El máximo positivo representable como `Decimal` exacto.
    """
    return (Decimal(10) ** precision - 1).scaleb(-effective_scale(scale))


def scale_step(scale: int | None) -> Decimal:
    """Paso de la escala: ``10**-scale`` (`NUMERIC(_, 2)` ⇒ ``0.01``)."""
    return Decimal(1).scaleb(-effective_scale(scale))


def as_decimal(value: Any) -> Decimal:
    """Convierte a `Decimal` SIN ruido binario: un `float` pasa por su ``str``.

    ``Decimal(str(0.1))`` es ``Decimal('0.1')`` (no ``0.1000...0555``), que es lo
    que hace falta para contar decimales de forma fiable.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # defensivo: un bool no es un numérico válido
        raise TypeError("un bool no es un valor numérico")
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def quantize_to_scale(value: Any, scale: int | None) -> Decimal:
    """Redondea `value` a la escala (medio-a-par), como PostgreSQL al insertar."""
    return as_decimal(value).quantize(scale_step(scale), rounding=ROUND_HALF_EVEN)


def fits(value: Any, precision: int, scale: int | None) -> bool:
    """`True` si `value`, ya redondeado a la escala, cabe en `NUMERIC(p, s)`.

    Reproduce el orden de PostgreSQL: primero redondea a la escala (un exceso de
    decimales NO es error, se redondea) y después comprueba que la magnitud no
    desborda la precisión.
    """
    rounded = quantize_to_scale(value, scale)
    limit = representable_limit(precision, scale)
    return -limit <= rounded <= limit
