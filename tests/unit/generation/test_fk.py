"""Tests de los selectores de FK (T2.8, especificacion.md §7.4).

Los tests estadísticos son deliberadamente **gruesos** (semilla fija, tolerancias
amplias): comprueban la forma de la distribución —zipf sesgada, uniform plana,
quota dentro de cotas— sin fijar valores exactos que ataran el test a la
implementación del RNG. Lo que sí se exige con exactitud: las cotas de quota por
padre, el agotamiento de unique_subset y el determinismo bit a bit.
"""

from __future__ import annotations

from collections import Counter
from random import Random

import pytest

from synthdb.config.models import FkQuota, FkUniform, FkUniqueSubset, FkZipf
from synthdb.generation.fk import (
    NullRatioSelector,
    QuotaInfeasibleError,
    UniformSelector,
    UniqueSubsetExhaustedError,
    UniqueSubsetSelector,
    ZipfSelector,
    build_quota_assignment,
)


def _picks(selector: object, n: int, *, seed: int = 7) -> list[int | None]:
    """Llama `pick` `n` veces con un RNG sembrado y recoge los índices."""
    rng = Random(seed)
    return [selector.pick(rng) for _ in range(n)]  # type: ignore[attr-defined]


class _CountingSelector:
    """Selector de prueba que cuenta cuántas veces se le pide un índice."""

    def __init__(self) -> None:
        self.calls = 0

    def pick(self, rng: Random) -> int | None:
        self.calls += 1
        return 0


# --- uniform -----------------------------------------------------------------


def test_uniform_reparte_plano() -> None:
    counts = Counter(_picks(UniformSelector(n_parents=10), 20_000))
    # cada padre ~2000; banda ancha (±25 %) para no depender del RNG concreto
    for parent in range(10):
        assert 1_500 <= counts[parent] <= 2_500, f"padre {parent}: {counts[parent]}"


def test_uniform_sin_padres_es_error_accionable() -> None:
    with pytest.raises(ValueError, match="n_parents=0"):
        UniformSelector(n_parents=0)


# --- zipf --------------------------------------------------------------------


def test_zipf_concentra_en_los_primeros_padres() -> None:
    """El padre 0 (más popular por índice de inserción) recibe muchos más que el último."""
    counts = Counter(_picks(ZipfSelector(n_parents=10, s=1.3), 20_000))
    assert counts[0] == max(counts.values()), "el padre 0 debería ser el más popular"
    assert counts[0] > 2 * counts[9], f"padre 0={counts[0]} vs padre 9={counts[9]}"


def test_zipf_popularidad_por_indice_no_por_valor() -> None:
    """El sesgo va por posición de inserción: 0 > 1 > ... de forma aproximada y monótona-ish."""
    counts = Counter(_picks(ZipfSelector(n_parents=5, s=1.5), 20_000))
    # margen claro entre el primero y el último; los intermedios pueden barajarse un poco
    assert counts[0] > counts[4]
    assert counts[1] > counts[4]


def test_zipf_indices_siempre_en_rango() -> None:
    picks = _picks(ZipfSelector(n_parents=4, s=0.8), 5_000)
    assert all(p is not None and 0 <= p < 4 for p in picks)


# --- unique_subset -----------------------------------------------------------


def test_unique_subset_sin_repetidos_cuando_hay_de_sobra() -> None:
    selector = UniqueSubsetSelector(n_parents=100, n_rows=40, table="perfiles")
    picks = _picks(selector, 40)
    assert len(set(picks)) == 40  # sin repetición
    assert all(p is not None and 0 <= p < 100 for p in picks)


def test_unique_subset_agotamiento_es_error_accionable() -> None:
    selector = UniqueSubsetSelector(n_parents=3, n_rows=5, table="perfiles")
    rng = Random(1)
    seen = [selector.pick(rng) for _ in range(3)]
    assert len(set(seen)) == 3  # los tres primeros salen sin problema
    with pytest.raises(UniqueSubsetExhaustedError) as exc:
        selector.pick(rng)  # el cuarto ya no tiene padre libre
    message = str(exc.value)
    assert "perfiles" in message  # la tabla
    assert "3" in message and "5" in message  # padres disponibles y filas pedidas


# --- quota -------------------------------------------------------------------


def test_quota_respeta_las_cotas_por_padre_exactamente() -> None:
    assignment = build_quota_assignment(Random(0), n_parents=10, n_rows=60, min=3, max=9)
    assert len(assignment) == 60
    counts = Counter(assignment)
    assert set(counts) == set(range(10)), "todos los padres reciben algún hijo (min=3>0)"
    for parent, count in counts.items():
        assert 3 <= count <= 9, f"padre {parent} fuera de [3, 9]: {count}"


def test_quota_min_cero_permite_padres_sin_hijos() -> None:
    assignment = build_quota_assignment(Random(0), n_parents=8, n_rows=5, min=0, max=3)
    assert len(assignment) == 5
    counts = Counter(assignment)
    assert all(0 <= c <= 3 for c in counts.values())


