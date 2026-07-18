"""Semillas jerárquicas y RNG por fila (especificacion.md §13, T2.1).

Determinismo total (CLAUDE.md): toda la aleatoriedad del motor de generación
cuelga de estas dos funciones. No se usa `random` global, ni `datetime.now()`,
ni ninguna fuente de entropía del sistema; misma semilla global + mismo plan
⇒ mismos bytes.

La derivación es jerárquica en dos niveles:

1. `seed_for_table(seed_global, tabla)` mezcla la semilla global del usuario con
   el nombre de la tabla vía BLAKE2b, dando una semilla de tabla estable.
2. `rng_for_row(seed_tabla, indice)` deriva un `random.Random` **independiente
   por cada fila** a partir de su índice, no de un flujo secuencial por tabla.

La consecuencia clave del punto 2 es que el valor de la fila *i* depende solo de
`(seed_tabla, i)`: no del tamaño de lote con el que se genere, ni del orden en
que se generen las filas, ni de una futura paralelización. Un RNG secuencial por
tabla (avanzar un único `Random` fila tras fila) NO tendría esta propiedad:
cambiar el tamaño de lote cambiaría qué valores caen en qué fila.
"""

from __future__ import annotations

import hashlib
import random
import struct

_TABLE_SEED_BYTES = 8
"""Ancho del digest de la semilla de tabla: 64 bits, de sobra para el dominio."""

_ROW_SEED_BYTES = 32
"""Ancho del digest que siembra el `Random` de fila: 256 bits de entropía."""

_TABLE_PERSON = b"synthdb:tbl"
"""Personalización BLAKE2b del dominio «semilla de tabla»."""

_ROW_PERSON = b"synthdb:row"
"""Personalización BLAKE2b del dominio «semilla de fila»; separa este dominio del
de tabla aunque los bytes de entrada coincidieran."""


def _int_to_bytes(n: int) -> bytes:
    """Serializa un entero de signo y magnitud arbitrarios de forma canónica.

    Un entero de Python no tiene ancho fijo, así que se codifica el signo en un
    byte y la magnitud en el mínimo número de bytes big-endian. La codificación
    es inyectiva (enteros distintos ⇒ secuencias distintas), condición para que
    el framing con prefijo de longitud sea libre de colisiones.
    """
    sign = b"\x01" if n < 0 else b"\x00"
    magnitude = abs(n)
    length = max(1, (magnitude.bit_length() + 7) // 8)
    return sign + magnitude.to_bytes(length, "big")


def _frame(*parts: bytes) -> bytes:
    """Concatena varias partes con prefijo de longitud, sin ambigüedad.

    Prefijar cada parte con su longitud (8 bytes big-endian) garantiza que
    `_frame(a, b)` solo puede descomponerse de una manera: dos pares de entradas
    distintos nunca producen el mismo mensaje, así que el hash no colisiona por
    un corrimiento entre la semilla y el nombre de la tabla.
    """
    out = bytearray()
    for part in parts:
        out += struct.pack(">Q", len(part))
        out += part
    return bytes(out)


def seed_for_table(seed_global: int, table: str) -> int:
    """Deriva la semilla de una tabla a partir de la semilla global.

    `seed_tabla = blake2b(seed_global, nombre_tabla)` (especificacion.md §13).
    Estable entre ejecuciones y plataformas: dos tablas distintas del mismo
    esquema obtienen semillas independientes, y la misma tabla siempre la misma.

    Args:
        seed_global: Semilla del usuario (`config.yaml: seed`), un entero
            cualquiera (positivo, cero o negativo; sin cota de tamaño).
        table: Nombre canónico de la tabla.

    Returns:
        Semilla de tabla como entero sin signo de 64 bits.
    """
    message = _frame(_int_to_bytes(seed_global), table.encode("utf-8"))
    digest = hashlib.blake2b(message, digest_size=_TABLE_SEED_BYTES, person=_TABLE_PERSON).digest()
    return int.from_bytes(digest, "big")


def rng_for_row(table_seed: int, row_index: int) -> random.Random:
    """Construye el `random.Random` determinista de una fila concreta.

    La semilla del `Random` se deriva de `(table_seed, row_index)` con BLAKE2b,
    de modo que cada fila tiene su propio flujo aleatorio reproducible e
    **independiente del resto**. Generar la fila *i* no consume aleatoriedad de
    la fila *i-1*: por eso el resultado no depende del tamaño de lote ni del
    orden de generación (ver el docstring del módulo).

    Args:
        table_seed: Semilla de la tabla, típicamente de `seed_for_table`.
        row_index: Índice 0-based de la fila dentro de la tabla.

    Returns:
        Un `random.Random` recién sembrado, exclusivo de esa fila.
    """
    message = _frame(_int_to_bytes(table_seed), _int_to_bytes(row_index))
    digest = hashlib.blake2b(message, digest_size=_ROW_SEED_BYTES, person=_ROW_PERSON).digest()
    return random.Random(int.from_bytes(digest, "big"))
