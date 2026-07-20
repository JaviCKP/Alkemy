"""End-to-end contracts for the in-memory generation engine (T2.11-T2.13)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from synthdb.config.loader import load_config
from synthdb.config.models import Config, HierarchyConfig, OutputConfig, TableConfig
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
