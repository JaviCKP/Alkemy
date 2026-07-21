r"""Emisores CSV y JSON, un archivo por tabla (T2.14, especificacion.md §11).

`synthdb generate` usa estos sinks para escribir el `Dataset` sin tocar ninguna
base de datos. Ambos comparten tres invariantes de reproducibilidad y de
robustez multiplataforma (CLAUDE.md, criterios del Hito 2):

- **UTF-8 siempre**, también en Windows. El Hito 1 arrastró un fallo de `cp1252`
  al escribir en consolas Windows; aquí todos los archivos se abren con
  `encoding="utf-8"` explícito, nunca el del sistema.
- **Terminador de línea `\n` fijo**, independiente de la plataforma: los CSV se
  abren con `newline=""` (el módulo `csv` controla el salto de línea) y el
  `csv.writer` usa `lineterminator="\n"`. Así el mismo `Dataset` produce los
  mismos bytes en Linux y en Windows, condición del test de reproducibilidad
  (T2.16).
- **Orden de columnas del esquema**: la cabecera del CSV y las claves de cada
  objeto JSON siguen `TableSpec.columns`, no el orden de inserción del `dict`
  de la fila.

Representación de valores:

- **`NULL`**: campo vacío en CSV; `null` en JSON.
- **Arrays** (`list`): JSON compacto **dentro de la celda** en CSV
  (`["a","b"]`, `[]` si está vacío); lista JSON nativa en el archivo JSON.
- Fechas y `datetime` viajan en ISO 8601; `Decimal` como cadena para no perder
  precisión.
"""

from __future__ import annotations

import csv
import datetime
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from synthdb.ir.schema import TableSpec


def _json_default(value: Any) -> Any:
    """Serializa a JSON los tipos que `json` no cubre, sin perder información.

    `date`/`datetime` → ISO 8601; `Decimal` → cadena (precisión intacta);
    `bytes` → hexadecimal. Cualquier otro tipo inesperado cae a `str` para no
    romper la emisión en silencio.
    """
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    return str(value)


def _csv_cell(value: Any) -> str:
    """Convierte un valor de celda a su representación textual para CSV.

    `None` es el único valor que se representa como campo **vacío**; el resto se
    serializa de forma determinista e independiente del locale. Las listas
    (arrays de PostgreSQL) se serializan como JSON compacto dentro de la celda.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


class CsvSink:
    """Sink que escribe un CSV por tabla en un directorio (`emit.base.Sink`)."""

    def __init__(self, out_dir: str | Path) -> None:
        """Prepara el sink sobre `out_dir` (se crea si no existe).

        Args:
            out_dir: Directorio destino de los `<tabla>.csv`.
        """
        self.out_dir = Path(out_dir)
        self.paths: list[Path] = []

    def write_table(self, table: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
        """Escribe `<tabla>.csv` con cabecera y una fila por registro."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        columns = [column.name for column in table.columns]
        path = self.out_dir / f"{table.name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_csv_cell(row.get(column)) for column in columns])
        self.paths.append(path)

    def finalize(self) -> None:
        """No-op: cada CSV se cierra al escribirse."""


class JsonSink:
    """Sink que escribe un JSON por tabla (lista de objetos) en un directorio."""

    def __init__(self, out_dir: str | Path) -> None:
        """Prepara el sink sobre `out_dir` (se crea si no existe).

        Args:
            out_dir: Directorio destino de los `<tabla>.json`.
        """
        self.out_dir = Path(out_dir)
        self.paths: list[Path] = []

    def write_table(self, table: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
        """Escribe `<tabla>.json` con la lista de objetos de la tabla."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        columns = [column.name for column in table.columns]
        objects = [{column: row.get(column) for column in columns} for row in rows]
        path = self.out_dir / f"{table.name}.json"
        text = json.dumps(objects, ensure_ascii=False, indent=2, default=_json_default)
        path.write_text(text + "\n", encoding="utf-8")
        self.paths.append(path)

    def finalize(self) -> None:
        """No-op: cada JSON se cierra al escribirse."""
