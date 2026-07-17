"""Tests de src/synthdb/ir/hashing.py (T1.5, especificacion.md §5 y §13)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from synthdb.ir.hashing import schema_hash
from synthdb.ir.schema import (
    CheckSpec,
    ColumnSpec,
    RelationshipSpec,
    SchemaSpec,
    TableSpec,
    TypeSpec,
)

_HEX_64 = re.compile(r"[0-9a-f]{64}")


def _build_example_schema() -> SchemaSpec:
    """Esquema de dos tablas (`clientes` → `pedidos`) con FK y checks anidados."""
    clientes = TableSpec(
        name="clientes",
        kind="regular",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="nombre", type=TypeSpec(kind="text"), nullable=False),
            ColumnSpec(name="email", type=TypeSpec(kind="text"), nullable=False),
        ],
        primary_key=["id"],
        uniques=[["email"]],
    )

    pedidos = TableSpec(
        name="pedidos",
        kind="regular",
        columns=[
            ColumnSpec(
                name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
            ),
            ColumnSpec(name="cliente_id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(
                name="total",
                type=TypeSpec(kind="numeric", precision=10, scale=2),
                nullable=False,
                checks=[
                    CheckSpec(
                        sql_text="total > 0",
                        ast_supported=True,
                        columns_involved=["total"],
                        bounds_derived={"min_exclusive": 0},
                    ),
                ],
            ),
        ],
        primary_key=["id"],
        foreign_keys=[
            RelationshipSpec(
                columns=["cliente_id"],
                ref_table="clientes",
                ref_columns=["id"],
                nullable=False,
                cardinality_hint="many_to_one",
            ),
        ],
        checks=[
            CheckSpec(
                sql_text="total > 0",
                ast_supported=True,
                columns_involved=["total"],
                bounds_derived={"min_exclusive": 0},
            ),
        ],
    )

    return SchemaSpec(dialect="postgres", tables=[clientes, pedidos])


def _with_table(schema: SchemaSpec, index: int, table: TableSpec) -> SchemaSpec:
    tables = list(schema.tables)
    tables[index] = table
    return schema.model_copy(update={"tables": tables})


def test_same_schema_hashed_twice_is_identical() -> None:
    schema = _build_example_schema()

    assert schema_hash(schema) == schema_hash(schema)


def test_hash_is_stable_across_processes_and_hash_seeds(tmp_path: Path) -> None:
    """El hash no depende de PYTHONHASHSEED ni del proceso: nunca usa hash() de Python."""
    script = tmp_path / "compute_hash.py"
    script.write_text(
        textwrap.dedent(
            """
            from synthdb.ir.hashing import schema_hash
            from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec

            schema = SchemaSpec(
                dialect="postgres",
                tables=[
                    TableSpec(
                        name="clientes",
                        columns=[
                            ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False),
                            ColumnSpec(name="email", type=TypeSpec(kind="text"), nullable=False),
                        ],
                        primary_key=["id"],
                    ),
                ],
            )
            print(schema_hash(schema))
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


def test_hash_is_64_char_hex_digest() -> None:
    digest = schema_hash(_build_example_schema())

    assert _HEX_64.fullmatch(digest)


def test_reordering_tables_does_not_change_hash() -> None:
    schema = _build_example_schema()
    reordered = schema.model_copy(update={"tables": list(reversed(schema.tables))})

    assert schema_hash(schema) == schema_hash(reordered)


def test_reordering_homonymous_tables_in_different_schemas_does_not_change_hash() -> None:
    """`name` solo no basta: dos tablas `users` en namespaces distintos deben
    desempatar por `schema`, no por orden de entrada.
    """
    ventas_users = TableSpec(
        name="users",
        schema="ventas",
        columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
        primary_key=["id"],
    )
    rrhh_users = TableSpec(
        name="users",
        schema="rrhh",
        columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
        primary_key=["id"],
    )

    first = SchemaSpec(dialect="postgres", tables=[ventas_users, rrhh_users])
    second = SchemaSpec(dialect="postgres", tables=[rrhh_users, ventas_users])

    assert schema_hash(first) == schema_hash(second)


