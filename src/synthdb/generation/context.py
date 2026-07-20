"""Contexto de fila y orden de columnas intra-fila (T2.10, especificacion.md §7.2).

Dos piezas que habilitan las dependencias entre columnas de una misma fila:

1. `RowContext` extiende el `GenContext` de la sesión A rellenando el hueco que su
   docstring dejaba reservado: además del `rng`, la `column` y la `table`, expone
   `row` (los valores ya generados de la fila), `parent(<fk>)` (la fila padre
   elegida por una FK) y `refs` (constantes con nombre). Los generadores existentes
   siguen recibiendo un `GenContext` y no cambian de firma: `RowContext` es un
   subtipo, así que un generador que solo lee `rng`/`column`/`table` lo ignora, y el
   generador `derived` (que sí necesita `row`/`parent`/`refs`) lo estrecha con un
   `isinstance`.

2. `build_column_order` ordena las columnas de una tabla según las dependencias
   IMPLÍCITAS que las reglas del mini-DSL introducen: si una regla deriva o acota
   una columna leyendo otras, esas otras deben generarse antes. Es un topo-sort
   determinista (desempate alfabético); un ciclo entre columnas es un `PlanError`.

`parent()` se alimenta de un resolutor inyectado (`ParentResolver`). En esta sesión
el motor aún no existe; el resolutor típico envuelve un `dict` fila→padre construido
a mano (tests) o, en la sesión E, poblado por el motor desde el KeyStore/selector de
FK. El protocolo deja esa frontera explícita sin acoplar este módulo al motor.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from synthdb.generation.generators.base import GenContext
from synthdb.ir.plans import TablePlan
from synthdb.rules.dsl import Rule, rule_dependencies

ParentRow = dict[str, Any]
"""Una fila padre resuelta: sus columnas por nombre. `None` si no hay padre."""


class PlanError(ValueError):
    """Ciclo de dependencias entre columnas de una tabla, detectado en compilación.

    Se lanza cuando las reglas de una tabla se referencian en círculo (`a` deriva
    de `b`, `b` de `a`): no existe ningún orden de generación válido. El mensaje
    nombra las columnas del ciclo para que el usuario sepa qué regla romper. Es un
    error de compilación del plan, hermano del `PlanError` del fusor
    (`semantic/merge.py`), pero de otra fase: aquel resuelve contradicciones
    usuario-vs-IR, este ordena columnas dentro de una fila.
    """


@runtime_checkable
class ParentResolver(Protocol):
    """Resuelve una columna FK a la fila padre elegida para la fila en curso.

    Un `RowContext` delega en un `ParentResolver` para `parent(<fk>)`. En esta
    sesión el resolutor típico es `mapping_resolver`, que envuelve un `dict`; en la
    sesión E el motor inyectará uno que consulta el KeyStore y el selector de FK
    (`generation/fk.py`). Devuelve `None` cuando la FK es NULL o no se resolvió.
    """

    def __call__(self, fk_column: str) -> ParentRow | None:
        """Devuelve la fila padre de `fk_column`, o `None` si no hay."""
        ...


def _no_parent(fk_column: str) -> ParentRow | None:
    """Resolutor por defecto: no hay padres (todo `parent()` devuelve `None`)."""
    return None


@dataclass(frozen=True)
class _MappingResolver:
    """`ParentResolver` respaldado por un `dict` fila_fk → fila_padre."""

    parents: Mapping[str, ParentRow | None]

    def __call__(self, fk_column: str) -> ParentRow | None:
        """Busca `fk_column` en el mapa; ausente o NULL ⇒ `None`."""
        return self.parents.get(fk_column)


def mapping_resolver(parents: Mapping[str, ParentRow | None]) -> ParentResolver:
    """Construye un `ParentResolver` a partir de un `dict` FK → fila padre.

    Args:
        parents: Mapa de nombre de columna FK a la fila padre (un `dict` de
            columnas) elegida para la fila en curso, o `None` si la FK es NULL.

    Returns:
        Un resolutor que consulta ese mapa.
    """
    return _MappingResolver(dict(parents))


@dataclass
class RowContext(GenContext):
    """`GenContext` extendido con el estado de la fila (§7.2, T2.10).

    Rellena el hueco documentado en `GenContext`: además de `rng`/`column`/`table`,
    lleva los valores ya generados de la fila y los accesos a padre y constantes que
    las reglas del mini-DSL necesitan.

    Attributes:
        row: Valores ya generados de la fila, por nombre de columna. El generador
            `derived` y las reglas leen de aquí; el motor lo va rellenando en el
            orden de `build_column_order`.
        refs: Constantes con nombre del bloque `refs` del YAML, para `ref('<n>')`.
        resolve_parent: Resolutor de filas padre para `parent(<fk>)`. Por defecto no
            hay padres; inyecta uno con `mapping_resolver(...)` o desde el motor.
    """

    row: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)
    resolve_parent: ParentResolver = _no_parent

    def parent(self, fk_column: str) -> ParentRow | None:
        """Devuelve la fila padre elegida por `fk_column`, o `None` si no hay.

        Args:
            fk_column: Nombre de la columna FK cuyo padre se quiere (el mismo que se
                escribe en la regla como `parent(<fk_column>)`).

        Returns:
            La fila padre como `dict` de columnas, o `None` si la FK es NULL o no se
            inyectó su padre.
        """
        return self.resolve_parent(fk_column)


def build_column_order(table_plan: TablePlan, rules: Iterable[Rule]) -> list[str]:
    """Ordena las columnas de una tabla respetando las dependencias de las reglas.

    Extrae de cada regla `bound`/`derivation` la dependencia implícita "la columna
    que acota/deriva se genera después de las columnas locales que su expresión
    lee" (`rules.dsl.rule_dependencies`) y hace un topo-sort determinista de las
    columnas. El desempate entre columnas sin orden relativo es alfabético, de modo
    que el resultado es estable con independencia del orden de las reglas o del de
    la IR (CLAUDE.md: determinismo).

    Las aserciones no imponen orden (se comprueban tras generar toda la fila). Una
    regla que lea una columna inexistente en la tabla no añade arista aquí: la
    existencia de columnas se valida al evaluar (`RuleEvalError` con la columna
    concreta), no en la ordenación, que es puramente topológica.

    Args:
        table_plan: El plan de la tabla; sus columnas definen el conjunto a ordenar
            (en el orden de la IR, que solo importa como conjunto: el orden de
            salida lo fija el topo-sort).
        rules: Las reglas de la tabla, ya parseadas (`rules.parse_rule`). Se
            parsean una sola vez aguas arriba para que los errores de gramática
            afloren antes.

    Returns:
        Los nombres de las columnas en un orden de generación válido.

    Raises:
        PlanError: Si las reglas forman un ciclo de dependencias entre columnas.
    """
    columns = [col.column for col in table_plan.columns]
    colset = set(columns)

    # deps[c] = columnas que deben generarse ANTES que c (sus prerrequisitos).
    deps: dict[str, set[str]] = {c: set() for c in columns}
    for rule in rules:
        dependency = rule_dependencies(rule)
        if dependency is None:
            continue
        target, reads = dependency
        if target not in colset:
            continue
        for read in reads:
            if read in colset and read != target:
                deps[target].add(read)

    indegree = {c: len(deps[c]) for c in columns}
    dependents: dict[str, list[str]] = {c: [] for c in columns}
    for target, prerequisites in deps.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].append(target)

    # Kahn con un min-heap: entre las columnas ya disponibles se elige siempre la
    # alfabéticamente menor, lo que hace el orden totalmente determinista.
    ready = [c for c in columns if indegree[c] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        column = heapq.heappop(ready)
        order.append(column)
        for dependent in sorted(dependents[column]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(order) != len(columns):
        cycle = _find_cycle(deps)
        raise PlanError(
            f"tabla {table_plan.table}: las reglas forman un ciclo de dependencias entre "
            f"columnas ({' -> '.join(cycle)}); no hay un orden de generación válido. "
            "Rompe el ciclo reescribiendo una de esas reglas (p. ej. como aserción, que "
            "se comprueba tras generar y no impone orden)."
        )
    return order


def _find_cycle(deps: Mapping[str, set[str]]) -> list[str]:
    """Encuentra un ciclo en el grafo de dependencias `columna → prerrequisitos`.

    DFS con colores; recorre los vecinos en orden alfabético para que el ciclo
    reportado sea determinista. Devuelve el ciclo como lista cerrada
    (`[a, b, a]`), o vacío si no hay (no debería ocurrir cuando se le llama).
    """
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(deps, white)
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        path.append(node)
        for nxt in sorted(deps[node]):
            if color[nxt] == grey:
                return path[path.index(nxt) :] + [nxt]
            if color[nxt] == white:
                found = visit(nxt)
                if found is not None:
                    return found
        color[node] = black
        path.pop()
        return None

    for start in sorted(deps):
        if color[start] == white:
            found = visit(start)
            if found is not None:
                return found
    return []
