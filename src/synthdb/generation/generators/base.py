"""Interfaz, registro y envoltura de unicidad de los generadores (T2.2).

Un *generador* produce el valor de una columna a partir de un `GenContext` (el
RNG determinista de la fila, la columna de la IR y la tabla). Cada tipo de
generador declara sus parámetros como un modelo Pydantic propio, de modo que un
`GeneratorSpec` inválido (campos que faltan o sobran) se rechaza con un error de
campo exacto al resolverlo, no a mitad de la generación.

El registro es un `dict` por nombre poblado con `register()`. Los *entry points*
de plugins (`synthdb.generators`) son de la v1.0 (especificacion.md §10): aquí
solo el registro interno. Importar el paquete `synthdb.generation.generators`
registra todo el catálogo básico (efecto de importación de cada módulo).

Alcance H2 Sesión A — `TypeSpec.is_array` se **ignora**: un generador produce
siempre el valor de un ELEMENTO; envolver esos valores en un array de PostgreSQL
es trabajo del motor (sesión E). `GenContext` tampoco expone todavía `row` ni
`parent()` (dependencias intra-fila y con el padre): son de la sesión D. El hueco
está reservado en el docstring de `GenContext`, no implementado.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict

from synthdb.ir.schema import ColumnSpec, GeneratorSpec

_UNIQUE_RETRIES = 50
"""Reintentos máximos de la envoltura de unicidad antes de rendirse (T2.2)."""


@dataclass
class GenContext:
    """Contexto que recibe un generador para producir el valor de una celda.

    Attributes:
        rng: RNG determinista **de esta fila** (`generation.seeding.rng_for_row`).
            Es la única fuente de aleatoriedad admitida en la generación; usar
            `random` global rompería la reproducibilidad (CLAUDE.md).
        column: La columna de la IR que se está generando. El generador puede
            leer su `type` (kind, bits, longitud...) y `enum_values` como cotas.
        table: Nombre de la tabla propietaria, para mensajes y para el generador
            `template`.

    Diseño a futuro (sesión D, NO implementado aquí): este contexto se extenderá
    con `row: dict[str, Any]` (los valores ya generados de la misma fila, para
    derivaciones como `total = cantidad * precio`) y un accesor `parent(fk)` (la
    fila padre elegida por una FK, para coherencia con el padre, especificacion.md
    §7.2). Se documenta el hueco para que la firma de `generate()` no cambie al
    añadirlos.
    """

    rng: Random
    column: ColumnSpec
    table: str


@runtime_checkable
class Generator(Protocol):
    """Protocolo estructural de un generador de valores de columna."""

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve el valor de la columna `ctx.column` para una fila.

        Args:
            ctx: Contexto de generación (RNG de fila, columna, tabla).

        Returns:
            El valor generado; su tipo Python depende del generador.
        """
        ...


class GeneratorParams(BaseModel):
    """Base de los modelos de parámetros de cada generador.

    `extra="forbid"` es lo que convierte un parámetro de más en un error de
    validación explícito (CLAUDE.md: nada se ignora en silencio).
    """

    model_config = ConfigDict(extra="forbid")


P = TypeVar("P", bound=GeneratorParams)


@dataclass(frozen=True)
class _RegistryEntry:
    """Une el modelo de parámetros de un generador con su constructor."""

    params_model: type[GeneratorParams]
    build: Callable[[Any], Generator]


_REGISTRY: dict[str, _RegistryEntry] = {}


def register(
    name: str,
    params_model: type[P],
    build: Callable[[P], Generator],
) -> None:
    """Registra un generador bajo `name`.

    Args:
        name: Id del generador, el que llevará `GeneratorSpec.type`.
        params_model: Modelo Pydantic que valida `GeneratorSpec.params`.
        build: Constructor que recibe los parámetros ya validados y devuelve el
            generador.

    Raises:
        ValueError: Si `name` ya está registrado (colisión de catálogo).
    """
    if name in _REGISTRY:
        raise ValueError(f"El generador '{name}' ya está registrado.")
    _REGISTRY[name] = _RegistryEntry(params_model=params_model, build=build)


def registered_names() -> list[str]:
    """Devuelve los nombres de generador registrados, en orden alfabético."""
    return sorted(_REGISTRY)


class UnknownGeneratorError(KeyError):
    """`GeneratorSpec.type` no corresponde a ningún generador registrado."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"Generador desconocido: '{name}'. "
            f"Generadores registrados: {', '.join(available) or '(ninguno)'}. "
            f"Revisa 'generator.type' en la configuración o en el plan."
        )


class UniqueExhaustedError(RuntimeError):
    """La envoltura de unicidad agotó sus reintentos sin un valor nuevo."""

    def __init__(self, *, table: str, column: str, achieved: int, retries: int) -> None:
        self.table = table
        self.column = column
        self.achieved = achieved
        self.retries = retries
        super().__init__(
            f"No se pudo generar un valor único nuevo para {table}.{column} tras "
            f"{retries} reintentos; el generador solo alcanza a producir {achieved} "
            f"valores distintos. La cardinalidad pedida supera la alcanzable: reduce "
            f"el número de filas de la tabla, amplía el rango del generador, o quita "
            f"'unique' si la columna admite repetidos."
        )


@dataclass
class _UniqueGenerator:
    """Envuelve un generador para garantizar valores distintos entre filas.

    Mantiene un conjunto de valores vistos y reintenta hasta `retries` veces
    contra el mismo `GenContext` (cuyo `rng` avanza en cada intento) antes de
    lanzar `UniqueExhaustedError`. El estado vive tanto como el generador
    resuelto, es decir, toda la generación de la columna: por eso la unicidad se
    aplica a nivel de registro (`resolve`), no dentro de cada generador. Los
    valores deben ser hashables; los escalares del catálogo básico lo son (los
    arrays quedan fuera de alcance esta sesión, ver docstring del módulo).
    """

    inner: Generator
    retries: int = _UNIQUE_RETRIES
    _seen: set[Any] = field(default_factory=set, init=False, repr=False)

    def generate(self, ctx: GenContext) -> Any:
        """Devuelve un valor no visto antes o lanza `UniqueExhaustedError`."""
        for _ in range(self.retries):
            value = self.inner.generate(ctx)
            if value not in self._seen:
                self._seen.add(value)
                return value
        raise UniqueExhaustedError(
            table=ctx.table,
            column=ctx.column.name,
            achieved=len(self._seen),
            retries=self.retries,
        )


def resolve(spec: GeneratorSpec) -> Generator:
    """Resuelve un `GeneratorSpec` a un generador listo para usar.

    Valida `spec.params` contra el modelo del generador (error de campo exacto si
    faltan o sobran) y, si `spec.unique`, envuelve el resultado en la envoltura de
    unicidad.

    Args:
        spec: Especificación del generador (de la IR, el plan o la config).

    Returns:
        Un generador que cumple el protocolo `Generator`.

    Raises:
        UnknownGeneratorError: Si `spec.type` no está registrado.
        pydantic.ValidationError: Si `spec.params` no valida contra el modelo.
    """
    try:
        entry = _REGISTRY[spec.type]
    except KeyError:
        raise UnknownGeneratorError(spec.type, registered_names()) from None
    params = entry.params_model.model_validate(spec.params)
    generator = entry.build(params)
    if spec.unique:
        return _UniqueGenerator(generator)
    return generator
