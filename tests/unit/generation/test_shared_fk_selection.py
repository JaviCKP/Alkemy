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
from itertools import combinations, product
from random import Random
from types import SimpleNamespace

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
from synthdb.generation._table_assignment import _build_group_pairs
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


# --- Issue #47: UNIQUE compuesta de FKs en tabla regular ---------------------

_REGULAR_COMPOUND_UNIQUE_SCHEMA = """
CREATE TABLE a (id SERIAL PRIMARY KEY);
CREATE TABLE b (id SERIAL PRIMARY KEY);
CREATE TABLE x (
    id UUID PRIMARY KEY,
    a_id INT NOT NULL REFERENCES a(id),
    b_id INT NOT NULL REFERENCES b(id),
    note TEXT,
    flag BOOLEAN,
    UNIQUE (a_id, b_id)
);
"""


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_regular_compound_unique_fks_assign_without_replacement(seed: int, batch_size: int) -> None:
    """A regular table must coordinate FKs covered by a compound UNIQUE."""
    dataset = generate_dataset(
        parse_ddl(_REGULAR_COMPOUND_UNIQUE_SCHEMA),
        Config(
            seed=seed,
            tables={
                "a": TableConfig(rows=2),
                "b": TableConfig(rows=2),
                "x": TableConfig(rows=4),
            },
            output=OutputConfig(batch_size=batch_size),
        ),
    )

    assert dataset.quarantine == {}
    rows = dataset.tables["x"]
    assert len(rows) == 4
    assert {(row["a_id"], row["b_id"]) for row in rows} == {
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    }
    a_ids = {row["id"] for row in dataset.tables["a"]}
    b_ids = {row["id"] for row in dataset.tables["b"]}
    assert all(row["a_id"] in a_ids and row["b_id"] in b_ids for row in rows)


