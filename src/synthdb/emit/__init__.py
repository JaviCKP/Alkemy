"""Emisores de datos generados (T2.14, especificacion.md §11).

Tres destinos en el MVP, todos sobre el `Dataset` en memoria del motor:

- **CSV** y **JSON** por tabla (`csv_json`), vía el protocolo `Sink` (`base`).
- **`seed.sql`** de PostgreSQL (`sql_file`), dirigido por las fases del plan.

El emisor de base de datos (`database`) llega en el Hito 4; su hueco ya existe.
"""

from __future__ import annotations

from pathlib import Path

from synthdb.emit.base import Sink, write_dataset
from synthdb.emit.csv_json import CsvSink, JsonSink, validate_table_filenames
from synthdb.emit.sql_file import ExportIntegrityError, render_sql
from synthdb.generation.engine import Dataset
from synthdb.ir.schema import SchemaSpec

__all__ = [
    "CsvSink",
    "ExportIntegrityError",
    "JsonSink",
    "Sink",
    "generate_files",
    "render_sql",
    "write_dataset",
]


def generate_files(spec: SchemaSpec, dataset: Dataset, out_dir: str | Path, fmt: str) -> list[Path]:
    """Escribe el `Dataset` como CSV o JSON en `out_dir` y devuelve las rutas.

    Args:
        spec: La IR del esquema (orden de tablas y columnas).
        dataset: Resultado del motor de generación.
        out_dir: Directorio destino, creado si no existe.
        fmt: `"csv"` o `"json"`.

    Returns:
        Las rutas de los archivos escritos, en el orden de las tablas.

    Raises:
        ValueError: Si `fmt` no es `"csv"` ni `"json"`, o si dos tablas
            producirían el mismo archivo de salida (validado ANTES de escribir
            ninguna, para no dejar una salida parcial; hallazgo 2 de la
            revisión del PR #42).
    """
    if fmt not in {"csv", "json"}:
        raise ValueError(f"formato de salida no soportado: {fmt!r} (usa 'csv' o 'json').")
    out_path = Path(out_dir)
    validate_table_filenames(spec.tables, out_path, fmt)
    sink: CsvSink | JsonSink = CsvSink(out_path) if fmt == "csv" else JsonSink(out_path)
    write_dataset(spec, dataset, sink)
    return sink.paths
