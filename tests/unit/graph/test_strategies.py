"""Tests de src/synthdb/graph/strategies.py (T1.7, especificacion.md §6.2-6.3).

`resolve_cycles` recibe el `StructuralPlan` de `graph/dependency.py` (T1.6,
ya probado en `test_dependency.py`) y la `SchemaSpec` correspondiente, y
produce la secuencia final de `Phase` que ejecutará el motor de
generación. Todos los tests parsean SQL real con `parse_ddl` y encadenan
`analyze_structure` antes de `resolve_cycles`: lo que se ejercita es la
estrategia de generación, no un `StructuralPlan` construido a mano.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from synthdb.graph.dependency import analyze_structure
from synthdb.graph.strategies import UnbreakableCycle, resolve_cycles
from synthdb.ir.plans import (
    DeferredPhase,
    InsertLeveledPhase,
    InsertPhase,
    Phase,
    StructuralPlan,
    UpdatePhase,
)
from synthdb.ir.schema import SchemaSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _plan_and_schema(sql: str) -> tuple[StructuralPlan, SchemaSpec]:
    schema = parse_ddl(sql)
    plan = analyze_structure(schema)
    return plan, schema


def _resolve_fixture(name: str) -> tuple[list[Phase], StructuralPlan]:
    sql = (_SCHEMAS_DIR / f"{name}.sql").read_text(encoding="utf-8")
    schema = parse_ddl(sql)
    plan = analyze_structure(schema)
    return resolve_cycles(plan, schema), plan


# --- SCC de 1 tabla sin autorreferencia ⇒ InsertPhase simple ------------------


def test_single_table_scc_without_self_reference_is_a_plain_insert() -> None:
    sql = "CREATE TABLE t (id INT PRIMARY KEY, nombre TEXT NOT NULL);"
    schema = parse_ddl(sql)
    plan = analyze_structure(schema)

    phases = resolve_cycles(plan, schema)

    assert phases == [InsertPhase(tables=["t"])]


def test_independent_tables_in_the_same_structural_phase_are_fused_into_one_insert() -> None:
    # taller: clientes y piezas comparten fase 0 (T1.6) y no forman ciclo:
    # deben fusionarse en un único InsertPhase, orden alfabético.
    phases, plan = _resolve_fixture("taller")

    assert plan.tables_by_phase[0] == ["clientes", "piezas"]
    assert phases[0] == InsertPhase(tables=["clientes", "piezas"])


# --- Autorreferencias (especificacion.md §6.3) --------------------------------


def test_nullable_self_reference_produces_leveled_insert_without_warning() -> None:
    phases, plan = _resolve_fixture("rrhh_autoref_nullable")

    assert phases == [InsertLeveledPhase(table="empleados", self_fk_columns=["manager_id"])]
    assert plan.warnings == []


def test_not_null_non_deferrable_self_reference_forces_roots_to_point_to_self_and_warns() -> None:
    phases, plan = _resolve_fixture("rrhh_autoref_notnull")

    assert phases == [
        InsertLeveledPhase(
            table="empleados", self_fk_columns=["manager_id"], roots_point_to_self=True
        )
    ]
    assert len(plan.warnings) == 1
    warning = plan.warnings[0]
    assert "empleados" in warning
    assert "manager_id" in warning


def test_deferrable_self_reference_is_deferred() -> None:
    sql = """
        CREATE TABLE empleados (
            id SERIAL PRIMARY KEY,
            manager_id INT NOT NULL REFERENCES empleados(id) DEFERRABLE INITIALLY DEFERRED
        );
    """
    plan, schema = _plan_and_schema(sql)

    phases = resolve_cycles(plan, schema)

    assert phases == [DeferredPhase(tables=["empleados"])]
    assert plan.warnings == []


# --- Ciclos entre tablas (especificacion.md §6.2) -----------------------------


def test_cycle_breakable_by_a_nullable_fk_produces_insert_with_null_fks_then_update() -> None:
    phases, plan = _resolve_fixture("ciclos_nullable")

    assert plan.sccs == [["facturas", "pedidos"]]
    assert len(phases) == 2

    insert, update = phases
    assert isinstance(insert, InsertPhase)
    # pedidos primero: facturas.pedido_id es NOT NULL y exige que pedidos ya exista.
    assert insert.tables == ["pedidos", "facturas"]
    assert len(insert.null_fks) == 1
    assert insert.null_fks[0].table == "pedidos"
    assert insert.null_fks[0].columns == ["factura_id"]
    assert insert.null_fks[0].ref_table == "facturas"

    assert update == UpdatePhase(table="pedidos", columns=["factura_id"])


def test_cycle_breakable_only_by_a_deferrable_fk_is_deferred() -> None:
    phases, plan = _resolve_fixture("ciclos_deferrable")

    assert plan.sccs == [["facturas", "pedidos"]]
    assert phases == [DeferredPhase(tables=["facturas", "pedidos"])]


def test_unbreakable_cycle_raises_with_tables_and_fks_in_the_diagnostic() -> None:
    sql = (_SCHEMAS_DIR / "ciclos_unbreakable.sql").read_text(encoding="utf-8")
    schema = parse_ddl(sql)
    plan = analyze_structure(schema)

    with pytest.raises(UnbreakableCycle) as exc_info:
        resolve_cycles(plan, schema)

    error = exc_info.value
    assert error.tables == ["facturas", "pedidos"]
    assert {(edge.table, tuple(edge.columns)) for edge in error.edges} == {
        ("facturas", ("pedido_id",)),
        ("pedidos", ("factura_id",)),
    }

    message = str(error)
    assert "facturas" in message
    assert "pedidos" in message
    assert "pedido_id" in message
    assert "factura_id" in message
    # las tres salidas posibles, mencionadas explícitamente (especificacion.md §6.2)
    assert "anulable" in message.lower()
    assert "deferrable" in message.lower()
    assert "--allow-ddl" in message


# --- Determinismo --------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "cementerio",
        "ciclos_deferrable",
        "ciclos_nullable",
        "ecommerce",
        "inmobiliaria",
        "opaco",
        "rrhh_autoref_notnull",
        "rrhh_autoref_nullable",
        "taller",
    ],
)
def test_resolve_cycles_is_deterministic_across_repeated_runs(fixture_name: str) -> None:
    sql = (_SCHEMAS_DIR / f"{fixture_name}.sql").read_text(encoding="utf-8")

    results = []
    for _ in range(10):
        schema = parse_ddl(sql)
        plan = analyze_structure(schema)
        results.append(resolve_cycles(plan, schema))

    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("fixture_name", ["taller", "ciclos_nullable"])
def test_full_pipeline_is_deterministic_across_processes_and_hash_seeds(
    fixture_name: str, tmp_path: Path
) -> None:
    """`parse_ddl` → `analyze_structure` → `resolve_cycles` es determinista
    byte a byte con independencia de `PYTHONHASHSEED` (CLAUDE.md): ninguna
    de las tres funciones puede depender de `hash()` de Python ni del orden
    de iteración de un `set`/`dict` sin ordenar antes de usarlo."""
    sql = (_SCHEMAS_DIR / f"{fixture_name}.sql").read_text(encoding="utf-8")
    script = tmp_path / "run_pipeline.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json

            from synthdb.graph.dependency import analyze_structure
            from synthdb.graph.strategies import resolve_cycles
            from synthdb.parsing.ddl import parse_ddl

            schema = parse_ddl({sql!r})
            plan = analyze_structure(schema)
            phases = resolve_cycles(plan, schema)

            payload = {{
                "plan": plan.model_dump(mode="json"),
                "phases": [phase.model_dump(mode="json") for phase in phases],
            }}
            print(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
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