def test_regular_composite_primary_key_of_foreign_keys_uses_same_contract() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        note TEXT,
        flag BOOLEAN,
        extra INT,
        PRIMARY KEY (a_id, b_id)
    );
    """
    spec = parse_ddl(schema)
    dataset = generate_dataset(
        spec,
        Config(
            seed=5,
            tables={"a": TableConfig(rows=2), "b": TableConfig(rows=2), "x": TableConfig(rows=4)},
        ),
    )

    assert next(table for table in spec.tables if table.name == "x").kind == "regular"
    assert dataset.quarantine == {}
    assert len({(row["a_id"], row["b_id"]) for row in dataset.tables["x"]}) == 4


def _regular_compound_config(
    *, rows: int, seed: int = 0, batch_size: int = 5000, fk: dict[str, object] | None = None
) -> Config:
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2),
            "b": TableConfig(rows=2),
            "x": TableConfig(rows=rows, fk=fk or {}),
        },
        output=OutputConfig(batch_size=batch_size),
    )


def _regular_compound_oracle(parent_rows: int, requested: int) -> list[tuple[int, int]]:
    """Enumerate the tiny feasible edge set independently in the test."""
    candidates = list(product(range(1, parent_rows + 1), repeat=2))
    return candidates[:requested] if requested <= len(candidates) else []


@pytest.mark.parametrize("requested", [3, 4, 5])
def test_regular_compound_unique_matches_small_exhaustive_oracle(requested: int) -> None:
    feasible = _regular_compound_oracle(2, requested)
    for seed in range(5):
        if not feasible:
            with pytest.raises(GenerationError) as exc_info:
                generate_dataset(
                    parse_ddl(_REGULAR_COMPOUND_UNIQUE_SCHEMA),
                    _regular_compound_config(rows=requested, seed=seed),
                )
            message = str(exc_info.value)
            assert "tabla x" in message
            assert "a_id, b_id" in message
            assert str(requested) in message
            assert "4 combinaciones compatibles" in message
            continue

        dataset = generate_dataset(
            parse_ddl(_REGULAR_COMPOUND_UNIQUE_SCHEMA),
            _regular_compound_config(rows=requested, seed=seed),
        )
        pairs = {(row["a_id"], row["b_id"]) for row in dataset.tables["x"]}
        assert len(pairs) == requested
        assert pairs.issubset(set(_regular_compound_oracle(2, 4)))
        assert dataset.quarantine == {}


_REGULAR_MULTITENANT_COMPOUND_SCHEMA = """
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
CREATE TABLE regular_links (
    id UUID PRIMARY KEY,
    tenant_id INT NOT NULL,
    left_id INT NOT NULL,
    right_id INT NOT NULL,
    note TEXT,
    flag BOOLEAN,
    UNIQUE (tenant_id, left_id, right_id),
    FOREIGN KEY (tenant_id, left_id) REFERENCES lefts(tenant_id, id),
    FOREIGN KEY (tenant_id, right_id) REFERENCES rights(tenant_id, id)
);
"""


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_regular_multitenant_compound_unique_keeps_shared_discriminator(
    seed: int, batch_size: int
) -> None:
    spec = parse_ddl(_REGULAR_MULTITENANT_COMPOUND_SCHEMA)
    dataset = generate_dataset(
        spec,
        Config(
            seed=seed,
            tables={
                "lefts": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(100)}),
                "rights": TableConfig(rows=4, columns={"tenant_id": _seq(1), "id": _seq(200)}),
                "regular_links": TableConfig(rows=4),
            },
            output=OutputConfig(batch_size=batch_size),
        ),
    )

    assert next(table for table in spec.tables if table.name == "regular_links").kind == "regular"
    assert dataset.quarantine == {}
    rows = dataset.tables["regular_links"]
    assert len(rows) == 4
    assert len({(row["tenant_id"], row["left_id"], row["right_id"]) for row in rows}) == 4
    left_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["lefts"]}
    right_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["rights"]}
    assert all(
        (row["tenant_id"], row["left_id"]) in left_keys
        and (row["tenant_id"], row["right_id"]) in right_keys
        for row in rows
    )


def test_regular_compound_unique_can_coordinate_three_foreign_keys() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE c (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        c_id INT NOT NULL REFERENCES c(id),
        note TEXT,
        flag BOOLEAN,
        UNIQUE (a_id, b_id, c_id)
    );
    """
    for seed in range(5):
        dataset = generate_dataset(
            parse_ddl(schema),
            Config(
                seed=seed,
                tables={
                    "a": TableConfig(rows=2),
                    "b": TableConfig(rows=2),
                    "c": TableConfig(rows=2),
                    "x": TableConfig(rows=8),
                },
                output=OutputConfig(batch_size=1),
            ),
        )
        rows = dataset.tables["x"]
        assert dataset.quarantine == {}
        assert len({(row["a_id"], row["b_id"], row["c_id"]) for row in rows}) == 8


def test_multiple_compound_unique_constraints_share_one_joint_assignment() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        note TEXT,
        flag BOOLEAN,
        UNIQUE (a_id, b_id),
        UNIQUE (b_id, a_id)
    );
    """
    dataset = generate_dataset(
        parse_ddl(schema),
        Config(
            seed=7,
            tables={"a": TableConfig(rows=2), "b": TableConfig(rows=2), "x": TableConfig(rows=4)},
        ),
    )

    assert dataset.quarantine == {}
    assert len({(row["a_id"], row["b_id"]) for row in dataset.tables["x"]}) == 4


def test_overlapping_compound_unique_constraints_reject_before_invalid_rows() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE c (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        c_id INT NOT NULL REFERENCES c(id),
        note TEXT,
        flag BOOLEAN,
        UNIQUE (a_id, b_id),
        UNIQUE (a_id, c_id)
    );
    """
    with pytest.raises(GenerationError, match="comparten una componente de FKs"):
        generate_dataset(
            parse_ddl(schema),
            Config(
                seed=13,
                tables={
                    "a": TableConfig(rows=2),
                    "b": TableConfig(rows=2),
                    "c": TableConfig(rows=2),
                    "x": TableConfig(rows=2),
                },
            ),
        )


