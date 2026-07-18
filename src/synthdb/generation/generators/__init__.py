"""Catálogo de generadores y API de registro (T2.2/T2.3).

Importar este paquete registra todos los generadores del catálogo básico: cada
módulo del catálogo llama a `register()` al importarse, así que basta con
importarlos aquí (efecto de importación). El resto del código resuelve
generadores por nombre vía `resolve(GeneratorSpec)`.
"""

from synthdb.generation.generators import (  # noqa: F401 -- import por efecto (registro)
    faker_gen,
    numeric,
    temporal,
    text,
)
from synthdb.generation.generators.base import (
    GenContext,
    Generator,
    GeneratorParams,
    UniqueExhaustedError,
    UnknownGeneratorError,
    register,
    registered_names,
    resolve,
)

__all__ = [
    "GenContext",
    "Generator",
    "GeneratorParams",
    "UniqueExhaustedError",
    "UnknownGeneratorError",
    "register",
    "registered_names",
    "resolve",
]
