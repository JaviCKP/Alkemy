"""Especificación de distribuciones reutilizable (especificacion.md §5, §8, §11).

Forma anidada canónica que consumen los generadores numéricos, el selector de FK
y —en el Hito 3— el contrato del LLM:

    {"family": "uniform|normal|lognormal|zipf", "params": {...}}

Los parámetros viven DENTRO de `params` y se validan contra el modelo de su
familia: un campo desconocido para la familia es un error de campo exacto, nunca
se ignora en silencio (CLAUDE.md). No hay forma «plana» (parámetros hermanos de
`family`): la única forma admitida es la anidada.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from synthdb.generation.generators.base import GeneratorParams

DistributionFamily = Literal["uniform", "normal", "lognormal", "zipf"]
"""Familias soportadas por el catálogo del MVP (especificacion.md §7.3)."""


class UniformParams(GeneratorParams):
    """Uniforme: sin parámetros propios (el rango lo aporta el generador)."""


class NormalParams(GeneratorParams):
    """Normal (gaussiana) recortada al rango del generador."""

    mean: float | None = Field(default=None, description="Centro (def: punto medio del rango).")
    std: float | None = Field(default=None, description="Desviación típica (def: rango/6).")

    @model_validator(mode="after")
    def _validate(self) -> NormalParams:
        if self.std is not None and self.std < 0:
            raise ValueError("normal: 'std' no puede ser negativa.")
        return self


class LognormalParams(GeneratorParams):
    """Lognormal recortada al rango del generador."""

    median: float | None = Field(default=None, description="Mediana (def: punto medio del rango).")
    sigma: float | None = Field(default=None, description="Sigma en espacio log (def: 0.5).")

    @model_validator(mode="after")
    def _validate(self) -> LognormalParams:
        if self.median is not None and self.median <= 0:
            raise ValueError("lognormal: 'median' debe ser > 0.")
        if self.sigma is not None and self.sigma <= 0:
            raise ValueError("lognormal: 'sigma' debe ser > 0.")
        return self


class ZipfParams(GeneratorParams):
    """Zipf sobre los enteros del rango, sesgada hacia el mínimo."""

    s: float = Field(default=1.2, description="Exponente; mayor ⇒ más sesgo hacia el mínimo.")

    @model_validator(mode="after")
    def _validate(self) -> ZipfParams:
        if self.s <= 0:
            raise ValueError("zipf: el exponente 's' debe ser > 0.")
        return self


FamilyParams = UniformParams | NormalParams | LognormalParams | ZipfParams
"""Unión de los modelos de parámetros por familia."""

_FAMILY_PARAMS: dict[str, type[GeneratorParams]] = {
    "uniform": UniformParams,
    "normal": NormalParams,
    "lognormal": LognormalParams,
    "zipf": ZipfParams,
}


class DistributionSpec(BaseModel):
    """Familia de distribución más sus parámetros, en la forma anidada canónica.

    `params` se valida contra el modelo de `family` (ver el módulo). Reutilizable
    por cualquier generador que muestree (numérico, FK) y por el contrato del LLM
    del Hito 3. Por defecto, `uniform` sin parámetros.
    """

    model_config = ConfigDict(extra="forbid")

    family: DistributionFamily = "uniform"
    params: FamilyParams = Field(default_factory=UniformParams)

    @model_validator(mode="before")
    @classmethod
    def _validate_params_by_family(cls, data: Any) -> Any:
        """Valida `params` contra el modelo de la familia (error exacto si sobra)."""
        if not isinstance(data, dict):
            return data
        family = data.get("family", "uniform")
        model = _FAMILY_PARAMS.get(family)
        if model is None:
            # Familia inválida: deja que la validación del Literal `family` emita
            # el error exacto en lugar de adivinar un modelo de parámetros.
            return data
        raw = data.get("params", {})
        if isinstance(raw, GeneratorParams):
            raw = raw.model_dump()
        return {**data, "params": model.model_validate(raw)}
