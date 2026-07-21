"""Contrato de emisión de datos: el protocolo `Sink` (T2.14, especificacion.md §11).

Un *sink* recibe las filas ya generadas de una tabla y las materializa en algún
destino físico (archivos CSV/JSON en esta sesión; una base de datos real en el
Hito 4). El motor de generación (`generation.engine`) no sabe nada de la
representación de salida: entrega un `Dataset` en memoria y un emisor decide
cómo escribirlo. Esa frontera es la que permite añadir el emisor de BD del H4
sin tocar el motor.

El protocolo es deliberadamente **mínimo** —`write_table` + `finalize`— y
orientado a *tabla*, que es la unidad natural del emisor de base de datos:

- **`write_table(table, rows)`** recibe la `TableSpec` (para el orden y el tipo
  de las columnas, nunca releyendo el esquema) y las filas válidas de esa tabla.
  Los sinks de archivo escriben un fichero por llamada; el sink de BD del H4
  (`emit/database.py`) hará el INSERT por lotes dentro de una transacción y leerá
  de vuelta las claves autoincrementales reales (RETURNING) para poblar el
  `KeyStore` — de ahí que la unidad sea la tabla y no el `Dataset` entero.
- **`finalize()`** cierra el trabajo pendiente (un `commit`, un índice, un
  resumen). Para los sinks de archivo es un no-op; se declara para que el emisor
  de BD tenga dónde confirmar la transacción final.

`write_dataset` recorre las tablas **en el orden del esquema** y delega en el
sink. Ese orden basta para CSV/JSON (un archivo por tabla, independientes). El
emisor SQL (`emit/sql_file.render_sql`) **no** implementa `Sink` a propósito:
necesita el orden de *fases* del plan y las `UpdatePhase` (romper ciclos con
`INSERT ... NULL` seguido de `UPDATE`), algo que un contrato por-tabla no
expresa; por eso es una función sobre el `Dataset` completo. El sink de BD del
H4, que sí necesita las fases, extenderá este contrato o las orquestará en su
`finalize`, documentado allí cuando llegue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from synthdb.ir.schema import SchemaSpec, TableSpec

if TYPE_CHECKING:
    from synthdb.generation.engine import Dataset


@runtime_checkable
class Sink(Protocol):
    """Destino físico de las filas generadas, una tabla cada vez.

    Implementaciones actuales: `csv_json.CsvSink` y `csv_json.JsonSink`. El
    emisor de base de datos del Hito 4 (`emit/database.py`) implementará este
    mismo protocolo.
    """

    def write_table(self, table: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
        """Materializa las filas de una tabla.

        Args:
            table: La `TableSpec` de la IR, fuente única del orden y el tipo de
                las columnas (CLAUDE.md: nada aguas abajo relee el esquema).
            rows: Filas válidas de la tabla, cada una un mapa
                `columna -> valor` con los tipos Python del motor (`int`,
                `str`, `datetime.date`, `list`, `None`...).
        """
        ...

    def finalize(self) -> None:
        """Cierra el trabajo pendiente del sink (commit, resumen, cierre).

        No-op en los sinks de archivo; punto de confirmación de la transacción
        para el emisor de base de datos del Hito 4.
        """
        ...


def write_dataset(spec: SchemaSpec, dataset: Dataset, sink: Sink) -> None:
    """Vuelca un `Dataset` en un `Sink`, tabla a tabla, en el orden del esquema.

    Args:
        spec: La IR del esquema; fija el orden de las tablas y, por tabla, el
            orden de las columnas.
        dataset: Resultado en memoria del motor de generación. Solo se emiten
            las filas válidas (`dataset.tables`); la cuarentena no se escribe.
        sink: El destino que materializa cada tabla.
    """
    for table in spec.tables:
        sink.write_table(table, dataset.tables.get(table.name, []))
    sink.finalize()
