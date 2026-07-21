"""Generadores de texto e identificadores: `choice`, `template`, `uuid`, `fallback`.

`fallback` es la red de seguridad del fusor (especificacion.md §7.1): dado
cualquier kind del `TypeSpec`, produce un valor estructuralmente válido aunque su
semántica sea pobre. No interseca cotas de `CHECK` ni de enum más allá de leer
`enum_values`: recortar propuestas contra la IR es trabajo del fusor (T2.6), no de
un generador.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import Field, model_validator

from synthdb.generation.generators.base import GenContext, GeneratorParams, register
from synthdb.generation.numeric_bounds import quantize_to_scale, representable_limit

_DEFAULT_TEMPLATE = "{tabla}_{columna}_{n}"
"""Plantilla por defecto (especificacion.md §7.1)."""

_FALLBACK_MIN = datetime(2015, 1, 1)
_FALLBACK_MAX = datetime(2025, 1, 1)
"""Década fija para las fechas de `fallback` (sin `datetime.now()`, determinismo)."""

_FALLBACK_INT_LO = 0
_FALLBACK_INT_HI = 1000
"""Rango pequeño de `fallback` para enteros (especificacion.md §7.1)."""


class ChoiceParams(GeneratorParams):
    """Parámetros de `choice`: valores y pesos opcionales."""

    values: list[Any] = Field(min_length=1)
    weights: list[float] | None = None

    @model_validator(mode="after")
    def _validate(self) -> ChoiceParams:
        if self.weights is not None:
            if len(self.weights) != len(self.values):
                raise ValueError("choice: 'weights' debe tener la misma longitud que 'values'.")
            if any(w < 0 for w in self.weights):
                raise ValueError("choice: los pesos no pueden ser negativos.")
            if sum(self.weights) <= 0:
                raise ValueError("choice: la suma de pesos debe ser > 0.")
        return self


class ChoiceGenerator:
    """Elección ponderada entre valores fijos (T2.3)."""

    def __init__(self, params: ChoiceParams) -> None:
        self._values = params.values
        self._weights = params.weights

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve uno de los valores según los pesos (uniforme si no hay)."""
        return ctx.rng.choices(self._values, weights=self._weights, k=1)[0]


class TemplateParams(GeneratorParams):
    """Parámetros de `template`: cadena de formato y arranque del contador."""

    template: str = _DEFAULT_TEMPLATE
    start: int = 0


class TemplateGenerator:
    """Texto por plantilla con marcadores `{tabla}`/`{columna}`/`{n}` (T2.3).

    `{n}` es un contador interno (arranca en `start`) que avanza por llamada, así
    que sigue el ORDEN de generación igual que `sequence`. Se aceptan los alias en
    inglés `{table}`/`{column}`. La plantilla se valida al construir el generador
    (fallo temprano si tiene un marcador desconocido), no fila a fila.
    """

    def __init__(self, params: TemplateParams) -> None:
        self._template = params.template
        self._n = params.start
        try:
            self._render(table="", column="", n=0)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"template: plantilla inválida {params.template!r}. Marcadores "
                f"admitidos: {{tabla}}/{{table}}, {{columna}}/{{column}}, {{n}}."
            ) from exc

    def _render(self, *, table: str, column: str, n: int) -> str:
        return self._template.format_map(
            {"tabla": table, "table": table, "columna": column, "column": column, "n": n}
        )

    def generate(self, ctx: GenContext) -> Any:
        """Renderiza la plantilla con la tabla, la columna y el contador `{n}`."""
        value = self._render(table=ctx.table, column=ctx.column.name, n=self._n)
        self._n += 1
        return value


class UuidParams(GeneratorParams):
    """`uuid` no toma parámetros; su aleatoriedad sale de `ctx.rng`."""


class UuidGenerator:
    """UUID versión 4 derivado del RNG de fila, por tanto determinista (T2.3)."""

    def __init__(self, params: UuidParams) -> None:
        pass

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve un `uuid.UUID` v4 derivado del RNG de la fila."""
        return uuid_module.UUID(int=ctx.rng.getrandbits(128), version=4)


class FallbackParams(GeneratorParams):
    """`fallback` no toma parámetros; se guía por el kind de la columna."""


class FallbackGenerator:
    """Fallback seguro por kind del `TypeSpec` (especificacion.md §7.1, T2.3).

    Cubre todo el catálogo canónico de tipos: entero (rango pequeño), numérico,
    texto/`varchar(n)`/`char(n)` (respeta la longitud), fecha, timestamp (con zona
    si el tipo la declara), booleano 50/50, uuid, enum (usa `enum_values`) y, como
    último recurso, `json`/`bytea` con un valor vacío válido.
    """

    def __init__(self, params: FallbackParams) -> None:
        pass

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve un valor estructuralmente válido según el kind de la columna."""
        column = ctx.column
        kind = column.type.kind
        if kind == "integer":
            return ctx.rng.randint(_FALLBACK_INT_LO, _FALLBACK_INT_HI)
        if kind == "numeric":
            hi = float(_FALLBACK_INT_HI)
            if column.type.precision is not None:
                # No desbordar NUMERIC(p, s): recorta el rango y redondea a la escala.
                hi = min(hi, float(representable_limit(column.type.precision, column.type.scale)))
                raw = ctx.rng.uniform(0.0, hi)
                return float(quantize_to_scale(raw, column.type.scale))
            return round(ctx.rng.uniform(0.0, hi), 2)
        if kind == "boolean":
            return ctx.rng.random() < 0.5
        if kind == "uuid":
            return uuid_module.UUID(int=ctx.rng.getrandbits(128), version=4)
        if kind == "enum":
            if not column.enum_values:
                raise ValueError(
                    f"fallback: la columna enum {ctx.table}.{column.name} no declara "
                    f"'enum_values'; no hay valores seguros que elegir."
                )
            return ctx.rng.choice(column.enum_values)
        if kind == "date":
            lo, hi = _FALLBACK_MIN.date().toordinal(), _FALLBACK_MAX.date().toordinal()
            return date.fromordinal(ctx.rng.randint(lo, hi))
        if kind == "timestamp":
            total = int((_FALLBACK_MAX - _FALLBACK_MIN).total_seconds())
            value = _FALLBACK_MIN + timedelta(seconds=ctx.rng.randint(0, total))
            return value.replace(tzinfo=UTC) if column.type.with_timezone else value
        if kind == "json":
            return {}
        if kind == "bytea":
            return b""
        # text / varchar / char y cualquier otro: cadena, recortada a la longitud.
        text = f"{ctx.table}_{column.name}_{ctx.rng.getrandbits(32)}"
        length = column.type.length
        if kind in ("varchar", "char") and length is not None:
            return text[:length]
        return text


register("choice", ChoiceParams, ChoiceGenerator)
register("template", TemplateParams, TemplateGenerator)
register("uuid", UuidParams, UuidGenerator)
register("fallback", FallbackParams, FallbackGenerator)
