"""End-to-end contracts for the in-memory generation engine (T2.11-T2.13)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from synthdb.config.loader import load_config
from synthdb.config.models import (
    ColumnConfig,
    Config,
    FkUniqueSubset,
    HierarchyConfig,
    OutputConfig,
    TableConfig,
)
from synthdb.generation import engine
from synthdb.generation.engine import Dataset, GenerationError, PlanError, generate_dataset
from synthdb.ir.schema import SchemaSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _schema(name: str) -> SchemaSpec:
    return parse_ddl((_SCHEMAS / name).read_text("utf-8"))


def _digest(dataset: Dataset) -> str:
    payload = json.dumps(dataset.tables, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def test_integer_codigo_generation_is_deterministic_and_batch_independent() -> None:
    spec = parse_ddl(
        "CREATE TABLE inmobiliarias (id UUID PRIMARY KEY, siguiente_referencia INTEGER NOT NULL);"
    )

    def config(batch_size: int) -> Config:
        return Config(
            seed=42,
            tables={"inmobiliarias": TableConfig(rows=10)},
            output=OutputConfig(batch_size=batch_size),
        )

    first = generate_dataset(spec, config(1))
    second = generate_dataset(spec, config(5000))
    plan = next(table for table in first.table_plans.tables if table.table == "inmobiliarias")
    column_plan = next(column for column in plan.columns if column.column == "siguiente_referencia")
    values = [row["siguiente_referencia"] for row in first.tables["inmobiliarias"]]

    assert column_plan.source == "heuristic"
    assert column_plan.generator is not None
    assert column_plan.generator.type == "sequence"
    assert values == list(range(1, 11))
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    assert first.tables == second.tables
    assert first.quarantine == {}
    assert second.quarantine == {}


def test_simple_generation_is_independent_of_batch_size() -> None:
    spec = parse_ddl(
        "CREATE TABLE parent (id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE);"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL "
        "REFERENCES parent(id), value INT NOT NULL CHECK (value BETWEEN 1 AND 9));"
    )
    tables = {"parent": TableConfig(rows=20), "child": TableConfig(rows=40)}

    small = generate_dataset(spec, Config(seed=7, tables=tables, output=OutputConfig(batch_size=3)))
    large = generate_dataset(
        spec, Config(seed=7, tables=tables, output=OutputConfig(batch_size=5000))
    )

    assert _digest(small) == _digest(large)
    parent_ids = {row["id"] for row in small.tables["parent"]}
    assert all(row["parent_id"] in parent_ids for row in small.tables["child"])
    assert small.quarantine == {}


def test_complete_batch_receives_each_whole_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(engine, "complete_batch", lambda batch: seen.append(list(batch)))
    spec = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY, value TEXT NOT NULL);")

    dataset = generate_dataset(
        spec,
        Config(tables={"t": TableConfig(rows=7)}, output=OutputConfig(batch_size=3)),
    )

    assert [len(batch) for batch in seen] == [3, 3, 1]
    assert sum(seen, []) == dataset.tables["t"]


def test_nullable_cycle_is_updated_to_valid_final_foreign_keys() -> None:
    dataset = generate_dataset(
        _schema("ciclos_nullable.sql"),
        Config(
            seed=11,
            tables={"pedidos": TableConfig(rows=12), "facturas": TableConfig(rows=12)},
        ),
    )

    factura_ids = {row["id"] for row in dataset.tables["facturas"]}
    assert all(row["factura_id"] in factura_ids for row in dataset.tables["pedidos"])
    assert dataset.updates


def test_partial_nullable_composite_cycle_keeps_tenant_when_no_parent_matches() -> None:
    dataset = generate_dataset(
        _schema("crm_real_minimo.sql"),
        Config(
            seed=1,
            tables={
                "inmobiliarias": TableConfig(rows=5),
                "clientes": TableConfig(rows=5),
                "matches": TableConfig(rows=5),
            },
        ),
    )

    assert dataset.quarantine == {}
    assert any(row["match_id"] is None for row in dataset.tables["clientes"])
    assert all(row["inmobiliaria_id"] is not None for row in dataset.tables["clientes"])
    client_keys = {(row["inmobiliaria_id"], row["id"]) for row in dataset.tables["clientes"]}
    assert all(
        (row["inmobiliaria_id"], row["cliente_id"]) in client_keys
        for row in dataset.tables["matches"]
    )


def test_deferrable_cycle_is_completed_before_final_validation() -> None:
    dataset = generate_dataset(
        _schema("ciclos_deferrable.sql"),
        Config(
            seed=13,
            tables={"pedidos": TableConfig(rows=10), "facturas": TableConfig(rows=10)},
        ),
    )
    factura_ids = {row["id"] for row in dataset.tables["facturas"]}
    pedido_ids = {row["id"] for row in dataset.tables["pedidos"]}
    assert all(row["factura_id"] in factura_ids for row in dataset.tables["pedidos"])
    assert all(row["pedido_id"] in pedido_ids for row in dataset.tables["facturas"])
    assert dataset.quarantine == {}


@pytest.mark.parametrize(
    ("schema_name", "roots_self"),
    [("rrhh_autoref_nullable.sql", False), ("rrhh_autoref_notnull.sql", True)],
)
def test_self_reference_uses_only_previous_level(schema_name: str, roots_self: bool) -> None:
    dataset = generate_dataset(
        _schema(schema_name),
        Config(
            seed=3,
            tables={"empleados": TableConfig(rows=40)},
            hierarchy={"empleados.manager_id": HierarchyConfig(branching=3, max_depth=4)},
        ),
    )
    rows = dataset.tables["empleados"]
    levels = dataset.levels["empleados"]
    assert len(rows) == len(levels) == 40
    assert max(levels) <= 4
    by_id = {row["id"]: level for row, level in zip(rows, levels, strict=True)}
    for row, level in zip(rows, levels, strict=True):
        if level == 0:
            assert row["manager_id"] == row["id"] if roots_self else row["manager_id"] is None
        else:
            assert by_id[row["manager_id"]] == level - 1


def _multitenant_config(batch_size: int = 5000, customers_rows: int = 3) -> Config:
    return Config(
        seed=42,
        defaults={"rows": 10},
        tables={
            "tenants": TableConfig(rows=3),
            "operations": TableConfig(
                rows=3,
                fk={"tenant_id": FkUniqueSubset(strategy="unique_subset")},
            ),
            "customers": TableConfig(
                rows=customers_rows,
                fk={"tenant_id": FkUniqueSubset(strategy="unique_subset")},
            ),
            "offers": TableConfig(rows=15),
        },
        hierarchy={"offers.previous_id": HierarchyConfig(branching=2, max_depth=4)},
        output=OutputConfig(batch_size=batch_size),
    )


def test_composite_multitenant_self_reference_keeps_all_foreign_keys_valid() -> None:
    dataset = generate_dataset(_schema("multitenant_autoref.sql"), _multitenant_config())

    assert dataset.quarantine == {}
    assert len(dataset.tables["offers"]) == len(dataset.levels["offers"]) == 15
    operation_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["operations"]}
    customer_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["customers"]}
    offer_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["offers"]}
    by_id = {
        row["id"]: (row, level)
        for row, level in zip(dataset.tables["offers"], dataset.levels["offers"], strict=True)
    }

    for row in dataset.tables["offers"]:
        assert (row["tenant_id"], row["operation_id"]) in operation_keys
        if row["customer_id"] is not None:
            assert (row["tenant_id"], row["customer_id"]) in customer_keys
        level = by_id[row["id"]][1]
        if level == 0:
            assert row["previous_id"] is None
        else:
            parent, parent_level = by_id[row["previous_id"]]
            assert parent_level == level - 1
            assert parent["tenant_id"] == row["tenant_id"]
            assert (row["tenant_id"], row["previous_id"]) in offer_keys


def test_composite_multitenant_self_reference_uuid_roots_point_to_self() -> None:
    dataset = generate_dataset(_schema("multitenant_autoref_notnull.sql"), _multitenant_config())

    assert dataset.quarantine == {}
    rows = dataset.tables["offers"]
    levels = dataset.levels["offers"]
    for row, level in zip(rows, levels, strict=True):
        if level == 0:
            assert row["previous_id"] == row["id"]
        else:
            parent = next(parent for parent in rows if parent["id"] == row["previous_id"])
            assert parent["tenant_id"] == row["tenant_id"]


def test_partially_nullable_shared_fk_nulls_only_its_nullable_columns() -> None:
    dataset = generate_dataset(
        _schema("multitenant_autoref.sql"), _multitenant_config(customers_rows=1)
    )

    assert dataset.quarantine == {}
    assert any(row["customer_id"] is None for row in dataset.tables["offers"])
    assert all(row["tenant_id"] is not None for row in dataset.tables["offers"])
    operation_keys = {(row["tenant_id"], row["id"]) for row in dataset.tables["operations"]}
    assert all(
        (row["tenant_id"], row["operation_id"]) in operation_keys
        for row in dataset.tables["offers"]
    )


def test_composite_multitenant_generation_is_independent_of_batch_size() -> None:
    small = generate_dataset(_schema("multitenant_autoref.sql"), _multitenant_config(2))
    large = generate_dataset(_schema("multitenant_autoref.sql"), _multitenant_config(5000))

    assert _digest(small) == _digest(large)
    assert small.levels == large.levels


def test_composite_fk_without_compatible_required_parent_fails_actionably() -> None:
    spec = parse_ddl(
        """
        CREATE TABLE operations (
            tenant_id UUID NOT NULL,
            id UUID NOT NULL,
            PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE customers (
            tenant_id UUID NOT NULL,
            id UUID NOT NULL,
            PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE offers (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            operation_id UUID NOT NULL,
            customer_id UUID NOT NULL,
            previous_id UUID,
            FOREIGN KEY (tenant_id, operation_id)
                REFERENCES operations(tenant_id, id),
            FOREIGN KEY (tenant_id, customer_id)
                REFERENCES customers(tenant_id, id),
            FOREIGN KEY (tenant_id, previous_id)
                REFERENCES offers(tenant_id, id)
        );
        """
    )
    tenant_a = UUID("00000000-0000-0000-0000-000000000001")
    tenant_b = UUID("00000000-0000-0000-0000-000000000002")
    config = Config(
        tables={
            "operations": TableConfig(
                rows=1,
                columns={
                    "tenant_id": ColumnConfig(generator="choice", params={"values": [tenant_a]})
                },
            ),
            "customers": TableConfig(
                rows=1,
                columns={
                    "tenant_id": ColumnConfig(generator="choice", params={"values": [tenant_b]})
                },
            ),
            "offers": TableConfig(rows=1),
        },
        hierarchy={"offers.previous_id": HierarchyConfig(branching=2, max_depth=1)},
    )

    with pytest.raises(GenerationError, match=r"tabla offers.*no hay padre compatible"):
        generate_dataset(spec, config)


def test_shared_required_fks_choose_a_compatible_parent_combination() -> None:
    spec = parse_ddl(
        """
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
            id INT PRIMARY KEY,
            tenant_id INT NOT NULL,
            left_id INT NOT NULL,
            right_id INT NOT NULL,
            FOREIGN KEY (tenant_id, left_id) REFERENCES lefts(tenant_id, id),
            FOREIGN KEY (tenant_id, right_id) REFERENCES rights(tenant_id, id)
        );
        """
    )
    config = Config(
        seed=0,
        tables={
            "lefts": TableConfig(
                rows=2,
                columns={
                    "tenant_id": ColumnConfig(generator="sequence", params={"start": 1}),
                    "id": ColumnConfig(generator="sequence", params={"start": 10}),
                },
            ),
            "rights": TableConfig(
                rows=1,
                columns={
                    "tenant_id": ColumnConfig(generator="choice", params={"values": [2]}),
                    "id": ColumnConfig(generator="sequence", params={"start": 20}),
                },
            ),
            "links": TableConfig(
                rows=1,
                columns={"id": ColumnConfig(generator="sequence", params={"start": 30})},
            ),
        },
    )

    dataset = generate_dataset(spec, config)

    assert dataset.quarantine == {}
    assert dataset.tables["links"] == [{"id": 30, "tenant_id": 2, "left_id": 11, "right_id": 20}]


def test_required_fk_with_quarantined_empty_parent_quarantines_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = parse_ddl(
        """
        CREATE TABLE parent (id SERIAL PRIMARY KEY, value INT NOT NULL);
        CREATE TABLE child (
            id SERIAL PRIMARY KEY,
            parent_id INT NOT NULL REFERENCES parent(id)
        );
        """
    )

    def quarantine_parent(batch: list[dict[str, Any]]) -> None:
        if batch and "value" in batch[0]:
            batch[0]["value"] = None

    monkeypatch.setattr(engine, "complete_batch", quarantine_parent)

    dataset = generate_dataset(
        spec,
        Config(tables={"parent": TableConfig(rows=1), "child": TableConfig(rows=1)}),
    )

    assert dataset.tables["parent"] == []
    assert dataset.tables["child"] == []
    assert len(dataset.quarantine["parent"]) == 1
    assert len(dataset.quarantine["child"]) == 1


def test_match_full_root_does_not_become_partially_null_via_shared_fk() -> None:
    spec = parse_ddl(
        """
        CREATE TABLE parent (
            tenant_id INT NOT NULL,
            id INT NOT NULL,
            PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE offers (
            id INT PRIMARY KEY,
            tenant_id INT,
            parent_id INT,
            previous_id INT,
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, parent_id) REFERENCES parent(tenant_id, id),
            FOREIGN KEY (tenant_id, previous_id)
                REFERENCES offers(tenant_id, id) MATCH FULL
        );
        """
    )
    dataset = generate_dataset(
        spec,
        Config(
            tables={
                "parent": TableConfig(
                    rows=1,
                    columns={
                        "tenant_id": ColumnConfig(generator="sequence", params={"start": 1}),
                        "id": ColumnConfig(generator="sequence", params={"start": 10}),
                    },
                ),
                "offers": TableConfig(
                    rows=1,
                    columns={"id": ColumnConfig(generator="sequence", params={"start": 30})},
                ),
            },
            hierarchy={"offers.previous_id": HierarchyConfig(branching=2, max_depth=1)},
        ),
    )

    assert dataset.quarantine == {}
    row = dataset.tables["offers"][0]
    assert row["tenant_id"] is None
    assert row["parent_id"] is None
    assert row["previous_id"] is None


def test_qualified_self_reference_uses_the_canonical_table_name() -> None:
    spec = parse_ddl(
        "CREATE TABLE public.employees ("
        "id SERIAL PRIMARY KEY, "
        "manager_id INT REFERENCES public.employees(id), "
        "name TEXT NOT NULL"
        ");"
    )
    dataset = generate_dataset(
        spec,
        Config(
            seed=3,
            tables={"employees": TableConfig(rows=4)},
            hierarchy={"employees.manager_id": HierarchyConfig(branching=2, max_depth=3)},
        ),
    )

    rows = dataset.tables["employees"]
    ids = {row["id"] for row in rows}
    assert len(rows) == 4
    assert dataset.quarantine == {}
    assert rows[0]["manager_id"] is None
    assert all(row["manager_id"] is None or row["manager_id"] in ids for row in rows)


def test_qualified_non_nullable_self_reference_points_roots_to_themselves() -> None:
    spec = parse_ddl(
        "CREATE TABLE public.employees ("
        "id SERIAL PRIMARY KEY, "
        "manager_id INT NOT NULL REFERENCES public.employees(id), "
        "name TEXT NOT NULL"
        ");"
    )
    dataset = generate_dataset(
        spec,
        Config(
            seed=3,
            tables={"employees": TableConfig(rows=4)},
            hierarchy={"employees.manager_id": HierarchyConfig(branching=2, max_depth=3)},
        ),
    )

    rows = dataset.tables["employees"]
    levels = dataset.levels["employees"]
    by_id = {row["id"]: level for row, level in zip(rows, levels, strict=True)}
    assert dataset.quarantine == {}
    assert len(rows) == len(levels) == 4
    for row, level in zip(rows, levels, strict=True):
        if level == 0:
            assert row["manager_id"] == row["id"]
        else:
            assert by_id[row["manager_id"]] == level - 1


def test_qualified_self_reference_closure_uses_canonical_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = parse_ddl(
        "CREATE TABLE public.employees ("
        "id SERIAL PRIMARY KEY, "
        "manager_id INT REFERENCES public.employees(id), "
        "name TEXT NOT NULL"
        ");"
    )
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("name", 1, "name"))

    dataset = generate_dataset(
        spec,
        Config(
            seed=3,
            tables={"employees": TableConfig(rows=4)},
            hierarchy={"employees.manager_id": HierarchyConfig(branching=2, max_depth=3)},
        ),
    )

    assert dataset.tables["employees"] == []
    assert len(dataset.quarantine["employees"]) == 4
    assert dataset.levels["employees"] == []
    assert dataset.updates == []


def test_qualified_foreign_key_uses_the_parent_canonical_name() -> None:
    spec = parse_ddl(
        "CREATE TABLE public.parent (id SERIAL PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL "
        "REFERENCES public.parent(id));"
    )
    dataset = generate_dataset(
        spec,
        Config(tables={"parent": TableConfig(rows=3), "child": TableConfig(rows=6)}),
    )

    parent_ids = {row["id"] for row in dataset.tables["parent"]}
    assert dataset.quarantine == {}
    assert all(row["parent_id"] in parent_ids for row in dataset.tables["child"])


def test_qualified_foreign_key_to_unique_uses_the_parent_canonical_name() -> None:
    spec = parse_ddl(
        "CREATE TABLE public.parent ("
        "id SERIAL PRIMARY KEY, code INT NOT NULL UNIQUE"
        ");"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_code INT NOT NULL "
        "REFERENCES public.parent(code));"
    )
    dataset = generate_dataset(
        spec,
        Config(
            tables={
                "parent": TableConfig(
                    rows=3,
                    columns={"code": ColumnConfig(generator="sequence", params={"start": 10})},
                ),
                "child": TableConfig(rows=6),
            }
        ),
    )

    parent_codes = {row["code"] for row in dataset.tables["parent"]}
    assert dataset.quarantine == {}
    assert all(row["parent_code"] in parent_codes for row in dataset.tables["child"])


def test_text_array_and_quarantine_keep_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY, tags TEXT[] NOT NULL);")

    def corrupt_first(batch: list[dict[str, Any]]) -> None:
        if batch and batch[0]["id"] == 1:
            batch[0]["tags"] = "not-an-array"

    monkeypatch.setattr(engine, "complete_batch", corrupt_first)
    dataset = generate_dataset(
        spec,
        Config(tables={"t": TableConfig(rows=5)}, output=OutputConfig(batch_size=5)),
    )

    assert len(dataset.tables["t"]) == 4
    assert all(isinstance(row["tags"], list) for row in dataset.tables["t"])
    assert len(dataset.quarantine["t"]) == 1
    assert "lista" in dataset.quarantine["t"][0][2]


def test_abort_reports_the_table_and_column(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY, value INT NOT NULL);")

    def corrupt(batch: list[dict[str, Any]]) -> None:
        batch[0]["value"] = "bad"

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    with pytest.raises(GenerationError, match=r"tabla t, columnas value"):
        generate_dataset(
            spec,
            Config(
                tables={"t": TableConfig(rows=1)},
                output=OutputConfig(on_error="abort"),
            ),
        )


def test_rule_references_are_checked_before_any_row_is_generated() -> None:
    spec = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY, value INT NOT NULL);")
    config = Config(tables={"t": TableConfig(rows=1, rules=["value >= missing_column"])})
    with pytest.raises(PlanError, match=r"tabla t.*missing_column"):
        generate_dataset(spec, config)


def test_inmobiliaria_rules_foreign_keys_and_uniqueness_end_to_end() -> None:
    base = load_config(_CONFIGS / "inmobiliaria_ejemplo.yaml")
    table_counts = {"clientes": 200, "viviendas": 300, "compraventas": 150, "pagos": 400}
    tables = {
        name: base.tables.get(name, TableConfig()).model_copy(update={"rows": count})
        for name, count in table_counts.items()
    }

    small = generate_dataset(
        _schema("inmobiliaria.sql"),
        base.model_copy(update={"tables": tables, "output": OutputConfig(batch_size=50)}),
    )
    large = generate_dataset(
        _schema("inmobiliaria.sql"),
        base.model_copy(update={"tables": tables, "output": OutputConfig(batch_size=5000)}),
    )

    assert {name: len(rows) for name, rows in small.tables.items()} == table_counts
    assert small.quarantine == {}
    assert _digest(small) == _digest(large)
    vivienda = {row["id"]: row for row in small.tables["viviendas"]}
    assert all(
        row["fecha"].year >= vivienda[row["vivienda_id"]]["anio_construccion"]
        for row in small.tables["compraventas"]
    )
    emails = [row["email"] for row in small.tables["clientes"]]
    assert len(emails) == len(set(emails))


def test_bridge_pairs_are_unique() -> None:
    counts = {
        "clientes": 20,
        "vehiculos": 30,
        "piezas": 25,
        "reparaciones": 40,
        "reparacion_piezas": 100,
    }
    dataset = generate_dataset(
        _schema("taller.sql"),
        Config(seed=19, tables={name: TableConfig(rows=count) for name, count in counts.items()}),
    )
    pairs = [(row["reparacion_id"], row["pieza_id"]) for row in dataset.tables["reparacion_piezas"]]
    assert len(pairs) == len(set(pairs)) == counts["reparacion_piezas"]
    assert dataset.quarantine == {}


def test_multitenant_composite_cycle_and_arrays_are_completed() -> None:
    counts = {"inmobiliarias": 5, "clientes": 30, "matches": 30}
    dataset = generate_dataset(
        _schema("crm_real_minimo.sql"),
        Config(seed=23, tables={name: TableConfig(rows=count) for name, count in counts.items()}),
    )
    client_keys = {(row["inmobiliaria_id"], row["id"]) for row in dataset.tables["clientes"]}
    match_keys = {(row["inmobiliaria_id"], row["id"]) for row in dataset.tables["matches"]}
    assert all(isinstance(row["roles"], list) for row in dataset.tables["clientes"])
    assert all(
        (row["inmobiliaria_id"], row["match_id"]) in match_keys
        for row in dataset.tables["clientes"]
    )
    assert all(
        (row["inmobiliaria_id"], row["cliente_id"]) in client_keys
        for row in dataset.tables["matches"]
    )
    assert dataset.quarantine == {}


# --- Integridad referencial tras cuarentena (revisión sesión E, hallazgo 2) ----


def _null_field_of_id(marker: str, target_id: int, field: str):
    """`complete_batch` que anula `field` de la fila con `id==target_id`.

    Fuerza la cuarentena estructural de una fila concreta (un NOT NULL a NULL) para
    observar el cierre de la cuarentena. `marker` distingue la tabla del lote
    (p. ej. solo `pedidos` tiene `fecha`), ya que `complete_batch` no recibe el
    nombre de la tabla.
    """

    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if marker in row and row.get("id") == target_id:
                row[field] = None

    return corrupt


def test_deferred_cycle_cascades_quarantine_to_referencing_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("fecha", 1, "fecha"))
    dataset = generate_dataset(
        _schema("ciclos_deferrable.sql"),
        Config(
            seed=0,
            tables={"pedidos": TableConfig(rows=200), "facturas": TableConfig(rows=200)},
        ),
    )
    pedido_ids = {row["id"] for row in dataset.tables["pedidos"]}
    factura_ids = {row["id"] for row in dataset.tables["facturas"]}
    assert 1 not in pedido_ids  # el padre corrupto se apartó
    # Ninguna FK no nula queda colgando en ninguno de los dos sentidos del ciclo.
    assert all(row["pedido_id"] in pedido_ids for row in dataset.tables["facturas"])
    assert all(row["factura_id"] in factura_ids for row in dataset.tables["pedidos"])
    # Toda factura que apuntaba al pedido 1 cayó también (cierre de la cuarentena).
    assert all(row["pedido_id"] != 1 for row in dataset.tables["facturas"])
    assert "facturas" in dataset.quarantine  # el cierre alcanzó a las facturas
    # KeyStore/_key_sets reflejan solo las filas aceptadas.
    assert dataset._key_sets["pedidos"] == {(pid,) for pid in pedido_ids}
    assert dataset._key_sets["facturas"] == {(fid,) for fid in factura_ids}


def test_deferred_cycle_abort_still_raises_on_the_corrupted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("fecha", 1, "fecha"))
    with pytest.raises(GenerationError, match=r"tabla pedidos"):
        generate_dataset(
            _schema("ciclos_deferrable.sql"),
            Config(
                seed=0,
                tables={"pedidos": TableConfig(rows=200), "facturas": TableConfig(rows=200)},
                output=OutputConfig(on_error="abort"),
            ),
        )


def test_deferred_cycle_without_corruption_is_integral_and_deterministic() -> None:
    def run(batch_size: int) -> Dataset:
        return generate_dataset(
            _schema("ciclos_deferrable.sql"),
            Config(
                seed=0,
                tables={"pedidos": TableConfig(rows=200), "facturas": TableConfig(rows=200)},
                output=OutputConfig(batch_size=batch_size),
            ),
        )

    small, large = run(7), run(5000)
    assert small.quarantine == {}
    assert _digest(small) == _digest(large)


def test_leveled_root_corruption_cascades_to_its_whole_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El único root (nivel 0, capacidad 1) sostiene todo el árbol: apartarlo debe
    # arrastrar transitivamente a toda su descendencia, sin dejar ninguna colgando.
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("manager_id", 1, "nombre"))
    dataset = generate_dataset(
        _schema("rrhh_autoref_nullable.sql"),
        Config(
            seed=3,
            tables={"empleados": TableConfig(rows=40)},
            hierarchy={"empleados.manager_id": HierarchyConfig(branching=3, max_depth=4)},
        ),
    )
    assert dataset.tables["empleados"] == []
    assert dataset.levels["empleados"] == []
    assert len(dataset.quarantine["empleados"]) == 40


def test_leveled_intermediate_corruption_keeps_levels_aligned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Apartar un nodo intermedio arrastra su subárbol pero deja supervivientes: los
    # niveles deben seguir 1:1 con las filas aceptadas (no basta con recortar el
    # prefijo del lote).
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("manager_id", 2, "nombre"))
    dataset = generate_dataset(
        _schema("rrhh_autoref_nullable.sql"),
        Config(
            seed=3,
            tables={"empleados": TableConfig(rows=40)},
            hierarchy={"empleados.manager_id": HierarchyConfig(branching=3, max_depth=4)},
        ),
    )
    rows = dataset.tables["empleados"]
    levels = dataset.levels["empleados"]
    ids = {row["id"] for row in rows}
    assert rows  # hay supervivientes (no colapsó todo el árbol)
    assert 2 not in ids
    assert all(row["manager_id"] != 2 for row in rows)  # los subordinados directos cayeron
    # Ninguna autorreferencia no nula queda colgando y los niveles siguen alineados.
    assert all(row["manager_id"] is None or row["manager_id"] in ids for row in rows)
    assert len(rows) == len(levels)
    by_level = {row["id"]: level for row, level in zip(rows, levels, strict=True)}
    for row, level in zip(rows, levels, strict=True):
        if row["manager_id"] is not None:
            assert by_level[row["manager_id"]] == level - 1


def test_leveled_abort_still_raises_on_the_corrupted_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("manager_id", 1, "nombre"))
    with pytest.raises(GenerationError, match=r"tabla empleados"):
        generate_dataset(
            _schema("rrhh_autoref_nullable.sql"),
            Config(
                seed=3,
                tables={"empleados": TableConfig(rows=40)},
                hierarchy={"empleados.manager_id": HierarchyConfig(branching=3, max_depth=4)},
                output=OutputConfig(on_error="abort"),
            ),
        )


def test_leveled_without_corruption_is_integral_and_deterministic() -> None:
    def run(batch_size: int) -> Dataset:
        return generate_dataset(
            _schema("rrhh_autoref_nullable.sql"),
            Config(
                seed=3,
                tables={"empleados": TableConfig(rows=40)},
                hierarchy={"empleados.manager_id": HierarchyConfig(branching=3, max_depth=4)},
                output=OutputConfig(batch_size=batch_size),
            ),
        )

    small, large = run(6), run(5000)
    assert small.quarantine == {}
    assert len(small.tables["empleados"]) == len(small.levels["empleados"]) == 40
    assert _digest(small) == _digest(large)


# --- FKs que referencian UNIQUE distintas de la PK (revisión sesión E, hallazgo 3) --


def _fk_unique_target_tables() -> dict[str, TableConfig]:
    """Config de `fk_unique_target.sql`: columnas UNIQUE por `sequence`/`template`.

    Evita heurísticas por defecto que no son el objeto de este test (p. ej. una
    columna llamada `code` puede heurísticamente resolver a texto) y una
    plantilla `numero` sin recortar por longitud de tabla, que colisionaría.
    """
    return {
        "parent": TableConfig(
            rows=20, columns={"code": ColumnConfig(generator="sequence", params={"start": 1000})}
        ),
        "child": TableConfig(rows=40),
        "grandchild": TableConfig(rows=60),
        "parent_composite": TableConfig(
            rows=15, columns={"a": ColumnConfig(generator="sequence", params={"start": 1})}
        ),
        "child_composite": TableConfig(rows=30),
        "parent_reordered": TableConfig(
            rows=15, columns={"a": ColumnConfig(generator="sequence", params={"start": 1})}
        ),
        "child_reordered": TableConfig(rows=30),
        "pedidos_uk": TableConfig(
            rows=200,
            columns={"codigo": ColumnConfig(generator="sequence", params={"start": 5000})},
        ),
        "facturas_uk": TableConfig(
            rows=200,
            columns={"numero": ColumnConfig(generator="template", params={"template": "F{n}"})},
        ),
        "detalle_uk": TableConfig(rows=300),
    }


def test_fk_references_a_unique_column_that_is_not_the_primary_key() -> None:
    # Reproducción mínima obligatoria: child.parent_code REFERENCES parent(code),
    # una UNIQUE, no parent.id (la PK). Las filas hijas deben sobrevivir y
    # parent_code debe existir entre parent.code.
    dataset = generate_dataset(
        _schema("fk_unique_target.sql"), Config(seed=0, tables=_fk_unique_target_tables())
    )
    assert dataset.quarantine == {}
    codes = {row["code"] for row in dataset.tables["parent"]}
    parent_ids = {row["id"] for row in dataset.tables["parent"]}
    assert all(row["parent_code"] in codes for row in dataset.tables["child"])
    # Si la validación comparase (por error) contra la PK en vez de contra
    # `code`, esto lo delataría: los rangos de id (1..20) y code (1000..1019)
    # de este fixture no se solapan.
    assert not ({row["parent_code"] for row in dataset.tables["child"]} & parent_ids)


def test_fk_composite_references_a_composite_unique_not_the_primary_key() -> None:
    dataset = generate_dataset(
        _schema("fk_unique_target.sql"), Config(seed=0, tables=_fk_unique_target_tables())
    )
    assert dataset.quarantine == {}
    parent_pairs = {(row["a"], row["b"]) for row in dataset.tables["parent_composite"]}
    assert all((row["x"], row["y"]) in parent_pairs for row in dataset.tables["child_composite"])


def test_fk_ref_columns_in_different_order_than_the_composite_primary_key() -> None:
    # child_reordered(x, y) REFERENCES parent_reordered(b, a): x debe casar con
    # `b` del padre e y con `a` -el orden de ref_columns-, no con la PK en su
    # propio orden declarado (a, b).
    dataset = generate_dataset(
        _schema("fk_unique_target.sql"), Config(seed=0, tables=_fk_unique_target_tables())
    )
    assert dataset.quarantine == {}
    parents = {(row["a"], row["b"]) for row in dataset.tables["parent_reordered"]}
    children = dataset.tables["child_reordered"]
    assert children  # hay filas que comprobar
    for row in children:
        assert (row["y"], row["x"]) in parents  # (a, b) = (y, x), no (x, y)


def test_quarantined_unique_referenced_parent_cascades_transitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pedidos_uk <-> facturas_uk es un ciclo diferible (mecanismo de
    # ciclos_deferrable.sql) donde facturas_uk referencia pedidos_uk.codigo
    # (UNIQUE, no su PK). Corromper un pedido debe cuarentenar las facturas que
    # lo referenciaban POR codigo, y la cascada debe alcanzar detalle_uk -un
    # tercer salto fuera del ciclo, acíclico y por PK-: cierre transitivo sin
    # ninguna FK colgante en ningún salto.
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("fecha", 1, "fecha"))
    dataset = generate_dataset(
        _schema("fk_unique_target.sql"), Config(seed=0, tables=_fk_unique_target_tables())
    )
    assert set(dataset.quarantine) == {"pedidos_uk", "facturas_uk", "detalle_uk"}
    pedidos_codigos = {row["codigo"] for row in dataset.tables["pedidos_uk"]}
    facturas_ids = {row["id"] for row in dataset.tables["facturas_uk"]}
    assert 5000 not in pedidos_codigos  # codigo del pedido 1 (corrupto) desapareció
    assert all(row["pedido_codigo"] in pedidos_codigos for row in dataset.tables["facturas_uk"])
    assert all(row["factura_id"] in facturas_ids for row in dataset.tables["pedidos_uk"])
    assert all(row["factura_id"] in facturas_ids for row in dataset.tables["detalle_uk"])


# --- Dataset.updates coherente tras cuarentena (revisión sesión E, hallazgo 4) ------


def test_updates_stay_coherent_after_referential_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El cierre referencial de ciclos_deferrable.sql cuarentena varias filas
    # (revisión, hallazgo 2): Dataset.updates no puede seguir apuntando a
    # row_index de la lista sin filtrar. Comprueba las tres postcondiciones
    # del hallazgo 4 directamente sobre el resultado público.
    monkeypatch.setattr(engine, "complete_batch", _null_field_of_id("fecha", 1, "fecha"))
    dataset = generate_dataset(
        _schema("ciclos_deferrable.sql"),
        Config(
            seed=0,
            tables={"pedidos": TableConfig(rows=200), "facturas": TableConfig(rows=200)},
        ),
    )
    assert dataset.updates  # sigue habiendo actualizaciones que comprobar
    for update in dataset.updates:
        rows = dataset.tables[update.table]
        assert 0 <= update.row_index < len(rows)  # nunca fuera de rango
        row = rows[update.row_index]
        # Apunta a la MISMA fila para la que se creó: sus valores actuales
        # siguen coincidiendo con los que la actualización registró.
        assert all(row[column] == value for column, value in update.values.items())
    # ciclos_deferrable.sql solo tiene una FK diferida por tabla, así que cada
    # fila recibe como mucho una actualización: los índices no deben repetirse.
    by_table: dict[str, list[int]] = {}
    for update in dataset.updates:
        by_table.setdefault(update.table, []).append(update.row_index)
    for indices in by_table.values():
        assert len(indices) == len(set(indices))