def test_quota_infeasible_por_abajo() -> None:
    with pytest.raises(QuotaInfeasibleError) as exc:
        build_quota_assignment(Random(0), n_parents=3, n_rows=2, min=1, max=5)
    message = str(exc.value)
    assert "2" in message and "3" in message  # n_rows y n_parents
    assert "[3, 15]" in message  # rango factible n_parents*min .. n_parents*max


def test_quota_infeasible_por_arriba() -> None:
    with pytest.raises(QuotaInfeasibleError) as exc:
        build_quota_assignment(Random(0), n_parents=3, n_rows=20, min=0, max=5)
    message = str(exc.value)
    assert "20" in message
    assert "[0, 15]" in message  # rango factible


def test_quota_es_determinista() -> None:
    a = build_quota_assignment(Random(42), n_parents=6, n_rows=30, min=2, max=8)
    b = build_quota_assignment(Random(42), n_parents=6, n_rows=30, min=2, max=8)
    assert a == b


def test_quota_baraja_no_agrupa_por_padre() -> None:
    """El reparto se baraja: la lista no queda ordenada por padre (0,0,...,1,1,...)."""
    assignment = build_quota_assignment(Random(3), n_parents=5, n_rows=25, min=5, max=5)
    ordenada = sorted(assignment)
    assert assignment != ordenada  # con semilla fija, el barajado sí desordena


# --- null_ratio --------------------------------------------------------------


def test_null_ratio_proporcion_aproximada() -> None:
    selector = NullRatioSelector(UniformSelector(n_parents=20), null_ratio=0.3)
    picks = _picks(selector, 10_000)
    nulls = sum(1 for p in picks if p is None)
    assert 0.25 <= nulls / len(picks) <= 0.35  # ~30 %, banda ancha


def test_null_ratio_cero_nunca_es_nulo() -> None:
    picks = _picks(NullRatioSelector(UniformSelector(n_parents=5), null_ratio=0.0), 1_000)
    assert all(p is not None for p in picks)


def test_null_ratio_uno_siempre_nulo_y_no_toca_el_interior() -> None:
    inner = _CountingSelector()
    picks = _picks(NullRatioSelector(inner, null_ratio=1.0), 500)
    assert all(p is None for p in picks)
    assert inner.calls == 0  # el selector interior nunca se llama si todo es NULL


def test_null_ratio_consume_el_interior_solo_en_filas_no_nulas() -> None:
    """El RNG de selección interior se consume exactamente en las filas no nulas."""
    inner = _CountingSelector()
    picks = _picks(NullRatioSelector(inner, null_ratio=0.4), 5_000)
    non_null = sum(1 for p in picks if p is not None)
    assert inner.calls == non_null


def test_null_ratio_fuera_de_rango_es_error() -> None:
    with pytest.raises(ValueError, match=r"null_ratio"):
        NullRatioSelector(UniformSelector(n_parents=3), null_ratio=1.5)


# --- determinismo global (las cinco estrategias) -----------------------------


def test_determinismo_misma_semilla_misma_secuencia() -> None:
    def run(seed: int) -> dict[str, list[int | None] | list[int]]:
        return {
            "uniform": _picks(UniformSelector(10), 50, seed=seed),
            "zipf": _picks(ZipfSelector(10, 1.2), 50, seed=seed),
            "unique_subset": _picks(UniqueSubsetSelector(60, 50, "t"), 50, seed=seed),
            "null_ratio": _picks(NullRatioSelector(UniformSelector(10), 0.3), 50, seed=seed),
            "quota": build_quota_assignment(Random(seed), 10, 50, 2, 8),
        }

    assert run(123) == run(123)  # misma semilla ⇒ idéntico en las cinco


def test_determinismo_distinta_semilla_distinta_secuencia() -> None:
    a = _picks(UniformSelector(20), 50, seed=1)
    b = _picks(UniformSelector(20), 50, seed=2)
    assert a != b
    qa = build_quota_assignment(Random(1), 10, 50, 2, 8)
    qb = build_quota_assignment(Random(2), 10, 50, 2, 8)
    assert qa != qb


# --- contrato: los parámetros coinciden con los modelos de config ------------


def test_parametros_casan_con_los_modelos_de_config() -> None:
    """Los campos de las estrategias del YAML alimentan los selectores sin traducir nombres."""
    zipf = FkZipf(strategy="zipf", s=1.3)
    ZipfSelector(n_parents=5, s=zipf.s)  # FkZipf.s -> ZipfSelector.s

    quota = FkQuota(strategy="quota", min=1, max=4)
    build_quota_assignment(Random(0), 5, 10, quota.min, quota.max)  # FkQuota.min/max

    # uniform y unique_subset no tienen parámetros propios más allá de null_ratio
    assert FkUniform(strategy="uniform").null_ratio is None
    assert FkUniqueSubset(strategy="unique_subset").null_ratio is None
