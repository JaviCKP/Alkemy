"""Tests de los emisores CSV y JSON (T2.14).

Se construyen `TableSpec` y filas a mano para controlar exactamente los casos
que exige el criterio de aceptación: `NULL`, arrays (vacíos y no vacíos),
Unicode, orden de columnas del esquema y terminador de línea `\\n` fijo
(reproducibilidad multiplataforma, T2.16).
"""

from __future__ import annotations

import csv
import datetime
import json
from pathlib import Path

from synthdb.emit.csv_json import CsvSink, JsonSink
from synthdb.ir.schema import ColumnSpec, TableSpec, TypeSpec


def _table() -> TableSpec:
    """Tabla con una columna por cada caso: texto, array, fecha y numérico."""
    return TableSpec(
        name="t",
        columns=[
            ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False),
            ColumnSpec(name="nombre", type=TypeSpec(kind="text"), nullable=True),
            ColumnSpec(name="tags", type=TypeSpec(kind="text", is_array=True), nullable=True),
            ColumnSpec(name="alta", type=TypeSpec(kind="date"), nullable=True),
        ],
        primary_key=["id"],
    )


_ROWS = [
    {"id": 1, "nombre": "café ñ", "tags": ["a", "b'c"], "alta": datetime.date(2020, 1, 2)},
    {"id": 2, "nombre": None, "tags": [], "alta": None},
]


def test_csv_preserves_schema_column_order(tmp_path: Path) -> None:
    CsvSink(tmp_path).write_table(_table(), _ROWS)
    header = (tmp_path / "t.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "id,nombre,tags,alta"


def _read_csv_cells(path: Path) -> list[list[str]]:
    """Reparsea el CSV a celdas lógicas (deshace el entrecomillado del módulo)."""
    return list(csv.reader(path.read_text(encoding="utf-8").splitlines()))


def test_csv_null_is_an_empty_field(tmp_path: Path) -> None:
    CsvSink(tmp_path).write_table(_table(), _ROWS)
    cells = _read_csv_cells(tmp_path / "t.csv")
    # Fila 2: nombre NULL y alta NULL son campos vacíos.
    assert cells[2] == ["2", "", "[]", ""]


def test_csv_serialises_arrays_as_json_in_the_cell(tmp_path: Path) -> None:
    CsvSink(tmp_path).write_table(_table(), _ROWS)
    cells = _read_csv_cells(tmp_path / "t.csv")
    # La celda de un array es su JSON compacto; vacío es `[]`.
    assert cells[1][2] == json.dumps(["a", "b'c"], ensure_ascii=False, separators=(",", ":"))
    assert cells[2][2] == "[]"


def test_csv_is_utf8_with_fixed_newline(tmp_path: Path) -> None:
    CsvSink(tmp_path).write_table(_table(), _ROWS)
    raw = (tmp_path / "t.csv").read_bytes()
    assert "café ñ".encode() in raw  # UTF-8, no cp1252
    assert b"\r\n" not in raw  # terminador \n fijo, también en Windows
    assert raw.endswith(b"\n")


def test_json_null_arrays_and_order(tmp_path: Path) -> None:
    JsonSink(tmp_path).write_table(_table(), _ROWS)
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert [list(obj) for obj in data] == [["id", "nombre", "tags", "alta"]] * 2
    assert data[0]["tags"] == ["a", "b'c"]  # array como lista nativa
    assert data[0]["alta"] == "2020-01-02"  # fecha en ISO 8601
    assert data[1]["nombre"] is None  # NULL como null
    assert data[1]["tags"] == []  # array vacío como lista vacía


def test_json_is_utf8(tmp_path: Path) -> None:
    JsonSink(tmp_path).write_table(_table(), _ROWS)
    raw = (tmp_path / "t.json").read_bytes()
    assert "café ñ".encode() in raw


def test_sink_paths_are_recorded(tmp_path: Path) -> None:
    sink = CsvSink(tmp_path)
    sink.write_table(_table(), _ROWS)
    sink.finalize()
    assert sink.paths == [tmp_path / "t.csv"]
