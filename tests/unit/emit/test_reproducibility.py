"""Reproducibilidad byte a byte de la generación a CSV/JSON/SQL (T2.16).

Es el criterio de cierre del Hito 2: «misma semilla ⇒ mismos bytes»
(especificacion.md §13, §17). Dos garantías:

1. **Hashes golden (CSV).** Generar `inmobiliaria` con una configuración y
   semilla fijas produce CSVs cuyo SHA-256 coincide con los valores fijados
   aquí. Estos hashes tienen el **mismo régimen que los snapshots** (`syrupy`):
   no se regeneran sin una justificación explícita en el commit y el PR.
   Dependen de la salida de Faker con locale `es_ES`; una actualización de
   Faker que cambie esos textos exigiría regenerarlos, documentándolo.
2. **Idempotencia intra-proceso, en los TRES formatos** (CSV, JSON y SQL; T2.16
   original solo cubría CSV). Dos generaciones seguidas en el mismo proceso
   producen archivos/texto idénticos, byte a byte -incluido el terminador de
   línea, que `JsonSink`/`export` escriben como bytes UTF-8 directos
   precisamente para que esto se cumpla también en Windows (revisión PR #42,
   hallazgo 6: `Path.write_text` sin `newline=""` traduciría `\\n` a `\\r\\n`
   ahí, y solo en esa plataforma).

La configuración se fija en Python (no en un YAML editable) para que el golden
dependa solo de este archivo. No usa reglas del mini-DSL a propósito: el rango
temporal por defecto del generador (`2015–2025`) no admite las cotas de fecha
que algunas reglas derivarían, y aquí el objetivo es el determinismo del emisor,
no el DSL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from synthdb.config.models import ColumnConfig, Config, FkQuota, FkZipf, TableConfig
from synthdb.emit import generate_files, render_sql
from synthdb.generation.engine import generate_dataset
from synthdb.ir.schema import SchemaSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"

# Hashes golden: NO regenerar sin justificación en el commit/PR (ver docstring).
_GOLDEN_CSV_SHA256: dict[str, str] = {
    "clientes.csv": "8105e60eed2e434535102d8a234fb84aa3ba9f8478355232157a1fa7dc9dc9d3",
    "compraventas.csv": "1bd6e4ace6f598ff97bb5fd5894c5e98aaf6dffdc29560107716dbc7b988b490",
    "pagos.csv": "68df919f74aeb4f86f2d86af644b918f077e526c2572f16bef7911073a5b954d",
    "viviendas.csv": "65801475724b3510b38e19586803b3ee48ef21d86c0eb083559730af3331c0c1",
}


def _schema() -> SchemaSpec:
    return parse_ddl((_SCHEMAS / "inmobiliaria.sql").read_text("utf-8"))


def _config() -> Config:
    """Configuración fija y autocontenida de la que dependen los hashes golden."""
    return Config(
        seed=20240521,
        locale="es_ES",
        tables={
            "clientes": TableConfig(rows=50),
            "viviendas": TableConfig(
                rows=60,
                columns={
                    "superficie_m2": ColumnConfig(
                        generator="numeric_range", params={"min": 35, "max": 450}
                    ),
                    "direccion": ColumnConfig(
                        generator="faker", params={"provider": "street_address"}
                    ),
                },
            ),
            "compraventas": TableConfig(
                rows=40,
                fk={
                    "vivienda_id": FkQuota(strategy="quota", min=0, max=2),
                    "comprador_id": FkZipf(strategy="zipf", s=1.3),
                },
            ),
            "pagos": TableConfig(
                rows=80, fk={"compraventa_id": FkQuota(strategy="quota", min=1, max=12)}
            ),
        },
        refs={"precio_m2_base": 2350},
    )


def _generate_csv_hashes(out_dir: Path) -> dict[str, str]:
    spec = _schema()
    paths = generate_files(spec, generate_dataset(spec, _config()), out_dir, "csv")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_inmobiliaria_csv_matches_golden_hashes(tmp_path: Path) -> None:
    assert _generate_csv_hashes(tmp_path) == _GOLDEN_CSV_SHA256


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_two_generations_in_one_process_are_byte_identical(tmp_path: Path, fmt: str) -> None:
    spec = _schema()
    first = generate_files(spec, generate_dataset(spec, _config()), tmp_path / "a", fmt)
    second = generate_files(spec, generate_dataset(spec, _config()), tmp_path / "b", fmt)
    assert [path.name for path in first] == [path.name for path in second]
    for path_a, path_b in zip(first, second, strict=True):
        assert path_a.read_bytes() == path_b.read_bytes()


def test_two_sql_exports_in_one_process_are_byte_identical() -> None:
    # export no pasa por generate_files/Sink: render_sql produce el texto y
    # la CLI lo escribe como bytes UTF-8 directos (hallazgo 6); aquí se
    # comprueba la parte que sí es una función pura y reutilizable, render_sql.
    spec = _schema()
    config = _config()
    first = render_sql(spec, generate_dataset(spec, config), config).encode("utf-8")
    second = render_sql(spec, generate_dataset(spec, config), config).encode("utf-8")
    assert first == second
    assert b"\r\n" not in first
    assert first.endswith(b"\n")
