"""Generadores numéricos: `numeric_range` y `sequence` (T2.3).

Solo se usa `random` de la biblioteca estándar (CLAUDE.md, especificacion.md
§7.3): NumPy es un extra opcional post-MVP. Las distribuciones son deterministas
porque toda su aleatoriedad sale del `Random` de fila (`ctx.rng`), nunca de
`random` global.
"""

from __future__ import annotations

import bisect
import math
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any

from pydantic import Field, model_validator

from synthdb.generation.generators.base import GenContext, GeneratorParams, register
from synthdb.generation.generators.distributions import (
    DistributionSpec,
    LognormalParams,
    NormalParams,
    ZipfParams,
)
from synthdb.generation.numeric_bounds import (
    as_decimal,
    quantize_to_scale,
    representable_limit,
    scale_step,
)

_INT_BITS_BOUNDS: dict[int, tuple[int, int]] = {
    16: (-(2**15), 2**15 - 1),
    32: (-(2**31), 2**31 - 1),
    64: (-(2**63), 2**63 - 1),
}
"""Cota implícita de un entero por su ancho en bits, cuando no hay otra más
estrecha (especificacion.md §5; `TypeSpec.bits`)."""

_DEFAULT_INT_BITS = 32
"""Ancho asumido si `TypeSpec.bits` es `None` (entero sin ancho declarado)."""

_BOUNDED_SAMPLE_TRIES = 100
"""Reintentos de muestreo por rechazo antes de recortar al rango (normal/lognormal)."""

_ZIPF_MAX_RANKS = 100_000
"""Techo de categorías distintas de zipf: su cola es despreciable más allá, y
acota el coste de construir la CDF sobre rangos enteros enormes."""


def _local_decimal_context(*values: Decimal) -> Context:
    """Construye un contexto local suficiente para operar con `values` exactos."""
    required = 0
    for value in values:
        if not value.is_finite():
            continue
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            continue
        integer_digits = max(value.adjusted() + 1, 1) if value else 1
        required += integer_digits + max(-exponent, 0)
    return Context(prec=max(required + 8, 28))


class NumericRangeParams(GeneratorParams):
    """Parámetros de `numeric_range`.

    `min`/`max` son las cotas del rango; si faltan, para una columna entera se
    usan los bits del tipo como cota implícita, y para una numérica el rango
    `[0, 1]`. `round_to` es el *paso* de redondeo (1 ⇒ entero, 0.01 ⇒ dos
    decimales). `distribution` es la forma anidada `{family, params}`
    (`DistributionSpec`): los parámetros de cada familia se validan contra su
    propio modelo, y un campo ajeno a la familia es un error de campo exacto, no
    se ignora.
    """

    min: float | None = None
    max: float | None = None
    min_exclusive: bool = False
    max_exclusive: bool = False
    distribution: DistributionSpec = Field(default_factory=DistributionSpec)
    round_to: float | None = Field(default=None, description="Paso de redondeo (1 = entero).")

    @model_validator(mode="after")
    def _validate(self) -> NumericRangeParams:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("numeric_range: 'min' no puede ser mayor que 'max'.")
        if self.round_to is not None and self.round_to <= 0:
            raise ValueError("numeric_range: 'round_to' debe ser > 0.")
        return self


def _bit_bounds(bits: int | None) -> tuple[int, int]:
    """Cota entera implícita para un ancho en bits (def: 32 si es `None`)."""
    return _INT_BITS_BOUNDS.get(bits or _DEFAULT_INT_BITS, _INT_BITS_BOUNDS[_DEFAULT_INT_BITS])


