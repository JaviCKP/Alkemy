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

import base64
import csv
import datetime
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from synthdb.ir.schema import TableSpec


class EmitPathError(ValueError):
    """No se puede derivar un nombre de archivo seguro y único para una tabla.

    Colisión entre dos tablas que producirían el mismo archivo (bajo
    comparación insensible a mayúsculas, como en Windows/macOS) o una ruta que
    escaparía del directorio de salida. La CLI la mapea a un código de salida
    accionable, sin traceback (revisión PR #42).
    """


_SAFE_FILENAME_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789_-")
"""Bytes que un componente de nombre de archivo conserva literalmente: solo
minúsculas ASCII, dígitos, `_` y `-`. El marcador `~` queda fuera de este
conjunto; cualquier componente no conservable se representa como `~` seguido
de los bytes UTF-8 completos codificados en base32 minúscula sin padding.
La base32 es inyectiva, no introduce separadores ni `..`, y mantiene la salida
estable bajo comparación insensible a mayúsculas. PostgreSQL limita cada
identificador a 63 bytes: incluso el peor componente ocupa como máximo 102
caracteres (`~` + 101 de base32), por lo que schema+tabla+extensión cabe
holgadamente en un nombre de archivo de 255 bytes."""

_FILENAME_ENCODED_MARKER = "~"

_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
"""Nombres de dispositivo reservados de Windows: prohibidos como base de un
nombre de archivo con cualquier extensión, y (para nombres con varios puntos)
como el primer componente antes del primer punto."""


def _is_literal_filename_component(name: str) -> bool:
    """Devuelve si `name` puede conservarse sin perder seguridad ni inyectividad."""
    try:
        raw = name.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(raw) and all(byte in _SAFE_FILENAME_BYTES for byte in raw)


def _encode_name_component(name: str, *, force: bool = False) -> str:
    """Codifica un componente completo como nombre seguro, acotado e inyectivo.

    Los nombres ASCII minúsculos seguros se conservan para mantener los
    nombres normales (`foo` → `foo`). El resto, o los reservados de Windows,
    se codifica como `~` más base32 minúscula sin padding de sus bytes UTF-8
    completos. El marcador no aparece en los nombres conservados, así que una
    forma codificada no puede colisionar con una forma literal al plegar
    mayúsculas.
    """
    if not force and _is_literal_filename_component(name):
        return name
    encoded = base64.b32encode(name.encode("utf-8")).decode("ascii").rstrip("=")
    return _FILENAME_ENCODED_MARKER + encoded.lower()


def _table_identity(table: TableSpec) -> str:
    """Nombre legible de la tabla para mensajes de error (`esquema.tabla` o `tabla`)."""
    return f"{table.schema_}.{table.name}" if table.schema_ else table.name


def _safe_table_filename(table: TableSpec, ext: str) -> str:
    """Nombre de archivo determinista, inyectivo (case-insensitive) y seguro.

    Una tabla sin `schema_` y con un nombre ya en minúsculas seguro produce
    exactamente `<tabla>.<ext>` (caso normal preservado). Cualquier otro
    carácter -incluidas mayúsculas, separadores de ruta, `..`, control,
    espacios y Unicode- se codifica como `~` más base32 minúscula sin padding
    de sus bytes UTF-8 completos. Una tabla cualificada antepone el esquema
    codificado y un `.` separador; como la codificación no contiene puntos, el
    único `.` literal del *stem* es ese separador, lo que distingue sin
    ambigüedad `esquema.tabla` de una tabla llamada `esquema.tabla`. Si el
    **primer componente** (esquema si lo hay, si no la tabla) coincide con un
    dispositivo reservado de Windows, también se codifica completo: no se
    antepone un prefijo que pueda colisionar con un nombre real como `_con`.
    """
    raw_components = [table.name]
    if table.schema_:
        raw_components = [table.schema_, table.name]
    components = [
        _encode_name_component(component, force=component.casefold() in _WINDOWS_RESERVED_STEMS)
        for component in raw_components
    ]
    stem = ".".join(components)
    return f"{stem}.{ext}"


def _resolve_safe_path(out_dir: Path, table: TableSpec, ext: str) -> Path:
    r"""Ruta final de `table` dentro de `out_dir`, verificada por contención.

    El esquema de `_safe_table_filename` garantiza matemáticamente que el
    resultado no puede escapar de `out_dir` (nunca produce `/`, `\\` ni `..`
    crudos), pero se resuelve y se comprueba de forma explícita como defensa
    en profundidad (revisión PR #42): si algún caso no previsto lo violara,
    falla con `EmitPathError` en vez de escribir fuera del directorio pedido.
    """
    filename = _safe_table_filename(table, ext)
    candidate = out_dir / filename
    if candidate.resolve().parent != out_dir.resolve():
        raise EmitPathError(
            f"tabla {_table_identity(table)}: el archivo derivado ({filename!r}) "
            f"escaparía de {out_dir}. Esto no debería ocurrir con la codificación "
            "actual del nombre; repórtalo con el nombre exacto de la tabla."
        )
    return candidate


def validate_table_filenames(tables: Sequence[TableSpec], out_dir: Path, ext: str) -> None:
    """Calcula y valida el archivo de CADA tabla antes de escribir ninguno.

    Detecta colisiones -dos tablas que producirían el mismo archivo bajo
    comparación **insensible a mayúsculas** (como Windows/macOS)- y cualquier
    ruta que escaparía de `out_dir`, recorriendo TODAS las tablas antes de que
    `write_table` escriba la primera, para no dejar una salida parcial si la
    que falla es una tabla posterior de la lista (revisión PR #42).

    Args:
        tables: Tablas del esquema, en el orden en que se van a escribir.
        out_dir: Directorio destino (no hace falta que exista todavía).
        ext: Extensión sin punto (`"csv"` o `"json"`).

    Raises:
        EmitPathError: si dos tablas colisionan en el mismo archivo (case-
            insensitive), o si alguna ruta resuelta escaparía de `out_dir`.
    """
    seen: dict[str, str] = {}
    for table in tables:
        path = _resolve_safe_path(out_dir, table, ext)
        identity = _table_identity(table)
        key = path.name.casefold()
        if key in seen:
            raise EmitPathError(
                f"las tablas {seen[key]!r} y {identity!r} producirían el mismo "
                f"archivo de salida ({path.name!r}, comparando sin distinguir "
                "mayúsculas como en Windows/macOS); no se puede generar sin "
                "ambigüedad. Renombra una de las dos tablas o su esquema."
            )
        seen[key] = identity


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
        path = _resolve_safe_path(self.out_dir, table, "csv")
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
        path = _resolve_safe_path(self.out_dir, table, "json")
        text = json.dumps(objects, ensure_ascii=False, indent=2, default=_json_default)
        # `write_bytes`, no `write_text`: `json.dumps(indent=2)` produce `\n`
        # entre líneas, y `Path.write_text` sin `newline=""` los traduciría a
        # `\r\n` en Windows (modo texto por defecto), rompiendo la
        # reproducibilidad byte a byte entre plataformas (revisión PR #42,
        # hallazgo 6). Escribir bytes UTF-8 directos no traduce nada.
        path.write_bytes((text + "\n").encode("utf-8"))
        self.paths.append(path)

    def finalize(self) -> None:
        """No-op: cada JSON se cierra al escribirse."""
