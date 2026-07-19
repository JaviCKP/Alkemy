"""Selección de claves foráneas: qué padre referencia cada fila hija (T2.8, §7.4).

Un *selector* recibe el RNG determinista de una fila y devuelve el **índice** de
un padre dentro del `KeyStore` (T2.7), o `None` si la FK queda a NULL. Trabaja
con índices, no con las claves: el motor (Sesión E) traduce el índice a la clave
real con `KeyStore.get(tabla_padre, indice)`, de modo que una PK compuesta se
muestrea como una sola tupla y nunca por componentes (especificacion.md §7.4).

Contrato para el motor (consumo por lotes, Sesión E):

- **`FkSelector.pick(rng) -> int | None`** es la API común de las estrategias que
  deciden fila a fila: `uniform`, `zipf`, `unique_subset` y la envoltura
  `null_ratio`. El motor construye un selector por columna FK y lo llama una vez
  por fila hija, con el RNG de esa fila (`generation.seeding.rng_for_row`). Como
  cada fila trae su propio RNG independiente, el resultado no depende del tamaño
  de lote ni del orden de generación.
- **`build_quota_assignment(...)`** es aparte porque `quota` no se decide fila a
  fila sino repartiendo el lote entero entre los padres respetando `[min, max]`;
  devuelve la asignación completa de una vez.
- Los parámetros de cada estrategia (`s`, `min`, `max`, `null_ratio`) coinciden
  en nombre con los modelos de `config/models.py` (`FkZipf`, `FkQuota`, …): el
  motor mapea la estrategia del plan (`GeneratorSpec(type="fk").params`) a estos
  selectores sin traducir nombres.

Determinismo (CLAUDE.md): toda la aleatoriedad entra por el `rng` recibido; no
hay `random` global ni estado oculto salvo el que `unique_subset` necesita para
no repetir padre, que evoluciona de forma determinista con las llamadas.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from random import Random
from typing import Protocol, runtime_checkable


class FkSelectionError(ValueError):
    """Error accionable al seleccionar una FK: cardinalidad o parámetros imposibles."""


class UniqueSubsetExhaustedError(FkSelectionError):
    """`unique_subset` se quedó sin padres: hay más hijos que padres disponibles."""

    def __init__(self, *, table: str, n_parents: int, n_rows: int) -> None:
        """Construye el error nombrando la tabla y los dos números en conflicto.

        Args:
            table: Tabla hija cuya FL 1:1 no se puede satisfacer.
            n_parents: Padres disponibles (el máximo de hijos sin repetir).
            n_rows: Filas hijas pedidas.
        """
        self.table = table
        self.n_parents = n_parents
        self.n_rows = n_rows
        super().__init__(
            f"unique_subset agotado en '{table}': hay {n_parents} padres disponibles pero se "
            f"piden {n_rows} filas hijas sin reemplazo (1:1). Reduce las filas de '{table}' a "
            f"lo sumo a {n_parents}, o usa otra estrategia de FK si la relación no es 1:1."
        )


class QuotaInfeasibleError(FkSelectionError):
    """La cuota `[min, max]` por padre no puede alojar exactamente `n_rows` hijos."""

    def __init__(self, *, n_parents: int, n_rows: int, min_per: int, max_per: int) -> None:
        """Construye el error con los cuatro números y el rango factible.

        Args:
            n_parents: Número de padres entre los que repartir.
            n_rows: Filas hijas a repartir.
            min_per: Mínimo de hijos por padre pedido.
            max_per: Máximo de hijos por padre pedido.
        """
        self.n_parents = n_parents
        self.n_rows = n_rows
        self.min_per = min_per
        self.max_per = max_per
        low = n_parents * min_per
        high = n_parents * max_per
        super().__init__(
            f"quota infactible: no se pueden repartir {n_rows} filas entre {n_parents} padres "
            f"con [min={min_per}, max={max_per}] hijos por padre. El total factible está en "
            f"[{low}, {high}]; ajusta min/max, el número de padres o el de filas."
        )


def _require_parents(n_parents: int) -> None:
    """Rechaza construir un selector fila a fila sin padres a los que apuntar."""
    if n_parents <= 0:
        raise FkSelectionError(
            f"No hay padres disponibles (n_parents={n_parents}) para seleccionar una FK. El "
            f"motor genera la tabla padre antes que la hija; si la relación es opcional, deja "
            f"la FK anulable y usa null_ratio."
        )


@runtime_checkable
class FkSelector(Protocol):
    """Estrategia de selección de FK que decide fila a fila (contrato de `pick`)."""

    def pick(self, rng: Random) -> int | None:
        """Elige el índice del padre para una fila hija, o `None` si la FK va a NULL.

        Args:
            rng: RNG determinista de la fila hija (`generation.seeding.rng_for_row`).

        Returns:
            El índice 0-based del padre dentro del `KeyStore`, o `None` para NULL.
        """
        ...


@dataclass
class UniformSelector:
    """Cada padre con la misma probabilidad (defecto de §7.4)."""

    n_parents: int

    def __post_init__(self) -> None:
        """Valida que haya al menos un padre al que apuntar."""
        _require_parents(self.n_parents)

    def pick(self, rng: Random) -> int | None:
        """Devuelve un índice de padre equiprobable en `[0, n_parents)`."""
        return rng.randrange(self.n_parents)


@dataclass
class ZipfSelector:
    """Popularidad sesgada: pocos padres concentran muchos hijos (§7.4).

    El ranking de popularidad se fija por **índice de inserción**: el padre `0` es
    el más popular, el `1` el siguiente, etc. (peso `1 / (i + 1) ** s`). Es una
    decisión deliberada y determinista —no se ordena por el valor de la clave—,
    de modo que quién es «popular» solo depende del orden en que el motor insertó
    los padres, no de sus datos.
    """

    n_parents: int
    s: float
    _cumulative: list[float] = field(init=False, repr=False, default_factory=list)

    def __post_init__(self) -> None:
        """Precalcula la CDF acumulada normalizada de los pesos zipfianos."""
        _require_parents(self.n_parents)
        weights = [1.0 / ((i + 1) ** self.s) for i in range(self.n_parents)]
        total = sum(weights)
        acc = 0.0
        cumulative: list[float] = []
        for weight in weights:
            acc += weight
            cumulative.append(acc / total)
        # El último punto se fija a 1.0 exacto: rng.random() ∈ [0, 1) cae siempre
        # por debajo, así que el índice devuelto nunca se sale de rango por el
        # redondeo de coma flotante de la suma acumulada.
        cumulative[-1] = 1.0
        self._cumulative = cumulative

    def pick(self, rng: Random) -> int | None:
        """Muestrea un índice de padre por la CDF (padres bajos = más probables)."""
        return bisect.bisect_right(self._cumulative, rng.random())


@dataclass
class UniqueSubsetSelector:
    """Relación 1:1: cada padre a lo sumo un hijo, muestreo sin reemplazo (§7.4).

    Es la única estrategia con estado: recuerda qué padres quedan libres. El motor
    la llama una vez por fila hija; cuando se agota (más hijos que padres) lanza
    `UniqueSubsetExhaustedError`. `n_rows` solo se guarda para poder nombrar
    «filas pedidas» en ese error.
    """

    n_parents: int
    n_rows: int
    table: str
    _remaining: list[int] = field(init=False, repr=False, default_factory=list)

    def __post_init__(self) -> None:
        """Inicializa la reserva de padres libres, en orden de inserción."""
        self._remaining = list(range(self.n_parents))

    def pick(self, rng: Random) -> int | None:
        """Devuelve un padre no usado aún; agotamiento ⇒ `UniqueSubsetExhaustedError`."""
        if not self._remaining:
            raise UniqueSubsetExhaustedError(
                table=self.table, n_parents=self.n_parents, n_rows=self.n_rows
            )
        pos = rng.randrange(len(self._remaining))
        # Swap-pop: quitar en O(1) sin conservar el orden (irrelevante sin reemplazo).
        last = len(self._remaining) - 1
        self._remaining[pos], self._remaining[last] = self._remaining[last], self._remaining[pos]
        return self._remaining.pop()


@dataclass
class NullRatioSelector:
    """Envoltura que pone la FK a NULL con probabilidad `null_ratio` (§7.4).

    Decide el NULL **antes** de delegar en el selector interno: consume un único
    `rng.random()` para la moneda y solo si sale «no nulo» llama a `inner.pick`.
    Así el RNG de selección del interior se consume exclusivamente en las filas no
    nulas, y —al traer cada fila su propio RNG— el consumo de una fila no depende
    de cuántos NULL cayeran en las demás.

    Envuelve cualquier estrategia fila a fila (`uniform`/`zipf`/`unique_subset`).
    Para `quota`, que reparte el lote entero, el NULL lo aplica el motor a nivel de
    lote antes de asignar los supervivientes (no hay una envoltura `pick` posible
    sobre un asignador por lote).
    """

    inner: FkSelector
    null_ratio: float

    def __post_init__(self) -> None:
        """Valida que `null_ratio` sea una probabilidad en `[0, 1]`."""
        if not 0.0 <= self.null_ratio <= 1.0:
            raise FkSelectionError(f"null_ratio debe estar en [0, 1]; recibido {self.null_ratio}.")

    def pick(self, rng: Random) -> int | None:
        """`None` con probabilidad `null_ratio`; si no, delega en el selector interno."""
        if rng.random() < self.null_ratio:
            return None
        return self.inner.pick(rng)


def build_quota_assignment(
    rng: Random, n_parents: int, n_rows: int, min: int, max: int
) -> list[int]:
    """Reparte `n_rows` hijos entre `n_parents` padres respetando `[min, max]` por padre.

    Asignador por **lote completo** (no fila a fila): útil para puentes y para
    «cada contrato tiene entre 1 y 12 pagos» (§7.4). Da a cada padre su `min`
    garantizado y reparte el excedente al azar entre los que aún tienen holgura,
    hasta cubrir exactamente `n_rows`; después baraja la lista para que el índice
    del padre no quede correlacionado con la posición de la fila.

    Args:
        rng: RNG determinista del lote; fija el reparto y el barajado.
        n_parents: Número de padres disponibles.
        n_rows: Filas hijas a repartir.
        min: Mínimo de hijos por padre (coincide con `FkQuota.min`).
        max: Máximo de hijos por padre (coincide con `FkQuota.max`).

    Returns:
        Lista de longitud `n_rows` con el índice de padre de cada fila hija, ya
        barajada. El padre `p` aparece entre `min` y `max` veces.

    Raises:
        QuotaInfeasibleError: Si `n_rows` no cabe en `[n_parents*min, n_parents*max]`.
    """
    low = n_parents * min
    high = n_parents * max
    if not low <= n_rows <= high:
        raise QuotaInfeasibleError(n_parents=n_parents, n_rows=n_rows, min_per=min, max_per=max)

    counts = [min] * n_parents
    remaining = n_rows - low
    # Padres que aún admiten más hijos (holgura max-min); se reparte el excedente
    # uno a uno entre ellos y se retiran al llenarse (swap-pop, O(1)).
    available = [p for p in range(n_parents) if max - min > 0]
    while remaining > 0:
        pos = rng.randrange(len(available))
        parent = available[pos]
        counts[parent] += 1
        remaining -= 1
        if counts[parent] == max:
            available[pos] = available[-1]
            available.pop()

    assignment: list[int] = []
    for parent, count in enumerate(counts):
        assignment.extend([parent] * count)
    rng.shuffle(assignment)
    return assignment
