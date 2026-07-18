"""Tests del `KeyStore` (T2.7, especificacion.md §7.4).

Lo que importa comprobar: que una PK compuesta se devuelve como la tupla entera
(nunca componentes mezclados), que el almacén es append-only y estable por
índice, y un smoke de rendimiento a escala (10⁶ claves) marcado como lento.
"""

from __future__ import annotations

import random
import time

import pytest

from synthdb.generation.keystore import KeyStore


def test_pk_simple_se_guarda_como_tupla_de_uno() -> None:
    store = KeyStore()
    store.add("clientes", [1, 2, 3])
    assert store.count("clientes") == 3
    assert store.get("clientes", 0) == (1,)
    assert store.get("clientes", 2) == (3,)


def test_pk_compuesta_devuelve_la_tupla_entera() -> None:
    """`get` de una PK compuesta devuelve la tupla completa, jamás un componente."""
    store = KeyStore()
    store.add("pagos", [(10, 1), (10, 2), (11, 1)])
    assert store.get("pagos", 0) == (10, 1)
    assert store.get("pagos", 1) == (10, 2)
    assert store.get("pagos", 2) == (11, 1)
    # nunca un componente suelto: siempre una tupla de 2
    assert all(len(store.get("pagos", i)) == 2 for i in range(3))


def test_add_es_append_only_y_estable_por_indice() -> None:
    store = KeyStore()
    store.add("t", [1, 2])
    store.add("t", [3])
    assert store.count("t") == 3
    # los índices previos no se mueven al añadir más
    assert store.get("t", 0) == (1,)
    assert store.get("t", 1) == (2,)
    assert store.get("t", 2) == (3,)


def test_add_acepta_un_generador() -> None:
    store = KeyStore()
    store.add("t", ((i,) for i in range(5)))
    assert store.count("t") == 5
    assert store.get("t", 4) == (4,)


def test_count_de_tabla_desconocida_es_cero() -> None:
    assert KeyStore().count("no_existe") == 0


def test_get_de_tabla_desconocida_es_keyerror_accionable() -> None:
    store = KeyStore()
    with pytest.raises(KeyError) as exc:
        store.get("fantasma", 0)
    assert "fantasma" in str(exc.value)


def test_get_fuera_de_rango_es_indexerror() -> None:
    store = KeyStore()
    store.add("t", [1])
    with pytest.raises(IndexError):
        store.get("t", 5)


def test_tablas_independientes() -> None:
    store = KeyStore()
    store.add("a", [1, 2])
    store.add("b", [(9, 9)])
    assert store.count("a") == 2
    assert store.count("b") == 1
    assert store.get("b", 0) == (9, 9)


@pytest.mark.slow
def test_smoke_rendimiento_millon_de_claves() -> None:
    """10⁶ claves añadidas + 10⁵ accesos aleatorios en tiempo razonable (~2 s objetivo).

    El interés es detectar una degradación gruesa (p. ej. un acceso que dejara de
    ser O(1)): la cota de 5 s es holgada a propósito para no volverse inestable en
    una CI cargada, mientras que una regresión a O(n) por acceso tardaría minutos.
    """
    n = 1_000_000
    accesses = 100_000
    store = KeyStore()

    start = time.perf_counter()
    store.add("big", ((i,) for i in range(n)))
    assert store.count("big") == n

    rng = random.Random(0)
    checksum = 0
    for _ in range(accesses):
        idx = rng.randrange(n)
        key = store.get("big", idx)
        checksum += key[0]
    elapsed = time.perf_counter() - start

    assert checksum >= 0  # los accesos devolvieron tuplas usables
    assert elapsed < 5.0, (
        f"KeyStore demasiado lento: {elapsed:.2f}s para {n} adds + {accesses} gets"
    )
