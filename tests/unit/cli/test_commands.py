"""Tests de los comandos `plan`, `generate` y `export` (T2.15, cierre del Hito 2).

Se ejercitan con `typer.testing.CliRunner` sobre los fixtures reales de
`tests/schemas/` y configuraciones YAML mínimas escritas en `tmp_path`. Cubren
la salida, los códigos de salida (0/1/2/3/4/5), que `--dry-run` no escribe nada
y que la cuarentena se informa siempre.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from synthdb.cli import app
from synthdb.generation import engine

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_runner = CliRunner()


def _fixture(name: str) -> str:
    return str(_SCHEMAS / f"{name}.sql")


def _config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _invoke(*args: str) -> Result:
    return _runner.invoke(app, list(args))


# Configuración limpia de inmobiliaria: sin reglas (evita el rango temporal por
# defecto), con `superficie_m2` explícito para no depender de heurísticas.
_INMOBILIARIA_CFG = """
seed: 7
locale: es_ES
tables:
  clientes: {rows: 8}
  viviendas:
    rows: 10
    columns:
      superficie_m2: {generator: numeric_range, params: {min: 35, max: 450}}
  compraventas:
    rows: 6
    fk: {vivienda_id: {strategy: uniform}, comprador_id: {strategy: uniform}}
  pagos:
    rows: 10
    fk: {compraventa_id: {strategy: quota, min: 1, max: 5}}
