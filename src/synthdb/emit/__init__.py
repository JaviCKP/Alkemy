"""Emisores de datos generados (T2.14, especificacion.md §11).

Tres destinos en el MVP, todos sobre el `Dataset` en memoria del motor:

- **CSV** y **JSON** por tabla (`csv_json`), vía el protocolo `Sink` (`base`).
- **`seed.sql`** de PostgreSQL (`sql_file`), dirigido por las fases del plan.

El emisor de base de datos (`database`) llega en el Hito 4; su hueco ya existe.
"""

from __future__ import annotations

from pathlib import Path

from synthdb.emit.base import Sink, write_dataset
from synthdb.emit.csv_json import CsvSink, JsonSink
from synthdb.emit.sql_file import render_sql
from synthdb.generation.engine import Dataset
from synthdb.ir.schema import SchemaSpec

__all__ = [
    "CsvSink",
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
        ValueError: Si `fmt` no es `"csv"` ni `"json"`.
    """
    sink: CsvSink | JsonSink
    if fmt == "csv":
        sink = CsvSink(out_dir)
    elif fmt == "json":
        sink = JsonSink(out_dir)
    else:
        raise ValueError(f"formato de salida no soportado: {fmt!r} (usa 'csv' o 'json').")
    write_dataset(spec, dataset, sink)
    return sink.paths
