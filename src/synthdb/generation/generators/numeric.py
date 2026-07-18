"""Generadores numéricos: `numeric_range` y `sequence` (T2.3).

Solo se usa `random` de la biblioteca estándar (CLAUDE.md, especificacion.md
§7.3): NumPy es un extra opcional post-MVP. Las distribuciones son deterministas
porque toda su aleatoriedad sale del `Random` de fila (`ctx.rng`), nunca de
`random` global.
"""

from __future__ import annotations

import bisect
import math
from typing import Any, Literal

from pydantic import Field, model_validator

from synthdb.generation.generators.base import GenContext, GeneratorParams, register

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


class NumericRangeParams(GeneratorParams):
    """Parámetros de `numeric_range`.

    `min`/`max` son las cotas del rango; si faltan, para una columna entera se
    usan los bits del tipo como cota implícita, y para una numérica el rango
    `[0, 1]`. `round_to` es el *paso* de redondeo (1 ⇒ entero, 0.01 ⇒ dos
    decimales). Cada familia de distribución usa sus propios campos opcionales;
    los que sobran para la familia elegida simplemente se ignoran.
    """

    min: float | None = None
    max: float | None = None
    min_exclusive: bool = False
    max_exclusive: bool = False
    distribution: Literal["uniform", "normal", "lognormal", "zipf"] = "uniform"
    mean: float | None = Field(default=None, description="Normal: centro (def: punto medio).")
    std: float | None = Field(default=None, description="Normal: desviación (def: rango/6).")
    median: float | None = Field(default=None, description="Lognormal: mediana (def: punto medio).")
    sigma: float | None = Field(default=None, description="Lognormal: sigma log (def: 0.5).")
    s: float = Field(default=1.2, description="Zipf: exponente; mayor ⇒ más sesgo hacia el mínimo.")
    round_to: float | None = Field(default=None, description="Paso de redondeo (1 = entero).")

    @model_validator(mode="after")
    def _validate(self) -> NumericRangeParams:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("numeric_range: 'min' no puede ser mayor que 'max'.")
        if self.round_to is not None and self.round_to <= 0:
            raise ValueError("numeric_range: 'round_to' debe ser > 0.")
        if self.s <= 0:
            raise ValueError("numeric_range: el exponente zipf 's' debe ser > 0.")
        if self.std is not None and self.std < 0:
            raise ValueError("numeric_range: 'std' no puede ser negativa.")
        if self.sigma is not None and self.sigma <= 0:
            raise ValueError("numeric_range: 'sigma' debe ser > 0.")
        if self.median is not None and self.median <= 0:
            raise ValueError("numeric_range: 'median' debe ser > 0 (lognormal).")
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
            value = int(round(self._quantize(self._sample(ctx, float(lo), float(hi)))))
            return max(lo, min(hi, value))
        lo_f, hi_f = self._float_bounds()
        if lo_f > hi_f:
            raise ValueError(
                f"numeric_range: rango vacío en {ctx.table}.{ctx.column.name} (min>max)."
            )
        x = max(lo_f, min(hi_f, self._quantize(self._sample(ctx, lo_f, hi_f))))
        return self._apply_float_exclusivity(x, lo_f, hi_f)

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

    def _quantize(self, x: float) -> float:
        if self._p.round_to is None:
            return x
        return round(x / self._p.round_to) * self._p.round_to

    def _apply_float_exclusivity(self, x: float, lo: float, hi: float) -> float:
        step = self._p.round_to if self._p.round_to else max((hi - lo) * 1e-9, 1e-9)
        if self._p.min_exclusive and x <= lo:
            x = lo + step
        if self._p.max_exclusive and x >= hi:
            x = hi - step
        return max(lo, min(hi, x))

    def _sample(self, ctx: GenContext, lo: float, hi: float) -> float:
        if hi <= lo:
            return lo
        dist = self._p.distribution
        if dist == "uniform":
            return ctx.rng.uniform(lo, hi)
        if dist == "normal":
            return self._sample_normal(ctx, lo, hi)
        if dist == "lognormal":
            return self._sample_lognormal(ctx, lo, hi)
        return float(self._sample_zipf(ctx, lo, hi))

    def _sample_normal(self, ctx: GenContext, lo: float, hi: float) -> float:
        mean = self._p.mean if self._p.mean is not None else (lo + hi) / 2
        std = self._p.std if self._p.std is not None else (hi - lo) / 6
        if std <= 0:
            return mean
        for _ in range(_BOUNDED_SAMPLE_TRIES):
            x = ctx.rng.gauss(mean, std)
            if lo <= x <= hi:
                return x
        return max(lo, min(hi, ctx.rng.gauss(mean, std)))

    def _sample_lognormal(self, ctx: GenContext, lo: float, hi: float) -> float:
        median = self._p.median if self._p.median is not None else max((lo + hi) / 2, 1.0)
        if median <= 0:
            median = max(hi, 1.0)
        mu = math.log(median)
        sigma = self._p.sigma if self._p.sigma is not None else 0.5
        for _ in range(_BOUNDED_SAMPLE_TRIES):
            x = ctx.rng.lognormvariate(mu, sigma)
            if lo <= x <= hi:
                return x
        return max(lo, min(hi, ctx.rng.lognormvariate(mu, sigma)))

    def _sample_zipf(self, ctx: GenContext, lo: float, hi: float) -> int:
        lo_i, hi_i = math.ceil(lo), math.floor(hi)
        if hi_i <= lo_i:
            return lo_i
        key = (lo_i, hi_i)
        cdf = self._zipf_cdf
        if self._zipf_key != key or cdf is None:
            cdf = self._build_zipf_cdf(lo_i, hi_i)
            self._zipf_cdf = cdf
            self._zipf_key = key
        idx = min(bisect.bisect_left(cdf, ctx.rng.random()), len(cdf) - 1)
        return lo_i + idx

    def _build_zipf_cdf(self, lo_i: int, hi_i: int) -> list[float]:
        n = min(hi_i - lo_i + 1, _ZIPF_MAX_RANKS)
        weights = [1.0 / (k**self._p.s) for k in range(1, n + 1)]
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