"""

_CICLOS_CFG = "seed: 3\ntables: {pedidos: {rows: 5}, facturas: {rows: 5}}\n"


# --- plan --------------------------------------------------------------------


def test_plan_shows_columns_sources_and_phases(tmp_path: Path) -> None:
    result = _invoke("plan", _fixture("inmobiliaria"), "-c", _config(tmp_path, _INMOBILIARIA_CFG))
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Plan de generación" in out
    assert "clientes" in out
    assert "Fuente" in out  # cabecera de la rejilla por columna
    assert "heuristic" in out  # al menos una columna resuelta por heurística
    assert "Fases" in out


def test_plan_without_config_uses_defaults() -> None:
    result = _invoke("plan", _fixture("inmobiliaria"))
    assert result.exit_code == 0, result.output


def test_plan_no_llm_flag_is_accepted_as_noop() -> None:
    result = _invoke("plan", _fixture("inmobiliaria"), "--no-llm")
    assert result.exit_code == 0, result.output


def test_plan_json_is_valid_and_deterministic(tmp_path: Path) -> None:
    cfg = _config(tmp_path, _INMOBILIARIA_CFG)
    first = _invoke("plan", _fixture("inmobiliaria"), "-c", cfg, "--json")
    second = _invoke("plan", _fixture("inmobiliaria"), "-c", cfg, "--json")
    assert first.exit_code == 0
    assert first.output == second.output  # byte a byte
    data = json.loads(first.output)
    assert {"tables", "phases", "rules", "warnings"} <= data.keys()
    assert data["tables"]


def test_plan_missing_file_exits_three(tmp_path: Path) -> None:
    result = _invoke("plan", str(tmp_path / "no_existe.sql"))
    assert result.exit_code == 3
    assert "Traceback" not in result.output


def test_plan_unbreakable_cycle_exits_two() -> None:
    result = _invoke("plan", _fixture("ciclos_unbreakable"))
    assert result.exit_code == 2
    assert "Traceback" not in result.output


# --- generate ----------------------------------------------------------------


def test_generate_writes_one_csv_per_table(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _invoke(
        "generate",
        _fixture("inmobiliaria"),
        "-c",
        _config(tmp_path, _INMOBILIARIA_CFG),
        "-o",
        str(out),
    )
    assert result.exit_code == 0, result.output
    written = sorted(p.name for p in out.glob("*.csv"))
    assert written == ["clientes.csv", "compraventas.csv", "pagos.csv", "viviendas.csv"]


def test_generate_json_format_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(out),
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.glob("*.json")) == ["facturas.json", "pedidos.json"]


def test_generate_csv_serialises_arrays_and_nulls(tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = _config(
        tmp_path,
        "seed: 23\ntables: {inmobiliarias: {rows: 5}, clientes: {rows: 30}, matches: {rows: 30}}\n",
    )
    result = _invoke("generate", _fixture("crm_real_minimo"), "-c", cfg, "-o", str(out))
    assert result.exit_code == 0, result.output
    text = (out / "clientes.csv").read_text(encoding="utf-8")
    assert "roles" in text.splitlines()[0]  # la columna array está
    assert "[]" in text or '["' in text  # arrays serializados como JSON


def test_generate_bad_format_exits_four(tmp_path: Path) -> None:
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(tmp_path / "out"),
        "--format",
        "parquet",
    )
    assert result.exit_code == 4
    assert "Traceback" not in result.output


def test_generate_dry_run_leaves_the_directory_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()  # directorio preexistente: debe quedar completamente vacío
    result = _invoke(
        "generate",
        _fixture("inmobiliaria"),
        "-c",
        _config(tmp_path, _INMOBILIARIA_CFG),
        "-o",
        str(out),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert list(out.iterdir()) == []  # ni un archivo escrito
    assert "Muestra" in result.output  # sí imprime el plan y la muestra


def test_generate_dry_run_does_not_even_create_the_directory(tmp_path: Path) -> None:
    out = tmp_path / "nope"
    result = _invoke(
        "generate",
        _fixture("inmobiliaria"),
        "-c",
        _config(tmp_path, _INMOBILIARIA_CFG),
        "-o",
        str(out),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert not out.exists()  # ni siquiera se crea el directorio


def test_generate_syntax_error_exits_one(tmp_path: Path) -> None:
    bad = tmp_path / "roto.sql"
    bad.write_text("CREATE TABLE t (id INT PRIMARY KEY", encoding="utf-8")
    result = _invoke(
        "generate", str(bad), "-c", _config(tmp_path, _CICLOS_CFG), "-o", str(tmp_path / "o")
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_generate_config_error_exits_four(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "tables: {t: {desconocido: 1}}\n")  # campo desconocido
    result = _invoke("generate", _fixture("ciclos_nullable"), "-c", cfg, "-o", str(tmp_path / "o"))
    assert result.exit_code == 4
    assert "Traceback" not in result.output


def test_generate_plan_error_exits_four(tmp_path: Path) -> None:
    # Una regla que referencia una columna inexistente ⇒ PlanError en compilación.
    cfg = _config(
        tmp_path,
        'tables: {pedidos: {rows: 3, rules: ["fecha >= no_existe"]}, facturas: {rows: 3}}\n',
    )
    result = _invoke("generate", _fixture("ciclos_nullable"), "-c", cfg, "-o", str(tmp_path / "o"))
    assert result.exit_code == 4
    assert "no_existe" in result.output
    assert "Traceback" not in result.output


def test_generate_abort_exits_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if "fecha" in row and row.get("id") == 1:
                row["fecha"] = None  # NOT NULL a NULL ⇒ fila inválida

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    cfg = _config(
        tmp_path,
        "seed: 3\noutput: {on_error: abort}\ntables: {pedidos: {rows: 5}, facturas: {rows: 5}}\n",
    )
    result = _invoke("generate", _fixture("ciclos_nullable"), "-c", cfg, "-o", str(tmp_path / "o"))
    assert result.exit_code == 5
    assert "abort" in result.output
    assert "Traceback" not in result.output


def test_generate_reports_non_empty_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if "fecha" in row and row.get("id") == 1:
                row["fecha"] = None

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    out = tmp_path / "out"
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(out),
    )
    assert result.exit_code == 0, result.output  # quarantine no cambia el código
    assert "Cuarentena" in result.output
    assert "pedidos" in result.output


# --- export ------------------------------------------------------------------


def test_export_writes_seed_sql(tmp_path: Path) -> None:
    seed = tmp_path / "seed.sql"
    result = _invoke(
        "export",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "--format",
        "sql",
        "-o",
        str(seed),
    )
    assert result.exit_code == 0, result.output
    script = seed.read_text(encoding="utf-8")
    assert "INSERT INTO pedidos" in script
    assert "UPDATE pedidos SET" in script


def test_export_bad_format_exits_four(tmp_path: Path) -> None:
    result = _invoke(
        "export",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "--format",
        "csv",
        "-o",
        str(tmp_path / "seed.sql"),
    )
    assert result.exit_code == 4
    assert "Traceback" not in result.output


def test_export_dry_run_writes_nothing(tmp_path: Path) -> None:
    seed = tmp_path / "seed.sql"
    result = _invoke(
        "export",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "--format",
        "sql",
        "-o",
        str(seed),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert not seed.exists()
    assert "Muestra" in result.output