def test_reordering_columns_changes_hash() -> None:
    schema = _build_example_schema()
    clientes = schema.tables[0]
    reordered_columns = [clientes.columns[0], clientes.columns[2], clientes.columns[1]]
    reordered = _with_table(schema, 0, clientes.model_copy(update={"columns": reordered_columns}))

    assert schema_hash(schema) != schema_hash(reordered)


def test_changing_a_structural_attribute_changes_hash() -> None:
    schema = _build_example_schema()
    clientes = schema.tables[0]
    nullable_nombre = clientes.columns[1].model_copy(update={"nullable": True})
    columns = [clientes.columns[0], nullable_nombre, clientes.columns[2]]
    mutated = _with_table(schema, 0, clientes.model_copy(update={"columns": columns}))

    assert schema_hash(schema) != schema_hash(mutated)


def test_changing_schema_hash_field_does_not_change_hash() -> None:
    schema = _build_example_schema()
    mutated = schema.model_copy(update={"hash": "f" * 64})

    assert schema_hash(schema) == schema_hash(mutated)


def test_changing_schema_warnings_does_not_change_hash() -> None:
    schema = _build_example_schema()
    mutated = schema.model_copy(update={"warnings": ["aviso nuevo"]})

    assert schema_hash(schema) == schema_hash(mutated)


def test_changing_table_kind_does_not_change_hash() -> None:
    schema = _build_example_schema()
    mutated = _with_table(schema, 0, schema.tables[0].model_copy(update={"kind": "lookup"}))

    assert schema_hash(schema) == schema_hash(mutated)


def test_changing_relationship_cardinality_hint_does_not_change_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    fk = pedidos.foreign_keys[0].model_copy(update={"cardinality_hint": "one_to_one"})
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"foreign_keys": [fk]}))

    assert schema_hash(schema) == schema_hash(mutated)


# --- ADR-004: is_array/match_full/on_delete_set_columns son estructurales -----
# --- (entran en el hash); nullable_columns es derivado (no entra) -------------


def test_changing_type_is_array_changes_hash() -> None:
    schema = _build_example_schema()
    clientes = schema.tables[0]
    nombre = clientes.columns[1]
    array_type = nombre.type.model_copy(update={"is_array": True})
    array_nombre = nombre.model_copy(update={"type": array_type})
    columns = [clientes.columns[0], array_nombre, clientes.columns[2]]
    mutated = _with_table(schema, 0, clientes.model_copy(update={"columns": columns}))

    assert schema_hash(schema) != schema_hash(mutated)


def test_changing_relationship_match_full_changes_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    fk = pedidos.foreign_keys[0].model_copy(update={"match_full": True})
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"foreign_keys": [fk]}))

    assert schema_hash(schema) != schema_hash(mutated)


def test_changing_relationship_on_delete_set_columns_changes_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    fk = pedidos.foreign_keys[0].model_copy(
        update={"on_delete": "set_null", "on_delete_set_columns": ["cliente_id"]}
    )
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"foreign_keys": [fk]}))

    assert schema_hash(schema) != schema_hash(mutated)


def test_changing_relationship_nullable_columns_does_not_change_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    fk = pedidos.foreign_keys[0].model_copy(update={"nullable_columns": ["cliente_id"]})
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"foreign_keys": [fk]}))

    assert schema_hash(schema) == schema_hash(mutated)


def test_changing_check_ast_supported_does_not_change_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    check = pedidos.checks[0].model_copy(update={"ast_supported": False})
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"checks": [check]}))

    assert schema_hash(schema) == schema_hash(mutated)


def test_changing_check_bounds_derived_does_not_change_hash() -> None:
    schema = _build_example_schema()
    pedidos = schema.tables[1]
    check = pedidos.checks[0].model_copy(update={"bounds_derived": None})
    mutated = _with_table(schema, 1, pedidos.model_copy(update={"checks": [check]}))

    assert schema_hash(schema) == schema_hash(mutated)