@pytest.mark.parametrize(
    ("fk", "rows"),
    [
        (
            {
                "a_id": FkQuota(strategy="quota", min=1, max=1),
                "b_id": FkQuota(strategy="quota", min=1, max=1),
            },
            2,
        ),
        (
            {
                "a_id": FkUniqueSubset(strategy="unique_subset"),
                "b_id": FkUniqueSubset(strategy="unique_subset"),
            },
            2,
        ),
        (
            {
                "a_id": FkUniqueSubset(strategy="unique_subset"),
                "b_id": FkQuota(strategy="quota", min=1, max=1),
            },
            2,
        ),
    ],
)
def test_regular_compound_unique_preserves_limited_fk_strategies(
    fk: dict[str, object], rows: int
) -> None:
    dataset = generate_dataset(
        parse_ddl(_REGULAR_COMPOUND_UNIQUE_SCHEMA),
        _regular_compound_config(rows=rows, seed=11, batch_size=1, fk=fk),
    )

    assert dataset.quarantine == {}
    assert len(dataset.tables["x"]) == rows
    a_counts = Counter(row["a_id"] for row in dataset.tables["x"])
    b_counts = Counter(row["b_id"] for row in dataset.tables["x"])
    assert all(count <= 1 for count in a_counts.values())
    assert all(count <= 1 for count in b_counts.values())


def test_regular_compound_unique_does_not_activate_for_normal_attribute() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id SERIAL PRIMARY KEY,
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        note INT NOT NULL,
        UNIQUE (a_id, b_id, note)
    );
    """
    dataset = generate_dataset(
        parse_ddl(schema),
        Config(
            seed=3,
            tables={
                "a": TableConfig(rows=2),
                "b": TableConfig(rows=2),
                "x": TableConfig(rows=5, columns={"note": _seq(1)}),
            },
        ),
    )

    assert dataset.quarantine == {}
    assert len(dataset.tables["x"]) == 5
    assert len({(row["a_id"], row["b_id"], row["note"]) for row in dataset.tables["x"]}) == 5


def test_single_fk_unique_keeps_one_to_one_selection() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT NOT NULL UNIQUE REFERENCES a(id),
        note TEXT,
        flag BOOLEAN
    );
    """
    for seed in range(5):
        dataset = generate_dataset(
            parse_ddl(schema),
            Config(seed=seed, tables={"a": TableConfig(rows=2), "x": TableConfig(rows=2)}),
        )
        assert dataset.quarantine == {}
        assert len({row["a_id"] for row in dataset.tables["x"]}) == 2