class NumericRangeGenerator:
    """Genera un número en un rango con una distribución dada (T2.3)."""

    def __init__(self, params: NumericRangeParams) -> None:
        self._p = params
        self._zipf_cdf: list[float] | None = None
        self._zipf_key: tuple[int, int] | None = None

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve un `int` si la columna es entera, si no un `float`."""
        if ctx.column.type.kind == "integer":
            lo, hi = self._int_bounds(ctx)
            if lo > hi:
                raise ValueError(
                    f"numeric_range: rango entero vacío en {ctx.table}.{ctx.column.name} "
                    f"(min>max tras aplicar exclusividades y la cota del tipo)."
                )
            sample = self._quantize(self._sample(ctx, float(lo), float(hi)), self._p.round_to)
            return max(lo, min(hi, int(round(sample))))
        return self._generate_numeric(ctx)

    def _generate_numeric(self, ctx: GenContext) -> float:
        """Rama `numeric`: respeta `NUMERIC(precision, scale)` cuando el tipo lo declara.

        Recorta el rango al representable por el tipo y redondea el resultado a la
        escala, con el mismo criterio exacto que `validation.structural`. Un
        `numeric` sin precisión declarada (p. ej. `double precision`) se comporta
        como hasta ahora: rango por defecto `[0, 1]` y `float` sin cuantizar.
        """
        type_spec = ctx.column.type
        if type_spec.precision is None:
            lo, hi = self._float_bounds()
            step = self._p.round_to
            if lo > hi:
                raise ValueError(
                    f"numeric_range: rango vacío en {ctx.table}.{ctx.column.name} "
                    f"(min>max tras recortar al rango representable del tipo)."
                )
            x = max(lo, min(hi, self._quantize(self._sample(ctx, lo, hi), step)))
            return self._apply_float_exclusivity(x, lo, hi, step)

        # Las cotas del tipo y el paso natural se mantienen como Decimal hasta
        # terminar la semántica NUMERIC. Convertir scale_step a float hace que
        # NUMERIC(1000, 500) use 0.0 y provoque DivisionByZero en _quantize.
        limit = representable_limit(type_spec.precision, type_spec.scale)
        negative_limit = limit.copy_negate()
        decimal_lo = as_decimal(self._p.min) if self._p.min is not None else Decimal("0")
        decimal_hi = as_decimal(self._p.max) if self._p.max is not None else Decimal("1")
        decimal_lo = max(decimal_lo, negative_limit)
        decimal_hi = min(decimal_hi, limit)
        if decimal_lo > decimal_hi:
            raise ValueError(
                f"numeric_range: rango vacío en {ctx.table}.{ctx.column.name} "
                f"(min>max tras recortar al rango representable del tipo)."
            )

        sampled = as_decimal(self._sample(ctx, float(decimal_lo), float(decimal_hi)))
        explicit_step = as_decimal(self._p.round_to) if self._p.round_to is not None else None
        if explicit_step is None:
            decimal_step = scale_step(type_spec.scale)
            decimal_x = quantize_to_scale(sampled, type_spec.scale)
        else:
            decimal_step = explicit_step
            decimal_x = as_decimal(self._quantize(float(sampled), self._p.round_to))
        decimal_x = max(decimal_lo, min(decimal_hi, decimal_x))
        if self._p.min_exclusive and decimal_x <= decimal_lo:
            decimal_x = _local_decimal_context(decimal_lo, decimal_step).add(
                decimal_lo, decimal_step
            )
        if self._p.max_exclusive and decimal_x >= decimal_hi:
            decimal_x = _local_decimal_context(decimal_hi, decimal_step).subtract(
                decimal_hi, decimal_step
            )
        decimal_x = max(decimal_lo, min(decimal_hi, decimal_x))

        # El contrato público del generador sigue siendo float; la conversión es
        # deliberadamente el último paso, después de cuantizar a la escala exacta.
        return float(quantize_to_scale(decimal_x, type_spec.scale))

    def _int_bounds(self, ctx: GenContext) -> tuple[int, int]:
        bit_lo, bit_hi = _bit_bounds(ctx.column.type.bits)
        raw_lo = self._p.min if self._p.min is not None else float(bit_lo)
        raw_hi = self._p.max if self._p.max is not None else float(bit_hi)
        lo = math.ceil(raw_lo)
        hi = math.floor(raw_hi)
        if self._p.min_exclusive and lo == raw_lo:
            lo += 1
        if self._p.max_exclusive and hi == raw_hi:
            hi -= 1
        return max(lo, bit_lo), min(hi, bit_hi)

    def _float_bounds(self) -> tuple[float, float]:
        lo = self._p.min if self._p.min is not None else 0.0
        hi = self._p.max if self._p.max is not None else 1.0
        return lo, hi

    def _quantize(self, x: float, step: float | None) -> float:
        """Redondea `x` al múltiplo de `step` con aritmética exacta de `Decimal`.

        Usar `Decimal` (no ``round(x / step) * step`` en coma flotante) evita el
        ruido binario que dejaría 0.37 como 0.37000000000000005 (CLAUDE.md). Los
        empates se alejan de cero, como PostgreSQL al insertar en `NUMERIC`
        (`numeric_bounds.quantize_to_scale` usa el mismo criterio).
        """
        if step is None:
            return x
        value = Decimal(str(x))
        s = step if isinstance(step, Decimal) else Decimal(str(step))
        if not s:
            raise ValueError("numeric_range: el paso de cuantización no puede ser 0.")
        ctx = _local_decimal_context(value, s)
        quotient = ctx.divide(value, s)
        integral = quotient.to_integral_value(rounding=ROUND_HALF_UP, context=ctx)
        return float(ctx.multiply(integral, s))

    def _apply_float_exclusivity(self, x: float, lo: float, hi: float, step: float | None) -> float:
        effective = step if step else max((hi - lo) * 1e-9, 1e-9)
        if self._p.min_exclusive and x <= lo:
            x = lo + effective
        if self._p.max_exclusive and x >= hi:
            x = hi - effective
        return max(lo, min(hi, x))

    def _sample(self, ctx: GenContext, lo: float, hi: float) -> float:
        if hi <= lo:
            return lo
        params = self._p.distribution.params
        if isinstance(params, NormalParams):
            return self._sample_normal(ctx, lo, hi, params)
        if isinstance(params, LognormalParams):
            return self._sample_lognormal(ctx, lo, hi, params)
        if isinstance(params, ZipfParams):
            return float(self._sample_zipf(ctx, lo, hi, params))
        return ctx.rng.uniform(lo, hi)  # UniformParams

    def _sample_normal(self, ctx: GenContext, lo: float, hi: float, p: NormalParams) -> float:
        mean = p.mean if p.mean is not None else (lo + hi) / 2
        std = p.std if p.std is not None else (hi - lo) / 6
        if std <= 0:
            return mean
        for _ in range(_BOUNDED_SAMPLE_TRIES):
            x = ctx.rng.gauss(mean, std)
            if lo <= x <= hi:
                return x
        return max(lo, min(hi, ctx.rng.gauss(mean, std)))

    def _sample_lognormal(self, ctx: GenContext, lo: float, hi: float, p: LognormalParams) -> float:
        median = p.median if p.median is not None else max((lo + hi) / 2, 1.0)
        if median <= 0:
            median = max(hi, 1.0)
        mu = math.log(median)
        sigma = p.sigma if p.sigma is not None else 0.5
        for _ in range(_BOUNDED_SAMPLE_TRIES):
            x = ctx.rng.lognormvariate(mu, sigma)
            if lo <= x <= hi:
                return x
        return max(lo, min(hi, ctx.rng.lognormvariate(mu, sigma)))

    def _sample_zipf(self, ctx: GenContext, lo: float, hi: float, p: ZipfParams) -> int:
        lo_i, hi_i = math.ceil(lo), math.floor(hi)
        if hi_i <= lo_i:
            return lo_i
        key = (lo_i, hi_i)
        cdf = self._zipf_cdf
        if self._zipf_key != key or cdf is None:
            cdf = self._build_zipf_cdf(lo_i, hi_i, p.s)
            self._zipf_cdf = cdf
            self._zipf_key = key
        idx = min(bisect.bisect_left(cdf, ctx.rng.random()), len(cdf) - 1)
        return lo_i + idx

    def _build_zipf_cdf(self, lo_i: int, hi_i: int, s: float) -> list[float]:
        n = min(hi_i - lo_i + 1, _ZIPF_MAX_RANKS)
        weights = [1.0 / (k**s) for k in range(1, n + 1)]
        total = math.fsum(weights)
        acc = 0.0
        cdf: list[float] = []
        for w in weights:
            acc += w
            cdf.append(acc / total)
        return cdf


class SequenceParams(GeneratorParams):
    """Parámetros de `sequence`: valor de arranque y paso."""

    start: int = 1
    step: int = 1

    @model_validator(mode="after")
    def _validate(self) -> SequenceParams:
        if self.step == 0:
            raise ValueError("sequence: 'step' no puede ser 0.")
        return self


class SequenceGenerator:
    """Secuencia aritmética `start, start+step, start+2·step, ...` (T2.3).

    No usa `ctx.rng`: es determinista por construcción. Un contador interno
    avanza en cada llamada, así que la secuencia sigue el ORDEN de llamada. El
    motor genera las filas en orden de índice, luego el resultado es estable e
    independiente del tamaño de lote (el lote no reordena las filas).
    """

    def __init__(self, params: SequenceParams) -> None:
        self._start = params.start
        self._step = params.step
        self._i = 0

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve el siguiente término de la secuencia (ignora `ctx.rng`)."""
        value = self._start + self._i * self._step
        self._i += 1
        return value


register("numeric_range", NumericRangeParams, NumericRangeGenerator)
register("sequence", SequenceParams, SequenceGenerator)
