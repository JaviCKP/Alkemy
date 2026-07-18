"""Tests de las semillas jerárquicas y el RNG por fila (T2.1)."""

import random

from synthdb.generation.seeding import rng_for_row, seed_for_table


def _draw(rng: random.Random) -> list[float]:
    return [rng.random() for _ in range(4)]


def test_seed_for_table_is_deterministic():
    assert seed_for_table(42, "clientes") == seed_for_table(42, "clientes")


def test_seed_for_table_varies_by_table_and_seed():
    a = seed_for_table(42, "clientes")
    b = seed_for_table(42, "viviendas")
    c = seed_for_table(43, "clientes")
    assert a != b
    assert a != c
    assert b != c


def test_seed_for_table_handles_negative_and_huge_seeds():
    # No debe lanzar y debe ser estable para enteros de cualquier signo/tamaño.
    for seed in (-1, 0, 2**70, -(2**70)):
        assert seed_for_table(seed, "t") == seed_for_table(seed, "t")


def test_rng_for_row_reproducible():
    s = seed_for_table(7, "pagos")
    assert _draw(rng_for_row(s, 3)) == _draw(rng_for_row(s, 3))


def test_rng_for_row_differs_between_rows():
    s = seed_for_table(7, "pagos")
    assert _draw(rng_for_row(s, 0)) != _draw(rng_for_row(s, 1))


def test_rng_for_row_differs_between_tables():
    a = rng_for_row(seed_for_table(7, "pagos"), 0)
    b = rng_for_row(seed_for_table(7, "clientes"), 0)
    assert _draw(a) != _draw(b)


def test_batch_size_independence():
    """Criterio de aceptación T2.1: mismo valor por fila con cualquier lote.

    Se generan 100 filas en lotes de 10, de 100 y de 7 (un tamaño que no divide
    a 100); el resultado por fila debe ser idéntico porque cada fila deriva su
    RNG del índice, no de un flujo secuencial por tabla.
    """
    s = seed_for_table(42, "clientes")

    def generate_in_batches(total: int, batch: int) -> list[float]:
        values: list[float] = []
        for start in range(0, total, batch):
            for i in range(start, min(start + batch, total)):
                values.append(rng_for_row(s, i).random())
        return values

    in_10 = generate_in_batches(100, 10)
    in_100 = generate_in_batches(100, 100)
    in_7 = generate_in_batches(100, 7)
    assert in_10 == in_100 == in_7
    assert len(in_10) == 100
