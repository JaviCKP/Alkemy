"""Semántica de `NUMERIC(precision, scale)`: rango representable y cuantización.

PostgreSQL almacena un `NUMERIC(p, s)` con como mucho `p` dígitos
significativos y exactamente `s` decimales: **redondea a la escala** al insertar
(empates alejándose de cero, no medio-a-par) y **rechaza** («numeric field
overflow») cuando la parte entera no cabe. Este módulo reproduce esa semántica
con aritmética EXACTA de `Decimal`/entero —nunca floats para contar dígitos
decimales, que arrastran errores de representación binaria (CLAUDE.md)— y la
comparten el motor (generación y compilación) y la validación estructural, de
modo que las tres usen el mismo criterio.

Cada función que hace aritmética `Decimal` de precisión variable construye su
propio `decimal.Context` local, dimensionado por la precisión/escala en juego
(nunca por `decimal.getcontext()`, que el proceso puede compartir con otro
código) y lo pasa explícitamente a cada operación. Es la única forma de que
`NUMERIC(1000, 0)` funcione exacto: la precisión ambiente por defecto es de
solo 28 dígitos, así que operar sin un contexto explícito trunca en silencio
(`scaleb`) o revienta con `InvalidOperation` (`quantize`) mucho antes de
llegar a 1000 dígitos. CLAUDE.md prohíbe además tocar el contexto global, así
que nunca se llama a `decimal.setcontext`/`getcontext().prec = …`.

El motor sigue produciendo valores numéricos como `float`; estas funciones solo
deciden cotas y redondeo, no cambian el tipo Python del valor generado.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal
from typing import Any

_PRECISION_HEADROOM = 12
"""Dígitos de margen sobre el mínimo estrictamente necesario.

