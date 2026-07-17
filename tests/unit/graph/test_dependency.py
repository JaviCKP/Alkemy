"""Tests de src/synthdb/graph/dependency.py (T1.6, especificacion.md §6.1 y §6.4).

`analyze_structure` recibe la `SchemaSpec` ya parseada (`parsing/ddl.py`) y
devuelve un `StructuralPlan` con las fases de dependencia, los ciclos y
autorreferencias detectados y las tablas puente; de paso rellena, mutando
`spec` in situ, los dos campos derivados que le corresponden
(`TableSpec.kind` y `RelationshipSpec.cardinality_hint`). Ninguno de los
dos está en el hash canónico (`ir/hashing.py`), así que `schema_hash` debe
quedar inalterado antes y después de la llamada.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import networkx as nx
import pytest

from synthdb.graph.dependency import analyze_structure, phase_layers
from synthdb.ir.hashing import schema_hash
from synthdb.ir.schema import SchemaSpec, TableSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

_ALL_FIXTURES = [
    "cementerio",
    "ciclos_deferrable",
    "ciclos_nullable",
    "ciclos_unbreakable",
    "ecommerce",
    "inmobiliaria",
    "opaco",
    "rrhh_autoref_notnull",
    "rrhh_autoref_nullable",
    "taller",
]


def _table(schema: SchemaSpec, name: str) -> TableSpec:
    for table in schema.tables:
        if table.name == name:
            return table
    raise AssertionError(f"tabla {name!r} no encontrada en {[t.name for t in schema.tables]}")


def _parse_fixture(name: str) -> SchemaSpec:
    sql = (_SCHEMAS_DIR / f"{name}.sql").read_text(encoding="utf-8")
    return parse_ddl(sql)


# --- inmobiliaria: fases exactas, sin ciclos ---------------------------------


def test_inmobiliaria_phases_are_a_strict_chain_with_no_fusion() -> None:
    schema = _parse_fixture("inmobiliaria")

    plan = analyze_structure(schema)

    assert plan.tables_by_phase == [
        ["clientes"],
        ["viviendas"],
        ["compraventas"],
        ["pagos"],
    ]
    assert plan.sccs == []
    assert plan.self_refs == []
    assert plan.bridges == []
    assert plan.warnings == []


def test_inmobiliaria_all_relationships_are_many_to_one() -> None:
    schema = _parse_fixture("inmobiliaria")

    analyze_structure(schema)

    hints = {
        (table.name, tuple(fk.columns)): fk.cardinality_hint
        for table in schema.tables
        for fk in table.foreign_keys
    }
    assert hints == {
        ("viviendas", ("propietario_id",)): "many_to_one",
        ("compraventas", ("vivienda_id",)): "many_to_one",
        ("compraventas", ("comprador_id",)): "many_to_one",
        ("pagos", ("compraventa_id",)): "many_to_one",
    }


def test_inmobiliaria_tables_stay_regular() -> None:
    schema = _parse_fixture("inmobiliaria")

    analyze_structure(schema)

    assert {table.name: table.kind for table in schema.tables} == {
        "clientes": "regular",
        "viviendas": "regular",
        "compraventas": "regular",
        "pagos": "regular",
    }


# --- taller: tabla puente, lookup y fusión de independientes -----------------


def test_taller_independent_tables_are_fused_into_the_same_phase() -> None:
    schema = _parse_fixture("taller")

    plan = analyze_structure(schema)

    # clientes y piezas no dependen la una de la otra: comparten fase 0.
    assert plan.tables_by_phase[0] == ["clientes", "piezas"]


def test_taller_bridge_table_is_detected_and_phased_after_both_parents() -> None:
    schema = _parse_fixture("taller")

    plan = analyze_structure(schema)

    assert plan.bridges == ["reparacion_piezas"]
    assert _table(schema, "reparacion_piezas").kind == "bridge"

    bridge_phase = next(i for i, p in enumerate(plan.tables_by_phase) if "reparacion_piezas" in p)
    reparaciones_phase = next(i for i, p in enumerate(plan.tables_by_phase) if "reparaciones" in p)
    piezas_phase = next(i for i, p in enumerate(plan.tables_by_phase) if "piezas" in p)

    assert bridge_phase > reparaciones_phase
    assert bridge_phase > piezas_phase


def test_taller_small_referenced_tables_without_fks_are_lookup() -> None:
    schema = _parse_fixture("taller")

    analyze_structure(schema)

    assert _table(schema, "clientes").kind == "lookup"
    assert _table(schema, "piezas").kind == "lookup"


def test_taller_all_relationships_are_many_to_one() -> None:
    schema = _parse_fixture("taller")

    analyze_structure(schema)

    hints = {
        (table.name, tuple(fk.columns)): fk.cardinality_hint
        for table in schema.tables
        for fk in table.foreign_keys
    }
    assert hints == {
        ("vehiculos", ("cliente_id",)): "many_to_one",
        ("reparaciones", ("vehiculo_id",)): "many_to_one",
        ("reparacion_piezas", ("reparacion_id",)): "many_to_one",
        ("reparacion_piezas", ("pieza_id",)): "many_to_one",
    }


# --- UNIQUE sobre FK ⇒ one_to_one --------------------------------------------


def test_unique_column_that_is_also_a_foreign_key_is_one_to_one() -> None:
    # cementerio.entierros.persona_id: "INT NOT NULL UNIQUE REFERENCES personas(id)".
    schema = _parse_fixture("cementerio")

    analyze_structure(schema)

    entierros = _table(schema, "entierros")
    persona_fk = next(fk for fk in entierros.foreign_keys if fk.columns == ["persona_id"])
    sepultura_fk = next(fk for fk in entierros.foreign_keys if fk.columns == ["sepultura_id"])

    assert persona_fk.cardinality_hint == "one_to_one"
    # sepultura_id no lleva UNIQUE: varias personas pueden compartir sepultura.
    assert sepultura_fk.cardinality_hint == "many_to_one"


def test_inline_unique_on_a_composite_foreign_key_is_one_to_one() -> None:
    sql = """
        CREATE TABLE a (id INT PRIMARY KEY);
        CREATE TABLE b (id INT PRIMARY KEY);
        CREATE TABLE ab (
            a_id INT NOT NULL,
            b_id INT NOT NULL,
            FOREIGN KEY (a_id, b_id) REFERENCES a(id),
            UNIQUE (a_id, b_id)
        );
    """
    schema = parse_ddl(sql)

    analyze_structure(schema)

    fk = _table(schema, "ab").foreign_keys[0]
    assert fk.cardinality_hint == "one_to_one"


def test_unique_that_only_partially_covers_the_foreign_key_is_not_one_to_one() -> None:
    # pagos.compraventa_id no es 1:1: el UNIQUE es (compraventa_id, num_plazo).
    schema = _parse_fixture("inmobiliaria")

    analyze_structure(schema)

    fk = _table(schema, "pagos").foreign_keys[0]
    assert fk.cardinality_hint == "many_to_one"


# --- Autorreferencias: self_reference + no crean arista -----------------------


@pytest.mark.parametrize("fixture_name", ["rrhh_autoref_nullable", "rrhh_autoref_notnull"])
def test_self_reference_is_detected_and_does_not_create_a_graph_edge(fixture_name: str) -> None:
    schema = _parse_fixture(fixture_name)

    plan = analyze_structure(schema)

    assert plan.self_refs == ["empleados"]
    assert plan.sccs == []  # la autorreferencia no es un ciclo entre tablas distintas
    assert plan.tables_by_phase == [["empleados"]]

    fk = _table(schema, "empleados").foreign_keys[0]
    assert fk.cardinality_hint == "self_reference"


# --- Ciclos entre tablas: SCC de 2+ ------------------------------------------


@pytest.mark.parametrize(
    "fixture_name", ["ciclos_nullable", "ciclos_deferrable", "ciclos_unbreakable"]
)
def test_two_table_cycle_is_a_single_scc_in_a_single_phase(fixture_name: str) -> None:
    schema = _parse_fixture(fixture_name)

    plan = analyze_structure(schema)

    assert plan.sccs == [["facturas", "pedidos"]]
    assert plan.tables_by_phase == [["facturas", "pedidos"]]
    assert plan.self_refs == []


# --- FK hacia tabla ausente: aviso, nunca crash ------------------------------


def test_foreign_key_to_a_table_missing_from_the_schema_warns_and_does_not_crash() -> None:
    sql = "CREATE TABLE t (id INT PRIMARY KEY, other_id INT REFERENCES noexiste(id));"
    schema = parse_ddl(sql)

    plan = analyze_structure(schema)

    assert any("t" in warning and "noexiste" in warning for warning in plan.warnings), plan.warnings
    # la tabla sigue plan planificada con normalidad, sin arista fantasma
    assert plan.tables_by_phase == [["t"]]
    assert _table(schema, "t").kind == "regular"
    # sin tabla destino no hay forma honesta de clasificar la cardinalidad
    assert _table(schema, "t").foreign_keys[0].cardinality_hint is None


# --- Determinismo -------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", _ALL_FIXTURES)
def test_analyze_structure_is_deterministic_across_repeated_runs(fixture_name: str) -> None:
    sql = (_SCHEMAS_DIR / f"{fixture_name}.sql").read_text(encoding="utf-8")

    plans = [analyze_structure(parse_ddl(sql)) for _ in range(10)]

    assert all(plan == plans[0] for plan in plans)


def test_hash_is_stable_across_processes_and_hash_seeds_after_analyze_structure(
    tmp_path: Path,
) -> None:
    """El plan estructural no depende de PYTHONHASHSEED (CLAUDE.md): nunca de
    `hash()` de Python ni del orden de iteración de un `set`/`dict` sin
    ordenar explícitamente antes de usarlo."""
    sql = (_SCHEMAS_DIR / "taller.sql").read_text(encoding="utf-8")
    script = tmp_path / "run_analyze_structure.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from synthdb.graph.dependency import analyze_structure
            from synthdb.ir.schema import canonical_json
            from synthdb.parsing.ddl import parse_ddl

            schema = parse_ddl({sql!r})
            plan = analyze_structure(schema)
            print(canonical_json(plan))
            """
        ),
        encoding="utf-8",
    )

    def _run_with_seed(seed: str) -> str:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        return result.stdout.strip()

    outputs = {_run_with_seed(seed) for seed in ("0", "1", "42")}

    assert len(outputs) == 1


# --- Invariante de hash --------------------------------------------------------


def test_schema_hash_is_unchanged_after_filling_kind_and_cardinality_hint() -> None:
    schema = _parse_fixture("taller")
    hash_before = schema_hash(schema)

    analyze_structure(schema)

    # de verdad se han rellenado los campos derivados (si no, el test de abajo
    # no comprobaría nada interesante)
    assert _table(schema, "reparacion_piezas").kind == "bridge"
    assert all(fk.cardinality_hint is not None for t in schema.tables for fk in t.foreign_keys)
    assert schema_hash(schema) == hash_before


# --- phase_layers: casos borde -------------------------------------------------


def test_phase_layers_on_a_graph_with_no_nodes_returns_no_phases() -> None:
    assert phase_layers(nx.DiGraph()) == []


def test_phase_layers_on_isolated_nodes_fuses_them_all_into_one_phase() -> None:
    g = nx.DiGraph()
    g.add_nodes_from(["b", "a", "c"])

    assert phase_layers(g) == [["a", "b", "c"]]
