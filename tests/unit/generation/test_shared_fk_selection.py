"""Regresiones del selector de FKs compartidas (revisión adversarial del PR #45).

Cubre los tres hallazgos publicados sobre la selección coordinada de FKs que
comparten columnas (típicamente un discriminador ``tenant_id``):

1. **Puente multi-tenant.** La asignación de una tabla puente no puede
   sustituir un índice aislado: debe construir un par completo compatible con los
   valores compartidos, o fallar de forma accionable.
2. **Cuota incumplida en silencio.** Una asignación de cuota incompatible con
   las FKs compartidas no puede reemplazarse por un padre aleatorio: se reasigna
   de forma determinista si cabe en ``min/max`` o se falla con error accionable.
3. **Complejidad O(n²).** La asignación conjunta y sus índices de soporte no
   pueden recorrer todos los padres por cada fila hija.

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
    FkUniform,
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
def test_multitenant_bridge_assignment_keeps_shared_tenant(seed: int) -> None:
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
    # `_SELECTION_WORK` es instrumentación privada de tests (no API pública de engine).
    return engine._SELECTION_WORK.filter_scans, dataset


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


# --- Bloqueante 1 (revisión de d86e249): dos cuotas compartidas coordinadas ----

_TWO_QUOTA_SCHEMA = """
CREATE TABLE a (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE b (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE c (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    a_id INT NOT NULL,
    b_id INT NOT NULL,
    FOREIGN KEY (tenant_id, a_id) REFERENCES a(tenant_id, id),
    FOREIGN KEY (tenant_id, b_id) REFERENCES b(tenant_id, id)
);
"""


def _two_quota_config(seed: int, batch_size: int) -> Config:
    quota = FkQuota(strategy="quota", min=1, max=1)
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
            "c": TableConfig(
                rows=2,
                columns={"id": _seq(100)},
                fk={"a_id": quota, "b_id": quota},
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", list(range(20)))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_two_shared_quotas_are_jointly_satisfied(seed: int, batch_size: int) -> None:
    # Dos FKs compuestas que comparten tenant_id, ambas quota(min=1,max=1), un
    # padre por tenant en cada lado y dos hijos: la solución conjunta existe (cada
    # padre recibe exactamente un hijo). Con d86e249 falla en la mitad de las
    # semillas porque cada vector de cuota se barajaba por separado.
    dataset = generate_dataset(parse_ddl(_TWO_QUOTA_SCHEMA), _two_quota_config(seed, batch_size))

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    assert len(rows) == 2
    a_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["a"]}
    b_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["b"]}
    for row in rows:
        assert (row["tenant_id"], row["a_id"]) in a_keys  # RI FK a
        assert (row["tenant_id"], row["b_id"]) in b_keys  # RI FK b
    a_counts = Counter((row["tenant_id"], row["a_id"]) for row in rows)
    b_counts = Counter((row["tenant_id"], row["b_id"]) for row in rows)
    # Conteo exacto: cada padre de ambas tablas recibe exactamente un hijo.
    assert dict(a_counts) == {(1, 10): 1, (2, 11): 1}
    assert dict(b_counts) == {(1, 20): 1, (2, 21): 1}


def test_two_shared_quotas_determinism_across_batch_size() -> None:
    spec = parse_ddl(_TWO_QUOTA_SCHEMA)
    small = generate_dataset(spec, _two_quota_config(seed=4, batch_size=1))
    large = generate_dataset(spec, _two_quota_config(seed=4, batch_size=5000))
    assert _digest(small) == _digest(large)


def test_two_shared_quotas_truly_infeasible_fails_actionably() -> None:
    # `a` tiene padres para los tenants 1 y 2; `b` solo para el tenant 1. Con
    # ambas quota(min=1,max=1) el padre de `a` del tenant 2 exige un hijo que
    # ninguna asignación conjunta puede darle (no hay `b` para el tenant 2).
    spec = parse_ddl(_TWO_QUOTA_SCHEMA)
    quota = FkQuota(strategy="quota", min=1, max=1)
    config = Config(
        seed=0,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(
                rows=1,
                columns={
                    "tenant_id": ColumnConfig(generator="choice", params={"values": [1]}),
                    "id": _seq(20),
                },
            ),
            "c": TableConfig(rows=2, columns={"id": _seq(100)}, fk={"a_id": quota, "b_id": quota}),
        },
    )
    with pytest.raises(GenerationError, match=r"tabla c.*cuota"):
        generate_dataset(spec, config)


def test_nullable_shared_fk_with_null_ratio_keeps_its_contract() -> None:
    # `null_ratio` sobre una FK compartida anulable: unas filas quedan a NULL en
    # esa relación sin romper el tenant compartido ni la otra FK obligatoria.
    spec = parse_ddl(
        """
        CREATE TABLE operations (
            tenant_id INT NOT NULL, id INT NOT NULL, PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE customers (
            tenant_id INT NOT NULL, id INT NOT NULL, PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE ops_children (
            id INT PRIMARY KEY,
            tenant_id INT NOT NULL,
            operation_id INT NOT NULL,
            customer_id INT,
            FOREIGN KEY (tenant_id, operation_id) REFERENCES operations(tenant_id, id),
            FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
        );
        """
    )

    def config(batch_size: int) -> Config:
        return Config(
            seed=7,
            tables={
                "operations": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(10)}),
                "customers": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(20)}),
                "ops_children": TableConfig(
                    rows=40,
                    columns={"id": _seq(100)},
                    fk={"customer_id": FkUniform(strategy="uniform", null_ratio=0.5)},
                ),
            },
            output=OutputConfig(batch_size=batch_size),
        )

    small = generate_dataset(spec, config(1))
    large = generate_dataset(spec, config(5000))
    assert small.quarantine == {}
    operation_keys = {(row["tenant_id"], row["id"]) for row in small.tables["operations"]}
    customer_keys = {(row["tenant_id"], row["id"]) for row in small.tables["customers"]}
    rows = small.tables["ops_children"]
    assert any(row["customer_id"] is None for row in rows)  # null_ratio surtió efecto
    assert any(row["customer_id"] is not None for row in rows)
    for row in rows:
        assert (row["tenant_id"], row["operation_id"]) in operation_keys  # obligatoria intacta
        if row["customer_id"] is not None:
            assert (row["tenant_id"], row["customer_id"]) in customer_keys  # tenant coherente
    assert _digest(small) == _digest(large)


# --- Bloqueante 2 (revisión de d86e249): coste de la asignación del puente ---

_BRIDGE_PROBE_SCHEMA = """
CREATE TABLE lefts (
    tenant_id INT NOT NULL, id INT NOT NULL, PRIMARY KEY (tenant_id, id)
);
CREATE TABLE rights (
    tenant_id INT NOT NULL, id INT NOT NULL, PRIMARY KEY (tenant_id, id)
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


def _bridge_probe(n: int) -> tuple[int, Dataset]:
    spec = parse_ddl(_BRIDGE_PROBE_SCHEMA)
    config = Config(
        seed=1,
        tables={
            "lefts": TableConfig(rows=n, columns={"tenant_id": _seq(1), "id": _seq(1)}),
            "rights": TableConfig(rows=n, columns={"tenant_id": _seq(1), "id": _seq(1)}),
            "links": TableConfig(rows=n),
        },
    )
    dataset = generate_dataset(spec, config)
    return engine._SELECTION_WORK.bridge_pairs_examined, dataset


def test_bridge_assignment_scales_linearly() -> None:
    # Puente multi-tenant alineado (un padre por lado y tenant, `links=n`): la
    # asignación no puede reconstruir el producto cartesiano por cada colisión.
    # El trabajo estructural (pares examinados por el asignador) debe crecer
    # lineal; un O(n²) al cuadruplicar n lo multiplica por ~16.
    work_small, ds_small = _bridge_probe(800)
    work_big, ds_big = _bridge_probe(3200)

    for dataset in (ds_small, ds_big):
        assert dataset.quarantine == {}
        links = dataset.tables["links"]
        pairs = {(row["left_id"], row["right_id"]) for row in links}
        assert len(pairs) == len(links)  # pares únicos
        left_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["lefts"]}
        right_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["rights"]}
        for row in links:
            assert (row["tenant_id"], row["left_id"]) in left_keys
            assert (row["tenant_id"], row["right_id"]) in right_keys
    assert work_small > 0
    # 4× n ⇒ ~4× trabajo (lineal). Cota holgada que un O(n²) (~16×) rompe.
    assert work_big <= 8 * work_small
    assert work_big <= 40 * 3200


# --- Revisión adversarial de 17e660b: asignación conjunta de tabla ------------

_NULLABLE_TWO_QUOTA_SCHEMA = """
CREATE TABLE a (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE b (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE c (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    a_id INT,
    b_id INT,
    FOREIGN KEY (tenant_id, a_id) REFERENCES a(tenant_id, id),
    FOREIGN KEY (tenant_id, b_id) REFERENCES b(tenant_id, id)
);
"""


def _nullable_two_quota_config(
    seed: int,
    batch_size: int,
    *,
    a_null_ratio: float = 0.5,
    b_null_ratio: float = 0.5,
) -> Config:
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
            "c": TableConfig(
                rows=20,
                columns={"id": _seq(100)},
                fk={
                    "a_id": FkQuota(strategy="quota", min=0, max=20, null_ratio=a_null_ratio),
                    "b_id": FkQuota(strategy="quota", min=0, max=20, null_ratio=b_null_ratio),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_nullable_shared_quotas_allow_independent_null_patterns(seed: int, batch_size: int) -> None:
    spec = parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA)
    dataset = generate_dataset(spec, _nullable_two_quota_config(seed, batch_size))

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    a_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["a"]}
    b_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["b"]}
    a_counts = Counter((row["tenant_id"], row["a_id"]) for row in rows if row["a_id"] is not None)
    b_counts = Counter((row["tenant_id"], row["b_id"]) for row in rows if row["b_id"] is not None)

    assert len(rows) == 20
    assert any((row["a_id"] is None) != (row["b_id"] is None) for row in rows)
    assert sum(a_counts.values()) == sum(row["a_id"] is not None for row in rows)
    assert sum(b_counts.values()) == sum(row["b_id"] is not None for row in rows)
    assert all(count <= 20 for count in a_counts.values())
    assert all(count <= 20 for count in b_counts.values())
    for row in rows:
        assert row["tenant_id"] is not None
        if row["a_id"] is not None:
            assert (row["tenant_id"], row["a_id"]) in a_keys
        if row["b_id"] is not None:
            assert (row["tenant_id"], row["b_id"]) in b_keys


def test_nullable_shared_quotas_can_have_different_null_ratios() -> None:
    spec = parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA)
    dataset = generate_dataset(
        spec,
        _nullable_two_quota_config(seed=0, batch_size=5000, a_null_ratio=0.0, b_null_ratio=1.0),
    )

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    assert all(row["a_id"] is not None for row in rows)
    assert all(row["b_id"] is None for row in rows)
    assert {row["tenant_id"] for row in rows} == {1, 2}


_COMPONENT_QUOTA_SCHEMA = """
CREATE TABLE a (
    x INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (x, id)
);
CREATE TABLE b (
    x INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (x, id)
);
CREATE TABLE d (
    y INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (y, id)
);
CREATE TABLE e (
    y INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (y, id)
);
CREATE TABLE c (
    id INT PRIMARY KEY,
    x INT NOT NULL,
    a_id INT NOT NULL,
    b_id INT NOT NULL,
    y INT NOT NULL,
    d_id INT NOT NULL,
    e_id INT NOT NULL,
    FOREIGN KEY (x, a_id) REFERENCES a(x, id),
    FOREIGN KEY (x, b_id) REFERENCES b(x, id),
    FOREIGN KEY (y, d_id) REFERENCES d(y, id),
    FOREIGN KEY (y, e_id) REFERENCES e(y, id)
);
"""


def _component_quota_config(seed: int, batch_size: int) -> Config:
    quota = FkQuota(strategy="quota", min=1, max=1)
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"x": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"x": _seq(1), "id": _seq(20)}),
            "d": TableConfig(rows=2, columns={"y": _seq(1), "id": _seq(30)}),
            "e": TableConfig(rows=2, columns={"y": _seq(1), "id": _seq(40)}),
            "c": TableConfig(
                rows=2,
                columns={"id": _seq(100)},
                fk={"a_id": quota, "b_id": quota, "d_id": quota, "e_id": quota},
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", range(10))
def test_shared_quota_components_are_coordinated_independently(seed: int) -> None:
    spec = parse_ddl(_COMPONENT_QUOTA_SCHEMA)
    small = generate_dataset(spec, _component_quota_config(seed, batch_size=1))
    large = generate_dataset(spec, _component_quota_config(seed, batch_size=5000))

    for dataset in (small, large):
        assert dataset.quarantine == {}
        rows = dataset.tables["c"]
        assert len(rows) == 2
        assert Counter((row["x"], row["a_id"]) for row in rows) == {
            (1, 10): 1,
            (2, 11): 1,
        }
        assert Counter((row["x"], row["b_id"]) for row in rows) == {
            (1, 20): 1,
            (2, 21): 1,
        }
        assert Counter((row["y"], row["d_id"]) for row in rows) == {
            (1, 30): 1,
            (2, 31): 1,
        }
        assert Counter((row["y"], row["e_id"]) for row in rows) == {
            (1, 40): 1,
            (2, 41): 1,
        }
    assert _digest(small) == _digest(large)


_BRIDGE_QUOTA_SCHEMA = """
CREATE TABLE a (id INT PRIMARY KEY);
CREATE TABLE b (id INT PRIMARY KEY);
CREATE TABLE x (
    a_id INT NOT NULL,
    b_id INT NOT NULL,
    PRIMARY KEY (a_id, b_id),
    FOREIGN KEY (a_id) REFERENCES a(id),
    FOREIGN KEY (b_id) REFERENCES b(id)
);
"""


def _bridge_quota_config(seed: int, batch_size: int) -> Config:
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"id": _seq(1)}),
            "b": TableConfig(rows=3, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=4,
                fk={"a_id": FkQuota(strategy="quota", min=2, max=2)},
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", range(20))
def test_bridge_assigner_preserves_quota_and_pair_uniqueness(seed: int) -> None:
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    small = generate_dataset(spec, _bridge_quota_config(seed, batch_size=1))
    large = generate_dataset(spec, _bridge_quota_config(seed, batch_size=5000))

    for dataset in (small, large):
        assert dataset.quarantine == {}
        rows = dataset.tables["x"]
        assert Counter(row["a_id"] for row in rows) == {1: 2, 2: 2}
        assert len({(row["a_id"], row["b_id"]) for row in rows}) == 4
    assert _digest(small) == _digest(large)


def _bridge_both_quota_config(seed: int, batch_size: int) -> Config:
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"id": _seq(1)}),
            "b": TableConfig(rows=3, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=4,
                fk={
                    "a_id": FkQuota(strategy="quota", min=2, max=2),
                    "b_id": FkQuota(strategy="quota", min=1, max=2),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


@pytest.mark.parametrize("seed", range(20))
def test_bridge_assigner_satisfies_quotas_on_both_sides(seed: int) -> None:
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    small = generate_dataset(spec, _bridge_both_quota_config(seed, batch_size=1))
    large = generate_dataset(spec, _bridge_both_quota_config(seed, batch_size=5000))

    for dataset in (small, large):
        assert dataset.quarantine == {}
        rows = dataset.tables["x"]
        assert Counter(row["a_id"] for row in rows) == {1: 2, 2: 2}
        b_counts = Counter(row["b_id"] for row in rows)
        assert set(b_counts) == {10, 11, 12}
        assert all(1 <= count <= 2 for count in b_counts.values())
        assert len({(row["a_id"], row["b_id"]) for row in rows}) == 4
    assert _digest(small) == _digest(large)


def test_bridge_assigner_rejects_incompatible_quotas_deterministically() -> None:
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    config = Config(
        seed=0,
        tables={
            "a": TableConfig(rows=2, columns={"id": _seq(1)}),
            "b": TableConfig(rows=3, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=4,
                fk={
                    "a_id": FkQuota(strategy="quota", min=2, max=2),
                    "b_id": FkQuota(strategy="quota", min=2, max=2),
                },
            ),
        },
    )

    messages: list[str] = []
    for seed in range(3):
        with pytest.raises(GenerationError, match=r"tabla puente x.*cuotas") as exc_info:
            generate_dataset(spec, config.model_copy(update={"seed": seed}))
        messages.append(str(exc_info.value))
    assert len(set(messages)) == 1


_BRIDGE_UNIFORM_SCHEMA = """
CREATE TABLE a (id INT PRIMARY KEY);
CREATE TABLE b (id INT PRIMARY KEY);
CREATE TABLE x (
    a_id INT NOT NULL,
    b_id INT NOT NULL,
    PRIMARY KEY (a_id, b_id),
    FOREIGN KEY (a_id) REFERENCES a(id),
    FOREIGN KEY (b_id) REFERENCES b(id)
);
"""


def test_bridge_without_quota_is_coarsely_uniform_without_replacement() -> None:
    spec = parse_ddl(_BRIDGE_UNIFORM_SCHEMA)
    counts: Counter[tuple[tuple[int, int], ...]] = Counter()
    for seed in range(1000):
        config = Config(
            seed=seed,
            tables={
                "a": TableConfig(rows=2, columns={"id": _seq(1)}),
                "b": TableConfig(rows=2, columns={"id": _seq(10)}),
                "x": TableConfig(rows=2),
            },
        )
        dataset = generate_dataset(spec, config)
        rows = dataset.tables["x"]
        assert dataset.quarantine == {}
        assert len({(row["a_id"], row["b_id"]) for row in rows}) == 2
        counts[tuple(sorted((row["a_id"], row["b_id"]) for row in rows))] += 1

    assert len(counts) == 6
    assert min(counts.values()) >= 100
    assert max(counts.values()) <= 230
