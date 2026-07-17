"""Grafo de dependencias, SCC y fases (T1.6, especificacion.md §6.1 y §6.4).

`analyze_structure` construye el grafo dirigido hijo→padre a partir de las
FK del esquema, calcula sus componentes fuertemente conexos (Tarjan, vía
networkx) y los organiza en fases topológicas fusionando en la misma fase
las tablas independientes entre sí. De paso completa los dos campos
derivados que le corresponden a este módulo (especificacion.md §5): rellena
`TableSpec.kind` y `RelationshipSpec.cardinality_hint` **mutando `spec` in
situ** (ambos campos están excluidos del hash canónico, `ir/hashing.py`).
No decide todavía cómo romper un ciclo ni una autorreferencia: eso es
`graph/strategies.py::resolve_cycles` (T1.7), que consume el
`StructuralPlan` que este módulo produce.
"""

from __future__ import annotations

import networkx as nx

from synthdb.ir.plans import StructuralPlan
from synthdb.ir.schema import SchemaSpec, TableKind, TableSpec


def index_tables(spec: SchemaSpec) -> dict[str, TableSpec]:
    """Indexa las tablas del esquema por los nombres con los que una FK puede referenciarlas.

    Cada tabla queda indexada por su `name` desnudo y, si declara un
    `schema` explícito, también por `"{schema}.{name}"` — el formato que usa
    `RelationshipSpec.ref_table` cuando el DDL declaró el namespace
    (parsing/ddl.py, T1.3).

    Args:
        spec: Esquema ya parseado.

    Returns:
        Mapa de nombre (desnudo o cualificado) a `TableSpec`.
    """
    index: dict[str, TableSpec] = {}
    for table in spec.tables:
        index[table.name] = table
        if table.schema_:
            index[f"{table.schema_}.{table.name}"] = table
    return index


def phase_layers(g: nx.DiGraph) -> list[list[str]]:
    """Agrupa los nodos de `g` en fases de dependencia, padres antes que hijos.

    Un componente fuertemente conexo (ciclo real de 2+ nodos) ocupa una
    única fase con todos sus miembros; los nodos sin dependencia entre sí se
    fusionan en la misma fase (la más temprana que sus propias dependencias
    permitan). Cada fase se devuelve ordenada alfabéticamente para que el
    resultado sea determinista con independencia del orden de iteración
    interno de `networkx` (o de `PYTHONHASHSEED`): la fase de un componente
    depende solo de la estructura del grafo (máximo de las fases de sus
    dependencias), nunca del orden en que se visiten.

    Args:
        g: Grafo dirigido con aristas hijo→padre (`u` depende de `v`).

    Returns:
        Fases en orden de dependencia; cada una, sus nodos en orden alfabético.
    """
    condensation = nx.condensation(g)
    parent_first = list(reversed(list(nx.topological_sort(condensation))))

    phase_index: dict[int, int] = {}
    for scc_id in parent_first:
        deps_phases = [phase_index[succ] for succ in condensation.successors(scc_id)]
        phase_index[scc_id] = (max(deps_phases) + 1) if deps_phases else 0

    max_phase = max(phase_index.values(), default=-1)
    return [
        sorted(
            name
            for scc_id, index in phase_index.items()
            if index == level
            for name in condensation.nodes[scc_id]["members"]
        )
        for level in range(max_phase + 1)
    ]


def _is_one_to_one(table: TableSpec, fk_columns: list[str]) -> bool:
    """`True` si un UNIQUE (o la PK) de `table` cae exactamente sobre `fk_columns`."""
    target = set(fk_columns)
    if table.primary_key and set(table.primary_key) == target:
        return True
    return any(set(unique) == target for unique in table.uniques)


def _is_bridge(table: TableSpec) -> bool:
    """`True` si la PK (o un UNIQUE) de `table` la forman columnas de ≥2 FK distintas.

    Además, la tabla debe tener a lo sumo 2 columnas que no pertenezcan a
    ninguna FK (especificacion.md §6.4): una tabla puente típica no aporta
    apenas atributos propios más allá del par de claves foráneas.
    """
    if len(table.foreign_keys) < 2:
        return False

    fk_columns = {column for fk in table.foreign_keys for column in fk.columns}
    non_fk_columns = [column.name for column in table.columns if column.name not in fk_columns]
    if len(non_fk_columns) > 2:
        return False

    for key in (table.primary_key, *table.uniques):
        if not key or not set(key).issubset(fk_columns):
            continue
        contributing_fks = {
            index for index, fk in enumerate(table.foreign_keys) if set(fk.columns) & set(key)
        }
        if len(contributing_fks) >= 2:
            return True
    return False


def _infer_kind(table: TableSpec, referenced: set[str]) -> TableKind:
    """Deriva `TableSpec.kind` (especificacion.md §6.4): `bridge`, `lookup` o `regular`."""
    if _is_bridge(table):
        return "bridge"
    if not table.foreign_keys and len(table.columns) <= 3 and table.name in referenced:
        return "lookup"
    return "regular"


def analyze_structure(spec: SchemaSpec) -> StructuralPlan:
    """Construye el grafo de dependencias del esquema y lo organiza en fases.

    Efecto secundario deliberado: rellena `TableSpec.kind` y
    `RelationshipSpec.cardinality_hint` mutando las tablas de `spec` (ambos
    excluidos del hash canónico, así que `schema_hash(spec)` no cambia antes
    y después de esta llamada). Una FK cuya tabla referenciada no existe en
    el esquema no añade arista al grafo: se registra como aviso en
    `StructuralPlan.warnings` y se ignora para la planificación de fases.

    Args:
        spec: Esquema ya parseado (`parsing/ddl.py`). Se muta in situ.

    Returns:
        El `StructuralPlan` con las fases, ciclos, autorreferencias y
        tablas puente detectados.
    """
    by_name = index_tables(spec)
    warnings: list[str] = []

    g: nx.DiGraph = nx.DiGraph()
    for table in spec.tables:
        g.add_node(table.name)

    self_refs: set[str] = set()
    for table in spec.tables:
        for fk in table.foreign_keys:
            target = by_name.get(fk.ref_table)
            if target is None:
                warnings.append(
                    f"tabla {table.name}: la FK ({', '.join(fk.columns)}) referencia "
                    f"{fk.ref_table!r}, que no existe en el esquema; se ignora para "
                    "la planificación de fases"
                )
                continue
            if target.name == table.name:
                self_refs.add(table.name)
                continue
            g.add_edge(table.name, target.name)

    referenced = {
        target.name
        for table in spec.tables
        for fk in table.foreign_keys
        if (target := by_name.get(fk.ref_table)) is not None
    }

    for table in spec.tables:
        table.kind = _infer_kind(table, referenced)
        for fk in table.foreign_keys:
            target = by_name.get(fk.ref_table)
            if target is None:
                continue
            if target.name == table.name:
                fk.cardinality_hint = "self_reference"
            elif _is_one_to_one(table, fk.columns):
                fk.cardinality_hint = "one_to_one"
            else:
                fk.cardinality_hint = "many_to_one"

    sccs = sorted(
        (
            sorted(component)
            for component in nx.strongly_connected_components(g)
            if len(component) > 1
        ),
        key=lambda members: members[0],
    )
    bridges = sorted(table.name for table in spec.tables if table.kind == "bridge")

    return StructuralPlan(
        tables_by_phase=phase_layers(g),
        sccs=sccs,
        self_refs=sorted(self_refs),
        bridges=bridges,
        warnings=warnings,
    )