def test_regular_compound_unique_respects_postgresql_null_distinct_semantics() -> None:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        note TEXT,
        flag BOOLEAN,
        UNIQUE (a_id, b_id)
    );
    """
    dataset = generate_dataset(
        parse_ddl(schema),
        Config(
            seed=1,
            tables={
                "a": TableConfig(rows=2),
                "b": TableConfig(rows=2),
                "x": TableConfig(
                    rows=4,
                    fk={"a_id": FkUniform(strategy="uniform", null_ratio=0.5)},
                ),
            },
            output=OutputConfig(batch_size=5000),
        ),
    )

    rows = dataset.tables["x"]
    non_null_pairs = [(row["a_id"], row["b_id"]) for row in rows if row["a_id"] is not None]
    assert any(row["a_id"] is None for row in rows)
    assert len(non_null_pairs) == len(set(non_null_pairs))
    assert dataset.quarantine == {}


def _regular_compound_probe(n: int) -> tuple[int, Dataset]:
    schema = """
    CREATE TABLE a (id SERIAL PRIMARY KEY);
    CREATE TABLE b (id SERIAL PRIMARY KEY);
    CREATE TABLE x (
        id UUID PRIMARY KEY,
        a_id INT NOT NULL REFERENCES a(id),
        b_id INT NOT NULL REFERENCES b(id),
        note TEXT,
        flag BOOLEAN,
        UNIQUE (a_id, b_id)
    );
    """
    dataset = generate_dataset(
        parse_ddl(schema),
        Config(
            seed=4,
            tables={"a": TableConfig(rows=n), "b": TableConfig(rows=n), "x": TableConfig(rows=n)},
        ),
    )
    return engine._SELECTION_WORK.compound_pairs_examined, dataset


def test_regular_compound_unique_selection_work_scales_with_requested_pairs() -> None:
    work_small, small = _regular_compound_probe(200)
    work_big, big = _regular_compound_probe(800)

    assert small.quarantine == {} and big.quarantine == {}
    assert work_small == 200
    assert work_big == 800
    assert work_big <= 8 * work_small


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


def test_nullable_shared_quotas_assign_groups_globally_before_consuming_capacity() -> None:
    """Different NULL masks must leave a jointly feasible group assignment."""
    spec = parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA)
    configs = [
        Config(
            seed=18,
            tables={
                "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
                "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
                "c": TableConfig(
                    rows=3,
                    columns={"id": _seq(100)},
                    fk={
                        "a_id": FkQuota(strategy="quota", min=0, max=1, null_ratio=0.5),
                        "b_id": FkQuota(strategy="quota", min=0, max=1, null_ratio=0.5),
                    },
                ),
            },
            output=OutputConfig(batch_size=batch_size),
        )
        for batch_size in (1, 5000)
    ]
    datasets = [generate_dataset(spec, config) for config in configs]

    for dataset in datasets:
        assert dataset.quarantine == {}
        rows = dataset.tables["c"]
        assert len(rows) == 3
        assert rows[0]["a_id"] is not None and rows[0]["b_id"] is None
        assert rows[1]["a_id"] is None and rows[1]["b_id"] is not None
        assert rows[2]["a_id"] is not None and rows[2]["b_id"] is not None
        assert rows[0]["tenant_id"] == rows[1]["tenant_id"]
        assert rows[0]["tenant_id"] != rows[2]["tenant_id"]
        assert all(
            count <= 1
            for count in Counter(
                (row["tenant_id"], row["a_id"]) for row in rows if row["a_id"] is not None
            ).values()
        )
        assert all(
            count <= 1
            for count in Counter(
                (row["tenant_id"], row["b_id"]) for row in rows if row["b_id"] is not None
            ).values()
        )
    assert _digest(datasets[0]) == _digest(datasets[1])


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_shared_quotas_with_same_null_ratio_allow_independent_masks(
    seed: int, batch_size: int
) -> None:
    """Equal null ratios do not require equal per-row masks."""
    spec = parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA)
    config = Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
            "c": TableConfig(
                rows=20,
                columns={"id": _seq(100)},
                fk={
                    "a_id": FkQuota(strategy="quota", min=1, max=20, null_ratio=0.5),
                    "b_id": FkQuota(strategy="quota", min=1, max=20, null_ratio=0.5),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )

    dataset = generate_dataset(spec, config)

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    a_counts = Counter((row["tenant_id"], row["a_id"]) for row in rows if row["a_id"])
    b_counts = Counter((row["tenant_id"], row["b_id"]) for row in rows if row["b_id"])
    assert all(1 <= a_counts[(tenant, 10 + tenant - 1)] <= 20 for tenant in (1, 2))
    assert all(1 <= b_counts[(tenant, 20 + tenant - 1)] <= 20 for tenant in (1, 2))


def _large_variable_component_config(rows: int, seed: int, batch_size: int) -> Config:
    return Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
            "c": TableConfig(
                rows=rows,
                columns={"id": _seq(100)},
                fk={
                    "a_id": FkQuota(strategy="quota", min=0, max=rows, null_ratio=0.5),
                    "b_id": FkQuota(strategy="quota", min=0, max=rows, null_ratio=0.5),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


def _large_variable_component_probe(
    rows: int, *, seed: int = 18, batch_size: int = 5000
) -> tuple[int, int, Dataset]:
    dataset = generate_dataset(
        parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA),
        _large_variable_component_config(rows, seed, batch_size),
    )
    return (
        engine._SELECTION_WORK.global_solver_work,
        engine._SELECTION_WORK.global_solver_states,
        dataset,
    )


@pytest.mark.parametrize("batch_size", [1, 5000])
def test_global_component_solver_handles_1200_rows_without_recursion(
    batch_size: int,
) -> None:
    """A feasible variable-mask component must not depend on Python stack depth."""
    dataset = generate_dataset(
        parse_ddl(_NULLABLE_TWO_QUOTA_SCHEMA),
        _large_variable_component_config(rows=1200, seed=18, batch_size=batch_size),
    )

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    assert len(rows) == 1200
    assert any((row["a_id"] is None) != (row["b_id"] is None) for row in rows)
    a_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["a"]}
    b_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["b"]}
    for row in rows:
        if row["a_id"] is not None:
            assert (row["tenant_id"], row["a_id"]) in a_keys
        if row["b_id"] is not None:
            assert (row["tenant_id"], row["b_id"]) in b_keys


def test_global_component_solver_work_is_linear_at_10000_rows() -> None:
    """Aggregated signatures keep solver work proportional to rows, not states^rows."""
    small_work, small_states, small = _large_variable_component_probe(1200)
    large_work, large_states, large = _large_variable_component_probe(10_000)

    for dataset, expected_rows in ((small, 1200), (large, 10_000)):
        assert dataset.quarantine == {}
        assert len(dataset.tables["c"]) == expected_rows
    assert small_work > 0 and small_states > 0
    assert large_work <= 12 * small_work
    assert large_states <= 12 * small_states
    assert large_work <= 200 * 10_000


_VARIABLE_QUOTA_UNIQUE_SCHEMA = """
CREATE TABLE quota_parents (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE unique_parents (
    tenant_id INT NOT NULL,
    id INT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE quota_unique_children (
    id INT PRIMARY KEY,
    tenant_id INT NOT NULL,
    quota_id INT,
    unique_id INT NOT NULL,
    FOREIGN KEY (tenant_id, quota_id) REFERENCES quota_parents(tenant_id, id),
    FOREIGN KEY (tenant_id, unique_id) REFERENCES unique_parents(tenant_id, id)
);
"""


def _variable_quota_unique_config(rows: int, seed: int, batch_size: int) -> Config:
    return Config(
        seed=seed,
        tables={
            "quota_parents": TableConfig(
                rows=rows,
                columns={"tenant_id": _seq(1), "id": _seq(10)},
            ),
            "unique_parents": TableConfig(
                rows=rows,
                columns={"tenant_id": _seq(1), "id": _seq(20)},
            ),
            "quota_unique_children": TableConfig(
                rows=rows,
                columns={"id": _seq(100)},
                fk={
                    "quota_id": FkQuota(strategy="quota", min=0, max=2, null_ratio=0.5),
                    "unique_id": FkUniqueSubset(strategy="unique_subset"),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )


def _variable_quota_unique_probe(
    rows: int, *, seed: int, batch_size: int
) -> tuple[int, int, Dataset]:
    dataset = generate_dataset(
        parse_ddl(_VARIABLE_QUOTA_UNIQUE_SCHEMA),
        _variable_quota_unique_config(rows, seed, batch_size),
    )
    return (
        engine._SELECTION_WORK.global_solver_work,
        engine._SELECTION_WORK.global_solver_states,
        dataset,
    )


def _assert_variable_quota_unique_contract(dataset: Dataset, rows: int) -> None:
    assert dataset.quarantine == {}
    children = dataset.tables["quota_unique_children"]
    assert len(children) == rows
    quota_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["quota_parents"]}
    unique_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["unique_parents"]}
    unique_counts = Counter((row["tenant_id"], row["unique_id"]) for row in children)
    quota_counts = Counter(
        (row["tenant_id"], row["quota_id"]) for row in children if row["quota_id"] is not None
    )
    assert {row["tenant_id"] for row in children} == set(range(1, rows + 1))
    assert set(unique_counts) == unique_keys
    assert all(count == 1 for count in unique_counts.values())
    assert all((key in quota_keys) for key in quota_counts)
    assert all(count <= 2 for count in quota_counts.values())
    quota_non_null = sum(row["quota_id"] is not None for row in children)
    assert rows // 4 <= quota_non_null <= (3 * rows + 3) // 4
    for row in children:
        assert (row["tenant_id"], row["unique_id"]) in unique_keys
        if row["quota_id"] is not None:
            assert (row["tenant_id"], row["quota_id"]) in quota_keys


def test_nullable_quota_unique_subset_scales_with_tenants_and_rows() -> None:
    """A bounded solver must not enumerate weak compositions before capacities."""
    measurements: dict[int, dict[int, tuple[int, int]]] = {1: {}, 5000: {}}
    digests: dict[int, dict[int, str]] = {1: {}, 5000: {}}
    for batch_size in measurements:
        for rows in (20, 100, 1000):
            work, states, dataset = _variable_quota_unique_probe(
                rows, seed=18, batch_size=batch_size
            )
            _assert_variable_quota_unique_contract(dataset, rows)
            assert work > 0 and states > 0
            measurements[batch_size][rows] = (work, states)
            digests[batch_size][rows] = _digest(dataset)

        assert measurements[batch_size][100][0] <= 8 * measurements[batch_size][20][0]
        assert measurements[batch_size][1000][0] <= 60 * measurements[batch_size][20][0]
        assert measurements[batch_size][100][1] <= 8 * measurements[batch_size][20][1]
        assert measurements[batch_size][1000][1] <= 60 * measurements[batch_size][20][1]
        assert measurements[batch_size][1000][0] <= 200 * 1000

    assert all(digests[1][rows] == digests[5000][rows] for rows in (20, 100, 1000))


def test_nullable_quota_unique_subset_infeasible_is_stable() -> None:
    """A missing unique parent is rejected independently of seed and batching."""
    messages: list[str] = []
    for seed in range(8):
        for batch_size in (1, 5000):
            config = _variable_quota_unique_config(20, seed, batch_size)
            config.tables["unique_parents"] = TableConfig(
                rows=19,
                columns={"tenant_id": _seq(1), "id": _seq(20)},
            )
            with pytest.raises(GenerationError, match="asignaci") as exc_info:
                generate_dataset(parse_ddl(_VARIABLE_QUOTA_UNIQUE_SCHEMA), config)
            messages.append(str(exc_info.value))

    assert len(set(messages)) == 1
    assert "quota_unique_children" in messages[0]
    assert "unique_subset" in messages[0]


_MIXED_QUOTA_UNIQUE_SCHEMA = """
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


@pytest.mark.parametrize("seed", range(30))
@pytest.mark.parametrize("batch_size", [1, 5000])
def test_quota_and_unique_subset_share_capacity_across_tenants(seed: int, batch_size: int) -> None:
    """A quota cannot consume a group needed by an independent unique subset."""
    spec = parse_ddl(_MIXED_QUOTA_UNIQUE_SCHEMA)
    config = Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(10)}),
            "b": TableConfig(rows=2, columns={"tenant_id": _seq(1), "id": _seq(20)}),
            "c": TableConfig(
                rows=2,
                columns={"id": _seq(100)},
                fk={
                    "a_id": FkQuota(strategy="quota", min=0, max=2),
                    "b_id": FkUniqueSubset(strategy="unique_subset"),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )

    dataset = generate_dataset(spec, config)

    assert dataset.quarantine == {}
    rows = dataset.tables["c"]
    assert {row["tenant_id"] for row in rows} == {1, 2}
    assert Counter((row["tenant_id"], row["a_id"]) for row in rows) == {
        (1, 10): 1,
        (2, 11): 1,
    }
    assert Counter((row["tenant_id"], row["b_id"]) for row in rows) == {
        (1, 20): 1,
        (2, 21): 1,
    }


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


@pytest.mark.parametrize("batch_size", [1, 5000])
def test_bridge_zero_min_quota_seed_two_finds_two_unique_pairs(batch_size: int) -> None:
    """A feasible 2x2 bridge must not depend on the quota seed."""
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    config = Config(
        seed=2,
        tables={
            "a": TableConfig(rows=2, columns={"id": _seq(1)}),
            "b": TableConfig(rows=2, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=2,
                fk={
                    "a_id": FkQuota(strategy="quota", min=0, max=2),
                    "b_id": FkQuota(strategy="quota", min=0, max=2),
                },
            ),
        },
        output=OutputConfig(batch_size=batch_size),
    )

    dataset = generate_dataset(spec, config)

    rows = dataset.tables["x"]
    assert dataset.quarantine == {}
    assert len(rows) == 2
    assert len({(row["a_id"], row["b_id"]) for row in rows}) == 2
    assert {row["a_id"] for row in rows} <= {1, 2}
    assert {row["b_id"] for row in rows} <= {10, 11}


def _bridge_has_degree_solution(
    left_count: int,
    right_count: int,
    rows: int,
    left_min: int,
    left_max: int,
    right_min: int,
    right_max: int,
) -> bool:
    """Exhaustively decide the small simple-bipartite bridge contract."""
    edges = list(product(range(left_count), range(right_count)))
    for selected in combinations(edges, rows):
        left_degrees = Counter(left for left, _ in selected)
        right_degrees = Counter(right for _, right in selected)
        if all(left_min <= left_degrees[left] <= left_max for left in range(left_count)) and all(
            right_min <= right_degrees[right] <= right_max for right in range(right_count)
        ):
            return True
    return False


def _all_small_bridge_contracts() -> list[tuple[int, int, int, int, int, int, int]]:
    return [
        (left_count, right_count, rows, left_min, left_max, right_min, right_max)
        for left_count in range(1, 4)
        for right_count in range(1, 4)
        for rows in range(left_count * right_count + 1)
        for left_min in range(right_count + 1)
        for left_max in range(left_min, right_count + 1)
        for right_min in range(left_count + 1)
        for right_max in range(right_min, left_count + 1)
    ]


def _quota_stub(minimum: int, maximum: int, key: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=(key,),
        relation=SimpleNamespace(params={"min": minimum, "max": maximum}),
    )


def test_bridge_quota_matches_complete_small_exhaustive_oracle() -> None:
    """Every small feasible contract works for every seed; infeasible ones never do."""
    contracts = _all_small_bridge_contracts()
    assert len(contracts) > 2000
    for case in contracts:
        (
            left_count,
            right_count,
            rows,
            left_min,
            left_max,
            right_min,
            right_max,
        ) = case
        expected = _bridge_has_degree_solution(*case)
        errors: list[str] = []
        for seed in range(5):
            work = SimpleNamespace(bridge_quota_work=0)
            try:
                pairs = _build_group_pairs(
                    list(range(left_count)),
                    list(range(right_count)),
                    rows,
                    _quota_stub(left_min, left_max, "left"),
                    _quota_stub(right_min, right_max, "right"),
                    Random(seed),
                    "x",
                    work,
                )
            except RuntimeError as exc:
                errors.append(str(exc))
                continue

            assert expected, case
            assert len(pairs) == rows
            assert len(set(pairs)) == rows
            left_counts = Counter(left for left, _ in pairs)
            right_counts = Counter(right for _, right in pairs)
            assert all(left_min <= left_counts[left] <= left_max for left in range(left_count))
            assert all(
                right_min <= right_counts[right] <= right_max for right in range(right_count)
            )

        if expected:
            assert not errors, (case, errors)
        else:
            assert len(errors) == 5, case
            assert len(set(errors)) == 1, (case, errors)


@pytest.mark.parametrize(
    (
        "left_count",
        "right_count",
        "rows",
        "left_min",
        "left_max",
        "right_min",
        "right_max",
    ),
    [
        (1, 1, 1, 0, 1, 0, 1),
        (1, 3, 3, 0, 3, 1, 1),
        (2, 2, 2, 0, 2, 0, 2),
        (2, 3, 3, 0, 2, 0, 1),
        (3, 2, 4, 1, 2, 0, 2),
        (3, 3, 5, 0, 2, 1, 2),
        (3, 3, 6, 1, 2, 1, 2),
    ],
)
@pytest.mark.parametrize("seed", range(10))
def test_bridge_quota_exhaustive_oracle_accepts_every_seed(
    left_count: int,
    right_count: int,
    rows: int,
    left_min: int,
    left_max: int,
    right_min: int,
    right_max: int,
    seed: int,
) -> None:
    """The seed may choose a feasible bridge, never certify feasibility."""
    assert _bridge_has_degree_solution(
        left_count,
        right_count,
        rows,
        left_min,
        left_max,
        right_min,
        right_max,
    )
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    config = Config(
        seed=seed,
        tables={
            "a": TableConfig(rows=left_count, columns={"id": _seq(1)}),
            "b": TableConfig(rows=right_count, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=rows,
                fk={
                    "a_id": FkQuota(strategy="quota", min=left_min, max=left_max),
                    "b_id": FkQuota(strategy="quota", min=right_min, max=right_max),
                },
            ),
        },
    )

    dataset = generate_dataset(spec, config)

    assert dataset.quarantine == {}
    result = dataset.tables["x"]
    assert len(result) == rows
    assert len({(row["a_id"], row["b_id"]) for row in result}) == rows
    assert all(1 <= row["a_id"] <= left_count for row in result)
    assert all(10 <= row["b_id"] < 10 + right_count for row in result)
    left_counts = Counter(row["a_id"] for row in result)
    right_counts = Counter(row["b_id"] for row in result)
    assert all(
        left_min <= left_counts.get(parent, 0) <= left_max for parent in range(1, left_count + 1)
    )
    assert all(
        right_min <= right_counts.get(parent, 0) <= right_max
        for parent in range(10, 10 + right_count)
    )


@pytest.mark.parametrize(
    "case",
    [
        (2, 1, 1, 2, 2, 0, 1),
        (2, 2, 3, 2, 2, 2, 2),
    ],
)
def test_bridge_quota_exhaustive_oracle_rejects_infeasible_stably(
    case: tuple[int, int, int, int, int, int, int],
) -> None:
    """An impossible bridge is rejected before seed-dependent choices."""
    left_count, right_count, rows, left_min, left_max, right_min, right_max = case
    assert not _bridge_has_degree_solution(*case)
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    messages: list[str] = []
    for seed in range(10):
        config = Config(
            seed=seed,
            tables={
                "a": TableConfig(rows=left_count, columns={"id": _seq(1)}),
                "b": TableConfig(rows=right_count, columns={"id": _seq(10)}),
                "x": TableConfig(
                    rows=rows,
                    fk={
                        "a_id": FkQuota(strategy="quota", min=left_min, max=left_max),
                        "b_id": FkQuota(strategy="quota", min=right_min, max=right_max),
                    },
                ),
            },
        )
        with pytest.raises(GenerationError, match=r"tabla puente x") as exc_info:
            generate_dataset(spec, config)
        messages.append(str(exc_info.value))
    assert len(set(messages)) == 1


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


def _bridge_quota_probe(n: int) -> tuple[int, Dataset]:
    spec = parse_ddl(_BRIDGE_QUOTA_SCHEMA)
    config = Config(
        seed=2,
        tables={
            "a": TableConfig(rows=n, columns={"id": _seq(1)}),
            "b": TableConfig(rows=n, columns={"id": _seq(10)}),
            "x": TableConfig(
                rows=n,
                fk={
                    "a_id": FkQuota(strategy="quota", min=0, max=n),
                    "b_id": FkQuota(strategy="quota", min=0, max=n),
                },
            ),
        },
        output=OutputConfig(batch_size=5000),
    )
    dataset = generate_dataset(spec, config)
    return engine._SELECTION_WORK.bridge_quota_work, dataset


def test_bridge_quota_resolution_work_scales_with_parents_and_requested_pairs() -> None:
    """Quota bridges must not inspect the complete L×R edge space."""
    measurements: list[tuple[int, int]] = []
    for n in (200, 800, 1600, 3200):
        work, dataset = _bridge_quota_probe(n)
        rows = dataset.tables["x"]
        assert dataset.quarantine == {}
        assert len(rows) == n
        assert len({(row["a_id"], row["b_id"]) for row in rows}) == n
        assert all(1 <= row["a_id"] <= n for row in rows)
        assert all(10 <= row["b_id"] < 10 + n for row in rows)
        assert work > 0
        assert work <= 32 * n
        measurements.append((n, work))

    assert measurements[-1][1] <= 20 * measurements[0][1]


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
