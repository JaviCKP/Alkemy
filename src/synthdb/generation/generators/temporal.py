"""Generador temporal: `datetime_range` para `date` y `timestamp` (T2.3).

El rango por defecto es una década FIJA. No se usa `datetime.now()` en ninguna
ruta de generación (CLAUDE.md): dos ejecuciones en días distintos deben producir
los mismos bytes, así que el «hoy» no puede entrar aquí.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import model_validator

from synthdb.generation.generators.base import GenContext, GeneratorParams, register

_DEFAULT_MIN = datetime(2015, 1, 1)
_DEFAULT_MAX = datetime(2025, 1, 1)
"""Década por defecto, fija para no depender de la fecha del sistema."""


class DatetimeRangeParams(GeneratorParams):
    """Parámetros de `datetime_range`: cotas `min`/`max` inclusivas.

    Se aceptan `datetime` o cadenas ISO (`"2015-01-01"`); Pydantic las coacciona.
    Una cadena solo-fecha se interpreta como medianoche. Si faltan, se usa la
    década por defecto del módulo.
    """

    min: datetime | None = None
    max: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> DatetimeRangeParams:
        lo = self.min if self.min is not None else _DEFAULT_MIN
        hi = self.max if self.max is not None else _DEFAULT_MAX
        if hi < lo:
            raise ValueError("datetime_range: 'max' debe ser >= 'min'.")
        return self


class DatetimeRangeGenerator:
    """Genera una fecha u hora en `[min, max]` según el kind de la columna (T2.3).

    Si la columna es `date` devuelve un `datetime.date` (resolución de día); si es
    `timestamp` devuelve un `datetime.datetime` (resolución de segundo), con
    `tzinfo=UTC` cuando el tipo declara zona horaria. La aritmética se hace por
    diferencia de `datetime` (nunca `datetime.timestamp()`, que asume zona local y
    rompería el determinismo entre máquinas).
    """

    def __init__(self, params: DatetimeRangeParams) -> None:
        self._min = params.min if params.min is not None else _DEFAULT_MIN
        self._max = params.max if params.max is not None else _DEFAULT_MAX

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve un `date` o `datetime` en `[min, max]` según el kind."""
        if ctx.column.type.kind == "date":
            lo = self._min.date().toordinal()
            hi = self._max.date().toordinal()
            return date.fromordinal(ctx.rng.randint(lo, hi))
        total_seconds = int((self._max - self._min).total_seconds())
        value = self._min + timedelta(seconds=ctx.rng.randint(0, total_seconds))
        if ctx.column.type.with_timezone:
            value = value.replace(tzinfo=UTC)
        return value


register("datetime_range", DatetimeRangeParams, DatetimeRangeGenerator)
