"""Estrategias de generación para ciclos y autorreferencias (T1.7, especificacion.md §6.2-6.3).

`resolve_cycles` consume el `StructuralPlan` de `graph/dependency.py` (T1.6)
y expande cada una de sus fases estructurales en la secuencia final y
concreta de `Phase` (`ir/plans.py`) que ejecutará el motor de generación:
inserción simple, autorreferencia por niveles, ciclo roto por FK anulable
(`InsertPhase` con `null_fks` + `UpdatePhase`) o diferido (`DeferredPhase`).
Cuando ninguna estrategia es posible sin modificar el DDL, levanta
`UnbreakableCycle` con un diagnóstico accionable en vez de inventar datos o
desactivar constraints (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from synthdb.graph.dependency import index_tables, phase_layers
from synthdb.ir.plans import (
    DeferredPhase,
    FkRef,
    InsertLeveledPhase,
    InsertPhase,
    Phase,
    StructuralPlan,
    UpdatePhase,
)
from synthdb.ir.schema import RelationshipSpec, SchemaSpec, TableSpec


class UnbreakableCycle(Exception):
    """Ciclo entre tablas sin ninguna FK anulable ni diferible (especificacion.md §6.2, opción 3).

    `tables` y `edges` quedan disponibles en la excepción para que quien la
    capture (p. ej. la CLI) pueda componer su propio informe además del
    mensaje ya legible de `str(exc)`.
    """

    def __init__(self, tables: list[str], edges: list[FkRef]) -> None:
        self.tables = tables
        self.edges = edges
        edges_desc = "; ".join(
            f"{edge.table}({', '.join(edge.columns)}) → {edge.ref_table}" for edge in edges
        )
        super().__init__(
            f"Ciclo irrompible entre las tablas {', '.join(tables)}: las FK implicadas "
            f"({edges_desc}) son todas NOT NULL y ninguna es DEFERRABLE. Opciones: "
            "(1) marcar alguna de estas FK como anulable; (2) marcarla DEFERRABLE "
            "INITIALLY DEFERRED; (3) usar --allow-ddl para desactivar y reactivar la "
            "constraint durante la carga (desaconsejado; nunca es el comportamiento "
            "por defecto)."
        )


@dataclass
class _Edge:
    """FK interna a un ciclo, ya resuelta contra las tablas que lo componen."""

    table: str
    columns: list[str]
    ref_table: str
    nullable_columns: list[str]
    match_full: bool
    deferrable: bool


def _edges_within(members: set[str], by_name: dict[str, TableSpec]) -> list[_Edge]:
    """FK cuyo origen y destino están ambos dentro de `members`, ordenadas deterministamente.

    Desempate siempre por orden alfabético de `(tabla, primera columna)`
    (CLAUDE.md: determinismo total ante empates).
    """
    edges: list[_Edge] = []
    for table_name in members:
        table = by_name[table_name]
        for fk in table.foreign_keys:
            target = by_name.get(fk.ref_table)
            if target is None or target.name == table.name or target.name not in members:
                continue
            edges.append(
                _Edge(
                    table=table.name,
                    columns=fk.columns,
                    ref_table=target.name,
                    nullable_columns=fk.nullable_columns,
                    match_full=fk.match_full,
                    deferrable=fk.deferrable,
                )
            )
    return sorted(edges, key=lambda edge: (edge.table, edge.columns[0]))


def _null_break_columns(edge: _Edge) -> list[str]:
    """Columnas a anular para romper el ciclo por esta FK, o `[]` si no es posible.

    Nulabilidad dirigida (ADR-004): bajo `MATCH SIMPLE` (defecto) basta con que
    la FK tenga alguna columna anulable —se anulan esas y el resto se inserta
    con su valor real—; bajo `MATCH FULL` un NULL parcial viola la restricción,
    así que solo es rompible si TODAS sus columnas admiten NULL.
    """
    if not edge.nullable_columns:
        return []
    if edge.match_full and len(edge.nullable_columns) != len(edge.columns):
        return []
    return edge.nullable_columns


def _resolve_cycle(members: list[str], by_name: dict[str, TableSpec]) -> list[Phase]:
    """Rompe un ciclo real de 2+ tablas siguiendo especificacion.md §6.2 y ADR-004."""
    edges = _edges_within(set(members), by_name)

    for edge in edges:
        null_columns = _null_break_columns(edge)
        if not null_columns:
            continue
        remainder: nx.DiGraph = nx.DiGraph()
        remainder.add_nodes_from(members)
        for other in edges:
            if other is edge:
                continue
            remainder.add_edge(other.table, other.ref_table)
        inner_order = [name for layer in phase_layers(remainder) for name in layer]

        insert = InsertPhase(
            tables=inner_order,
            null_fks=[
                FkRef(
                    table=edge.table,
                    columns=edge.columns,
                    ref_table=edge.ref_table,
                    null_columns=null_columns,
                )
            ],
        )
        update = UpdatePhase(table=edge.table, columns=null_columns)
        return [insert, update]

    deferrable_edge = next((edge for edge in edges if edge.deferrable), None)
    if deferrable_edge is not None:
        return [DeferredPhase(tables=sorted(members))]

    raise UnbreakableCycle(
        tables=sorted(members),
        edges=[
            FkRef(table=edge.table, columns=edge.columns, ref_table=edge.ref_table)
            for edge in edges
        ],
    )


def _self_reference_fk(table: TableSpec, by_name: dict[str, TableSpec]) -> RelationshipSpec:
    """La FK de autorreferencia de `table` (si hay varias, la primera por columnas)."""
    candidates = sorted(
        (
            fk
            for fk in table.foreign_keys
            if (target := by_name.get(fk.ref_table)) is not None and target.name == table.name
        ),
        key=lambda fk: fk.columns[0],
    )
    return candidates[0]


def _resolve_self_reference(
    table_name: str, by_name: dict[str, TableSpec], plan: StructuralPlan
) -> list[Phase]:
    """Estrategia de autorreferencia por niveles (especificacion.md §6.3)."""
    fk = _self_reference_fk(by_name[table_name], by_name)

    if fk.nullable_columns and (not fk.match_full or len(fk.nullable_columns) == len(fk.columns)):
        return [InsertLeveledPhase(table=table_name, self_fk_columns=fk.columns)]
    if fk.deferrable:
        return [DeferredPhase(tables=[table_name])]

    plan.warnings.append(
        f"tabla {table_name}: la FK de autorreferencia ({', '.join(fk.columns)}) es "
        "NOT NULL y no diferible; las filas raíz se referenciarán a sí mismas "
        "(roots_point_to_self) para poder insertarse sin modificar el DDL."
    )
    return [
        InsertLeveledPhase(table=table_name, self_fk_columns=fk.columns, roots_point_to_self=True)
    ]


def resolve_cycles(plan: StructuralPlan, spec: SchemaSpec) -> list[Phase]:
    """Expande cada fase estructural de `plan` en la secuencia final de `Phase`.

    Dentro de cada fase, las tablas se agrupan en unidades — un ciclo real
    (`plan.sccs`), una autorreferencia (`plan.self_refs`) o el resto de
    tablas independientes fusionadas en un único `InsertPhase` — y esas
    unidades se emiten ordenadas alfabéticamente por su tabla más temprana,
    para determinismo total ante empates (CLAUDE.md). Puede añadir avisos a
    `plan.warnings` (p. ej. autorreferencia NOT NULL no diferible).

    Args:
        plan: Salida de `graph/dependency.py::analyze_structure` (T1.6).
        spec: El mismo esquema ya analizado (con `kind`/`cardinality_hint`
            ya rellenados por T1.6, aunque este módulo solo necesita
            `nullable_columns`/`match_full`/`deferrable`/`columns` de cada
            `RelationshipSpec`).

    Returns:
        La secuencia ordenada de fases de ejecución.

    Raises:
        UnbreakableCycle: si un ciclo no tiene ninguna FK anulable ni
            diferible (especificacion.md §6.2, opción 3).
    """
    by_name = index_tables(spec)

    phases: list[Phase] = []
    for phase_tables in plan.tables_by_phase:
        remaining = set(phase_tables)
        units: list[tuple[str, list[Phase]]] = []

        for scc in plan.sccs:
            scc_members = set(scc)
            if scc_members <= remaining:
                units.append((scc[0], _resolve_cycle(scc, by_name)))
                remaining -= scc_members

        for table_name in plan.self_refs:
            if table_name in remaining:
                units.append((table_name, _resolve_self_reference(table_name, by_name, plan)))
                remaining.discard(table_name)

        if remaining:
            plain = sorted(remaining)
            units.append((plain[0], [InsertPhase(tables=plain)]))

        for _, unit_phases in sorted(units, key=lambda unit: unit[0]):
            phases.extend(unit_phases)

    return phases
