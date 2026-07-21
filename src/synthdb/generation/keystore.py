"""Almacén de claves primarias por tabla para la selección de FKs (T2.7, especificacion.md §7.4).

El `KeyStore` guarda, tabla a tabla, las claves primarias que el motor va
generando, para que las tablas hijas puedan referenciarlas por índice
(`generation/fk.py`, T2.8). Es *append-only*: el motor inserta las tablas en
orden de dependencia (padres antes que hijos, `graph/`), así que cuando una
tabla hija selecciona una FK todas las claves del padre ya están dentro y su
índice no cambia.

**Todo se guarda como tupla**, incluso una PK de una sola columna (`(valor,)`):
así la PK compuesta no es un caso especial en ningún consumidor
(`fk.py`, validación, emisores). `get` devuelve siempre la tupla entera; nunca
un componente suelto (especificacion.md §7.4: «con PK compuesta se muestrea el
índice de la tupla, nunca componentes sueltos»).

Alcance del MVP: **solo memoria**. La especificación (§7.4) prevé un *spill*
transparente a SQLite en disco por encima de ~10⁷ claves; eso es v1.0 y aquí no
se implementa. En memoria, la lista por tabla soporta holgadamente 10⁶ claves
con acceso por índice en O(1) (ver el smoke de rendimiento en los tests).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

Key = tuple[Any, ...]
"""Una clave primaria almacenada: siempre una tupla, de 1 componente o más."""


class KeyStore:
    """Colección *append-only* de claves primarias, indexada por tabla.

    Cada tabla tiene su propia lista de claves en orden de inserción. El acceso
    por índice (`get`) es O(1) y estable mientras dure la generación, que es lo
    que la selección de FKs necesita para muestrear un padre por su posición.
    """

    def __init__(self) -> None:
        """Crea un almacén vacío, sin ninguna tabla registrada."""
        self._by_table: dict[str, list[Key]] = {}

    def add(self, table: str, keys: Iterable[Any]) -> None:
        """Añade un lote de claves de `table`, preservando el orden de inserción.

        Cada elemento de `keys` es UNA clave primaria de una fila. Se normaliza a
        tupla: un escalar (PK simple) se envuelve en `(valor,)`; una tupla (PK
        compuesta, o ya envuelta) se guarda tal cual. De este modo el resto del
        motor trata siempre con tuplas y la PK compuesta no es un caso especial.

        Args:
            table: Nombre de la tabla propietaria de estas claves.
            keys: Iterable de claves; cada una un escalar o una tupla. Se consume
                una sola vez, así que puede ser un generador.
        """
        bucket = self._by_table.setdefault(table, [])
        for key in keys:
            bucket.append(key if isinstance(key, tuple) else (key,))

    def replace(self, table: str, keys: Iterable[Any]) -> None:
        """Reemplaza TODAS las claves de `table` por `keys` (normalizadas a tupla).

        Rompe deliberadamente la naturaleza *append-only* del almacén para un único
        uso: cuando la generación ya ha terminado y el motor cuarentena filas, deja
        el `KeyStore` reflejando solo las aceptadas. No se llama durante la selección
        de FKs, así que no altera la estabilidad de índices que esa fase necesita.

        Args:
            table: Tabla cuyas claves se sustituyen.
            keys: Claves aceptadas; cada una un escalar o una tupla (se envuelve un
                escalar en `(valor,)`, igual que `add`).
        """
        self._by_table[table] = [key if isinstance(key, tuple) else (key,) for key in keys]

    def count(self, table: str) -> int:
        """Devuelve cuántas claves hay almacenadas para `table` (0 si no existe)."""
        return len(self._by_table.get(table, ()))

    def get(self, table: str, index: int) -> Key:
        """Devuelve la clave completa en la posición `index` de `table`.

        El valor devuelto es la tupla entera de la PK, incluso para una PK de una
        sola columna (`(valor,)`): quien la consuma decide si usa un componente o
        toda la tupla, pero nunca se le entregan componentes mezclados de claves
        distintas.

        Args:
            table: Tabla de la que leer.
            index: Posición 0-based dentro de las claves de la tabla.

        Returns:
            La clave primaria como tupla.

        Raises:
            KeyError: Si `table` no tiene ninguna clave registrada.
            IndexError: Si `index` está fuera del rango de claves de la tabla.
        """
        try:
            keys = self._by_table[table]
        except KeyError:
            raise KeyError(
                f"KeyStore no tiene claves para la tabla '{table}': el motor debe "
                f"generarla (y registrar sus PKs con add) antes de que una tabla hija "
                f"la referencie."
            ) from None
        return keys[index]
