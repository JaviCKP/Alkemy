"""Tests de seguridad de nombres de archivo CSV/JSON (revisión PR #42, hallazgo 2).

`out_dir / f"{table.name}.csv"` permitía que un nombre de tabla como
`"../escaped"` escribiera fuera de `--out`. Estos tests cubren la codificación
segura e inyectiva (`_safe_table_filename`), la comprobación de contención
dentro de `out_dir` (`_resolve_safe_path`) y la validación de colisiones antes
de escribir el primer archivo (`validate_table_filenames`, invocada desde
`generate_files`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthdb.emit import generate_files
from synthdb.emit.csv_json import (
    CsvSink,
    _resolve_safe_path,
    _safe_table_filename,
    validate_table_filenames,
)
from synthdb.generation.engine import Dataset
from synthdb.ir.plans import InsertPhase
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec


def _table(name: str, schema: str | None = None) -> TableSpec:
    return TableSpec(
        name=name,
        schema=schema,
        columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
        primary_key=["id"],
    )


# --- Codificación: normal, path traversal, control, Windows, colisión -------


def test_normal_table_name_is_unchanged() -> None:
    # Comportamiento previo preservado a propósito para el caso común.
    assert _safe_table_filename(_table("clientes"), "csv") == "clientes.csv"
    assert _safe_table_filename(_table("clientes"), "json") == "clientes.json"


@pytest.mark.parametrize(
    "malicious_name",
    [
        "../escaped",
        "..\\escaped",
        "../../etc/passwd",
        "a/b",
        "a" + chr(92) + "b",
        "..",
        ".",
    ],
)
def test_path_traversal_names_are_encoded_away(malicious_name: str) -> None:
    filename = _safe_table_filename(_table(malicious_name), "csv")
    assert "/" not in filename
    assert "\\" not in filename
    assert ".." not in filename.removesuffix(".csv")  # el '..' del propio nombre, codificado


def test_control_characters_and_percent_are_encoded() -> None:
    filename = _safe_table_filename(_table("a\x00b\tc%d"), "csv")
    assert "\x00" not in filename
    assert "\t" not in filename
    # El propio '%' se codifica también, o el esquema dejaría de ser inyectivo.
    assert "%25" in filename


@pytest.mark.parametrize("reserved", ["CON", "con", "PRN", "aux", "NUL", "com1", "LPT9"])
def test_windows_reserved_device_names_are_prefixed(reserved: str) -> None:
    filename = _safe_table_filename(_table(reserved), "csv")
    stem = filename.removesuffix(".csv")
    assert stem.lower() not in {"con", "prn", "aux", "nul", "com1", "lpt9"}
    assert stem == f"_{reserved}"


def test_two_distinct_encodings_never_collide_for_different_names() -> None:
    names = ["clientes", "../clientes", "CLIENTES", "cliéntes", "cli%65ntes", "cli.entes"]
    filenames = {_safe_table_filename(_table(n), "csv") for n in names}
    assert len(filenames) == len(names)


def test_schema_qualified_tables_do_not_collide_with_same_named_table() -> None:
    unqualified = _safe_table_filename(_table("foo"), "csv")
    qualified_a = _safe_table_filename(_table("foo", schema="a"), "csv")
    qualified_b = _safe_table_filename(_table("foo", schema="b"), "csv")
    assert len({unqualified, qualified_a, qualified_b}) == 3


# --- Contención dentro de out_dir -------------------------------------------


def test_resolved_path_stays_within_out_dir(tmp_path: Path) -> None:
    path = _resolve_safe_path(tmp_path, _table("../escaped"), "csv")
    assert path.resolve().parent == tmp_path.resolve()


def test_csv_sink_actually_writes_inside_out_dir_for_traversal_name(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    sink = CsvSink(out_dir)
    sink.write_table(_table("../escaped"), [{"id": 1}])
    written = sink.paths[0]
    # La ruta escrita está DENTRO de out_dir, no en tmp_path directamente ni
    # en ningún ancestro.
    assert written.resolve().parent == out_dir.resolve()
    assert written.exists()
    # Y no se creó ningún archivo fuera de out_dir con el nombre crudo.
    assert not (tmp_path / "escaped.csv").exists()
    assert not (tmp_path.parent / "escaped.csv").exists()


# --- Validación de colisiones ANTES de escribir -----------------------------


def test_validate_table_filenames_raises_on_genuine_collision(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mismo archivo de salida"):
        validate_table_filenames([_table("dup"), _table("dup")], tmp_path, "csv")


def test_validate_table_filenames_accepts_distinct_tables(tmp_path: Path) -> None:
    validate_table_filenames([_table("a"), _table("b"), _table("c")], tmp_path, "csv")  # no raise


def test_generate_files_rejects_colliding_schema_before_writing_anything(
    tmp_path: Path,
) -> None:
    # Tres tablas; la tercera colisiona con la primera. Si la validación no
    # fuera previa a TODO el bucle de escritura, "a" y "b" ya se habrían
    # escrito cuando la colisión de la tercera aborta la ejecución.
    out_dir = tmp_path / "out"
    spec = SchemaSpec(dialect="postgres", tables=[_table("a"), _table("b"), _table("a")])
    dataset = Dataset(
        tables={"a": [{"id": 1}], "b": [{"id": 2}]}, phases=[InsertPhase(tables=["a", "b"])]
    )

    with pytest.raises(ValueError, match="mismo archivo de salida"):
        generate_files(spec, dataset, out_dir, "csv")

    # Ningún archivo parcial: el directorio ni siquiera debería tener 'a.csv'.
    assert not out_dir.exists() or list(out_dir.iterdir()) == []
