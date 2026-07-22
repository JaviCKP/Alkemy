"""Regresiones del selector de FKs compartidas (revisión adversarial del PR #45).

Cubre los tres hallazgos publicados sobre la selección coordinada de FKs que
comparten columnas (típicamente un discriminador ``tenant_id``):

1. **Puente multi-tenant.** La deduplicación de una tabla puente no puede
   sustituir un índice aislado: debe buscar un par completo compatible con los
   valores compartidos, o fallar de forma accionable.
2. **Cuota incumplida en silencio.** Una asignación de cuota incompatible con
   las FKs compartidas no puede reemplazarse por un padre aleatorio: se reasigna
   de forma determinista si cabe en ``min/max`` o se falla con error accionable.
3. **Complejidad O(n²).** El filtrado de candidatos por las FKs obligatorias
   restantes no puede recorrer todos los padres por cada fila hija.

Los esquemas se declaran en línea (como en ``test_engine.py``) para no depender
de fixtures nuevos ni de snapshots.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest

from synthdb.config.models import (
    ColumnConfig,
    Config,
    FkQuota,
    FkUniqueSubset,
    FkZipf,
    OutputConfig,
    TableConfig,
)
from synthdb.generation import engine
from synthdb.generation.engine import Dataset, GenerationError, generate_dataset
from synthdb.parsing.ddl import parse_ddl


def _digest(dataset: Dataset) -> str:
    payload = json.dumps(dataset.tables, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _seq(start: int) -> ColumnConfig:
    return ColumnConfig(generator="sequence", params={"start": start})


# --- Hallazgo 1: puente multi-tenant -----------------------------------------

_BRIDGE_SCHEMA = """
CREATE TABLE lefts (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE rights (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE links (
    tenant_id INT NOT NULL,
    left_id INT NOT NULL,
    right_id INT NOT NULL,
    PRIMARY KEY (tenant_id, left_id, right_id),
    FOREIGN KEY (tenant_id, left_id) REFERENCES lefts(tenant_id, id),
    FOREIGN KEY (tenant_id, right_id) REFERENCES rights(tenant_id, id)
);
"""


def _bridge_config(seed: int, batch_size: int, links_rows: int = 2) -> Config:
    # Dos tenants (1, 2), un padre por lado y tenant => exactamente dos
    # combinaciones válidas: (t1: left 100, right 300) y (t2: left 101, right 301).
    return Config(
        seed=seed,
        tables={
            "lefts": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(100)}),
            "rights": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(300)}),
            "links": TableConfig(rows=links_rows),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", [1, 4, 5, 10, 11, 12, 18, 19])
def test_multitenant_bridge_dedup_keeps_shared_tenant(seed: int) -> None:
    spec = parse_ddl(_BRIDGE_SCHEMA)
    small = generate_dataset(spec, _bridge_config(seed, batch_size=1))
    large = generate_dataset(spec, _bridge_config(seed, batch_size=5000))

    for dataset in (small, large):
        assert dataset.quarantine == {}
        links = dataset.tables["links"]
        assert len(links) == 2
        pairs = {(row["left_id"], row["right_id"]) for row in links}
        assert len(pairs) == 2  # dos pares únicos
        lefts_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["lefts"]}
        rights_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["rights"]}
        for row in links:
            assert (row["tenant_id"], row["left_id"]) in lefts_keys  # FK izquierda válida
            assert (row["tenant_id"], row["right_id"]) in rights_keys  # FK derecha válida
        # Tenant coherente: las dos únicas combinaciones cubren ambos tenants.
        assert {row["tenant_id"] for row in links} == {1, 2}

    assert _digest(small) == _digest(large)  # mismo resultado con batch_size distinto


def test_multitenant_bridge_without_enough_pairs_fails_actionably() -> None:
    spec = parse_ddl(_BRIDGE_SCHEMA)
    # Solo hay dos combinaciones compatibles pero se piden tres filas.
    config = _bridge_config(seed=1, batch_size=5000, links_rows=3)
    with pytest.raises(GenerationError, match=r"puente links.*3.*2|links.*combinaciones"):
        generate_dataset(spec, config)


# --- Hallazgo 2: cuota compartida --------------------------------------------

_QUOTA_SCHEMA = """
CREATE TABLE operations (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE customers (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE ops_children (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    operation_id INT NOT NULL,
    customer_id INT NOT NULL,
    FOREIGN KEY (tenant_id, operation_id) REFERENCES operations(tenant_id, id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
);
"""


def _quota_config(*, customers: TableConfig, batch_size: int = 5000) -> Config:
    return Config(
        seed=7,
        tables={
            "operations": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "customers": customers,
            "ops_children": TableConfig(
                rows=2,
                columns={"id": _seq(100)},
                fk={"operation_id": FkQuota(strategy="quota", min=1, max=1)},
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("batch_size", [1, 5000])
def test_shared_quota_is_honored_exactly_when_compatible(batch_size: int) -> None:
    spec = parse_ddl(_QUOTA_SCHEMA)
    # Un cliente por tenant => la cuota min=1,max=1 es factible.
    customers = TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)})
    dataset = generate_dataset(spec, _quota_config(customers=customers, batch_size=batch_size))

    assert dataset.quarantine == {}
    children = dataset.tables["ops_children"]
    assert len(children) == 2
    counts = Counter((row["tenant_id"], row["operation_id"]) for row in children)
    # Cada padre recibe exactamente un hijo (cuota respetada).
    assert counts == {(1, 10): 1, (2, 11): 1}
    customer_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["customers"]}
    for row in children:
        assert (row["tenant_id"], row["customer_id"]) in customer_keys


def test_shared_quota_incompatible_with_shared_fk_fails_actionably() -> None:
    spec = parse_ddl(_QUOTA_SCHEMA)
    # Solo existe cliente para el tenant 2: la operación del tenant 1 no tiene
    # ninguna combinación compatible, pero la cuota min=1 exige darle un hijo.
    customers = TableConfig(
        rows=1,
        columns={
            "tenant_id": ColumnConfig(generator="choice", params={"values": [2]}),
            "id": _seq(20),
        },
    )
    with pytest.raises(GenerationError, match=r"ops_children.*operation.*cuota|cuota.*compatible"):
        generate_dataset(spec, _quota_config(customers=customers))


# --- Contratos de estrategia sobre FKs compartidas ---------------------------

_STRATEGY_SCHEMA = """
CREATE TABLE operations (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE customers (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE ops_children (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    operation_id INT NOT NULL,
    customer_id INT NOT NULL,
    FOREIGN KEY (tenant_id, operation_id) REFERENCES operations(tenant_id, id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
);
"""


def _strategy_config(fk_operation: object, *, children: int, batch_size: int) -> Config:
    tables: dict[str, TableConfig] = {
        "operations": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(10)}),
        "customers": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(20)}),
        "ops_children": TableConfig(rows=children, columns={"id": _seq(100)}),
    }
    if fk_operation is not None:
        tables["ops_children"] = tables["ops_children"].model_copy(
            update={"fk": {"operation_id": fk_operation}}
        )
    return Config(seed=3, tables=tables, output=OutputConfig(batch_size=batch_size))


@pytest.mark.parametrize(
    ("fk_operation", "children"),
    [
        (FkZipf(strategy="zipf", s=1.3), 12),
        (FkUniqueSubset(strategy="unique_subset"), 4),
        (None, 12),  # uniform por defecto
    ],
)
def test_shared_fk_strategies_stay_coherent(fk_operation: object, children: int) -> None:
    spec = parse_ddl(_STRATEGY_SCHEMA)
    small = _strategy_config(fk_operation, children=children, batch_size=1)
    large = _strategy_config(fk_operation, children=children, batch_size=5000)
    ds_small = generate_dataset(spec, small)
    ds_large = generate_dataset(spec, large)

    for dataset in (ds_small, ds_large):
        assert dataset.quarantine == {}
        operation_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["operations"]}
        customer_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["customers"]}
        rows = dataset.tables["ops_children"]
        for row in rows:
            assert (row["tenant_id"], row["operation_id"]) in operation_keys
            assert (row["tenant_id"], row["customer_id"]) in customer_keys
        if isinstance(fk_operation, FkUniqueSubset):
            used = [(row["tenant_id"], row["operation_id"]) for row in rows]
            assert len(used) == len(set(used))  # 1:1 sin repetir padre

    assert _digest(ds_small) == _digest(ds_large)  # determinista con distinto batch_size


# --- Hallazgo 3: complejidad de la selección compartida ----------------------

_PROBE_SCHEMA = """
CREATE TABLE parents_a (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE parents_b (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE child (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    a_id INT NOT NULL,
    b_id INT NOT NULL,
    FOREIGN KEY (tenant_id, a_id) REFERENCES parents_a(tenant_id, id),
    FOREIGN KEY (tenant_id, b_id) REFERENCES parents_b(tenant_id, id)
);
"""


def _probe(n: int) -> tuple[int, Dataset]:
    spec = parse_ddl(_PROBE_SCHEMA)
    config = Config(
        seed=0,
        tables={
            "parents_a": TableConfig(rows=n, columns={"tenant_id": _seq(1), "id": _seq(1)}),
            "parents_b": TableConfig(rows=n, columns={"tenant_id": _seq(1), "id": _seq(1)}),
            "child": TableConfig(rows=n, columns={"id": _seq(1)}),
        },
    )
    dataset = generate_dataset(spec, config)
    return engine.filter_scan_count(), dataset


def test_shared_fk_selection_scales_linearly() -> None:
    # Sonda con dos tablas padre alineadas por tenant y una hija con dos FKs
    # compuestas que comparten tenant_id. El número de candidatos examinados al
    # filtrar por FKs obligatorias debe crecer lineal, no cuadráticamente: al
    # multiplicar N por 4, un algoritmo O(n²) multiplica el trabajo por ~16.
    scans_small, ds_small = _probe(200)
    scans_big, ds_big = _probe(800)

    assert ds_small.quarantine == {} and ds_big.quarantine == {}
    assert scans_small > 0
    # Cota lineal absoluta (holgada) y de crecimiento: O(n²) la rompe con claridad.
    assert scans_big <= 40 * 800
    assert scans_big <= 8 * scans_small
