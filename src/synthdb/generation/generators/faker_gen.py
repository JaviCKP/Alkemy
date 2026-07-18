"""Generador basado en Faker: proveedores parametrizados y deterministas (T2.3).

Determinismo (CLAUDE.md): se mantiene UNA instancia de `Faker` por (locale) y se
la resiembra con el RNG de cada fila antes de invocar al proveedor, de modo que
el valor depende solo de `(semilla de fila, proveedor)` y no del orden ni del
tamaño de lote. No se usa `faker.unique` (estado global del proceso): la unicidad
la aporta la envoltura de T2.2.
"""

from __future__ import annotations

from typing import Any

from faker import Faker
from pydantic import Field

from synthdb.generation.generators.base import GenContext, GeneratorParams, register

_DEFAULT_LOCALE = "es_ES"
"""Locale por defecto (`config.yaml: locale`). Cuando `config/models.py` (T2.5)
exista, pasará el locale del usuario vía `params`; por ahora el defecto vive aquí.
"""


class FakerParams(GeneratorParams):
    """Parámetros de `faker`: proveedor, locale y kwargs del proveedor."""

    provider: str = Field(description="Método de Faker: 'name', 'email', 'street_address'...")
    locale: str = _DEFAULT_LOCALE
    kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Argumentos por nombre para el método de Faker."
    )


class FakerGenerator:
    """Envuelve un proveedor de Faker sembrado determinísticamente por fila (T2.3)."""

    def __init__(self, params: FakerParams) -> None:
        self._provider = params.provider
        self._kwargs = params.kwargs
        self._faker = Faker(params.locale)
        if not hasattr(self._faker, params.provider):
            raise ValueError(
                f"faker: el proveedor '{params.provider}' no existe para el locale "
                f"'{params.locale}'. Revisa el nombre del método de Faker."
            )

    def generate(self, ctx: GenContext) -> Any:
        """Resiembra la instancia con el RNG de fila e invoca al proveedor."""
        self._faker.seed_instance(ctx.rng.getrandbits(64))
        return getattr(self._faker, self._provider)(**self._kwargs)


register("faker", FakerParams, FakerGenerator)