Cubre el acarreo de un redondeo que añade un dígito (9.995 → 10.00) y el +1/-1
de `has_quantized_value` al descartar un extremo excluido; nunca se opera tan
al límite del contexto como para necesitar más.
"""


def effective_scale(scale: int | None) -> int:
    """Escala efectiva: `NUMERIC(p)` sin escala equivale a `NUMERIC(p, 0)`."""
    return scale if scale is not None else 0


def _local_context(*digit_counts: int) -> Context:
    """`Context` LOCAL con precisión suficiente para `digit_counts`, nunca el global.

    Sumar los requisitos (en vez de tomar el máximo) sobredimensiona a
    propósito: es una cota segura y barata incluso cuando dos magnitudes
    independientes (p. ej. dígitos del valor y escala destino) contribuyen
    ambas al tamaño del resultado, como en `quantize`.
    """
    needed = sum(max(count, 0) for count in digit_counts)
    return Context(prec=max(needed, 1) + _PRECISION_HEADROOM)


def _integer_digit_count(value: Decimal) -> int:
    """Dígitos ENTEROS de `value` (parte antes de la coma), mínimo 1.

    ``len(value.as_tuple().digits)`` cuenta los dígitos ALMACENADOS del
    coeficiente, que puede ser engañosamente corto si `value` tiene un
    exponente grande: ``Decimal('2E+999')`` solo almacena un dígito, pero
    cuantizarlo a escala 0 necesita 1000 (el 2 seguido de 999 ceros). El
    exponente ajustado (`adjusted()`, la posición del dígito más
    significativo respecto de la coma) sí refleja la magnitud real y es un
    cálculo puro, sin contexto: `value.adjusted() + 1` es exactamente ese
    recuento de dígitos enteros.
    """
    if value == 0:
        return 1
    return max(value.adjusted() + 1, 1)


def representable_limit(precision: int, scale: int | None) -> Decimal:
    """Mayor magnitud representable en `NUMERIC(precision, scale)`.

    Es ``(10**precision - 1) / 10**scale``: `NUMERIC(3, 2)` ⇒ ``9.99``,
    `NUMERIC(5, 0)` ⇒ ``99999``. El menor representable es su negado.

    El coeficiente ``10**precision - 1`` se calcula con enteros nativos de
    Python (exactos, sin límite de dígitos) y solo se convierte a `Decimal`
    ya calculado: construir un `Decimal` desde un `int` es siempre exacto
    (no pasa por el contexto), así que ni siquiera `NUMERIC(1000, 0)` arriesga
    perder dígitos aquí. El único paso que sí necesita contexto es desplazar
    la coma (`scaleb`); se le da uno local dimensionado por `precision`.

    Args:
        precision: Dígitos significativos totales declarados por el tipo.
        scale: Decimales declarados; `None` se interpreta como ``0``.

    Returns:
        El máximo positivo representable como `Decimal` exacto.
    """
    scale_value = effective_scale(scale)
    coefficient = Decimal(10**precision - 1)
    return coefficient.scaleb(-scale_value, context=_local_context(precision))


def scale_step(scale: int | None) -> Decimal:
    """Paso de la escala: ``10**-scale`` (`NUMERIC(_, 2)` ⇒ ``0.01``)."""
    scale_value = effective_scale(scale)
    return Decimal(1).scaleb(-scale_value, context=_local_context(1, scale_value))


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
    """Redondea `value` a la escala, como PostgreSQL al insertar en `NUMERIC`.

    Los empates se alejan de cero (``9.995`` ⇒ ``10.00``, ``-9.995`` ⇒
    ``-10.00``): PostgreSQL no usa medio-a-par para `NUMERIC`. El contexto
    local se dimensiona por los dígitos ENTEROS de `value` más la escala
    destino (no basta con el mayor de los dos: cuantizar `5` a escala 1000
    produce 1001 dígitos significativos, uno más que cualquiera de los dos
    por separado), así que nunca depende de la precisión ambiente ni revienta
    con `InvalidOperation` ni para un valor con exponente grande ni para una
    escala grande.
    """
    decimal_value = as_decimal(value)
    scale_value = effective_scale(scale)
    ctx = _local_context(_integer_digit_count(decimal_value), scale_value)
    return decimal_value.quantize(scale_step(scale), rounding=ROUND_HALF_UP, context=ctx)


def fits(value: Any, precision: int, scale: int | None) -> bool:
    """`True` si `value`, ya redondeado a la escala, cabe en `NUMERIC(p, s)`.

    Reproduce el orden de PostgreSQL: primero redondea a la escala (un exceso de
    decimales NO es error, se redondea) y después comprueba que la magnitud no
    desborda la precisión.
    """
    rounded = quantize_to_scale(value, scale)
    limit = representable_limit(precision, scale)
    return limit.copy_negate() <= rounded <= limit


def has_quantized_value(
    precision: int,
    scale: int | None,
    *,
    low: Any = None,
    high: Any = None,
    min_exclusive: bool = False,
    max_exclusive: bool = False,
) -> bool:
    """`True` si algún múltiplo de la escala de `NUMERIC(precision, scale)` cae en `[low, high]`.

    No basta con comprobar que el intervalo *real* `[low, high]` se solapa con
    la ventana representable del tipo: la escala impone una rejilla (múltiplos
    de `scale_step`), así que un intervalo no vacío puede carecer de cualquier
    valor cuantizable. P. ej. `(1.001, 1.004)` con escala 2 no contiene ningún
    múltiplo de 0.01 aunque `1.001 <= 1.004`. Del mismo modo, un extremo
    exclusivo que coincide exactamente con un punto de la rejilla lo excluye
    (`min=9.99, min_exclusive=True` en `NUMERIC(3,2)` no admite `9.99`, y
    `9.99` es también el máximo representable, así que no queda nada).

    `low`/`high` ausentes son la ventana completa del tipo (``±representable_limit``,
    siempre inclusiva). Cuando la ventana del tipo es más estricta que el
    límite pedido, manda ella —de forma inclusiva, nunca hereda la
    exclusividad del límite pedido que sustituye—; en un empate exacto ambas
    cotas coinciden y la exclusividad pedida por el usuario se conserva (la
    intersección de "x ≥ L" con "x > L" es "x > L").

    Args:
        precision: Dígitos significativos totales del tipo.
        scale: Decimales declarados del tipo; `None` equivale a ``0``.
        low: Cota inferior pedida, o `None` para "sin cota" (⇒ el mínimo del tipo).
        high: Cota superior pedida, o `None` para "sin cota" (⇒ el máximo del tipo).
        min_exclusive: `True` si `low` no debe poder alcanzarse exactamente.
        max_exclusive: `True` si `high` no debe poder alcanzarse exactamente.

    Returns:
        `True` si existe al menos un valor representable que satisface el rango
        pedido y sus exclusividades; `False` si el rango es imposible.
    """
    limit = representable_limit(precision, scale)
    negative_limit = limit.copy_negate()

    eff_low = as_decimal(low) if low is not None else negative_limit
    eff_low_exclusive = min_exclusive if low is not None else False
    if eff_low < negative_limit:
        eff_low, eff_low_exclusive = negative_limit, False

    eff_high = as_decimal(high) if high is not None else limit
    eff_high_exclusive = max_exclusive if high is not None else False
    if eff_high > limit:
        eff_high, eff_high_exclusive = limit, False

    if eff_low > eff_high:
        return False

    scale_value = effective_scale(scale)
    ctx = _local_context(_integer_digit_count(eff_low), _integer_digit_count(eff_high), scale_value)
    shifted_low = eff_low.scaleb(scale_value, context=ctx)
    shifted_high = eff_high.scaleb(scale_value, context=ctx)

    n_low = shifted_low.to_integral_value(rounding=ROUND_CEILING, context=ctx)
    if eff_low_exclusive and n_low == shifted_low:
        n_low = ctx.add(n_low, Decimal(1))

    n_high = shifted_high.to_integral_value(rounding=ROUND_FLOOR, context=ctx)
    if eff_high_exclusive and n_high == shifted_high:
        n_high = ctx.subtract(n_high, Decimal(1))

    return n_low <= n_high
