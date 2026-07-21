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


def test_generate_dry_run_reports_quarantine_if_rendering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El informe no se pierde si el render del dry-run falla tras generar."""
    monkeypatch.setattr(engine, "complete_batch", _quarantine_pedidos_row_one)

    def fail_render(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fallo de render de muestra")

    monkeypatch.setattr("synthdb.cli._render_dry_run", fail_render)
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(tmp_path / "out"),
        "--dry-run",
    )

    assert result.exit_code == 1
    assert result.output.count("Cuarentena:") == 1
    assert "pedidos: 1 fila(s)" in result.output
    assert "Primer motivo:" in result.output


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


# --- Seguridad (revisión PR #42): cuarentena + SERIAL bloquea `export` -------


def test_export_with_gapped_serial_sequence_exits_four_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproducción end-to-end vía CLI del hallazgo 3: parent pierde su fila
    # intermedia (id=2) por cuarentena; varios child sobreviven referenciando
    # el id posterior al hueco (parent_id=3). export debe rechazarlo con
    # código 4, sin traceback, y sin escribir ningún seed.sql.
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(
        "CREATE TABLE parent (id SERIAL PRIMARY KEY, value INT NOT NULL);"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL "
        "REFERENCES parent(id));",
        encoding="utf-8",
    )
    cfg = _config(
        tmp_path,
        "seed: 7\n"
        "tables:\n"
        "  parent: {rows: 3}\n"
        "  child: {rows: 6, fk: {parent_id: {strategy: uniform}}}\n",
    )

    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if "value" in row and row.get("id") == 2:
                row["value"] = None

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    seed = tmp_path / "seed.sql"
    result = _invoke("export", str(schema_path), "-c", cfg, "--format", "sql", "-o", str(seed))

    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    assert "parent" in result.output
    assert not seed.exists()


def test_generate_with_gapped_serial_sequence_still_writes_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # El mismo escenario que arriba no afecta a generate --format csv/json:
    # cada fila lleva su id, así que no hay secuencia SERIAL que desalinear.
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(
        "CREATE TABLE parent (id SERIAL PRIMARY KEY, value INT NOT NULL);"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL "
        "REFERENCES parent(id));",
        encoding="utf-8",
    )
    cfg = _config(
        tmp_path,
        "seed: 7\n"
        "tables:\n"
        "  parent: {rows: 3}\n"
        "  child: {rows: 6, fk: {parent_id: {strategy: uniform}}}\n",
    )

    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if "value" in row and row.get("id") == 2:
                row["value"] = None

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    out = tmp_path / "out"
    result = _invoke("generate", str(schema_path), "-c", cfg, "-o", str(out))

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.glob("*.csv")) == ["child.csv", "parent.csv"]


# --- Seguridad (revisión PR #42): errores de E/S al escribir la salida ------


def test_generate_out_pointing_to_an_existing_file_exits_three(tmp_path: Path) -> None:
    # Reproducción del hallazgo 5: --out apunta a un archivo existente (no un
    # directorio). mkdir(exist_ok=True) solo tolera un directorio existente,
    # no un archivo: sin capturar, esto era un FileExistsError con traceback.
    blocker = tmp_path / "blocker"
    blocker.write_text("contenido preexistente que no debe tocarse", encoding="utf-8")
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(blocker),
    )
    assert result.exit_code == 3, result.output
    assert "Traceback" not in result.output
    # Rich puede partir la ruta larga en cualquier punto (incluso a mitad de
    # palabra) al ajustar el ancho de la consola; se compara sin NINGÚN
    # espacio en blanco (ni espacios ni saltos de línea) para no depender de
    # dónde cae el ajuste. El mensaje usa `repr(str(path))` (como `_read_sql`),
    # que en Windows dobla cada backslash: se compara contra ese mismo repr.
    no_whitespace = "".join(result.output.split())
    assert "".join(repr(str(blocker)).split()) in no_whitespace
    # El archivo preexistente no se tocó: ni se convirtió en directorio ni se
    # sobrescribió su contenido.
    assert blocker.is_file()
    assert blocker.read_text(encoding="utf-8") == "contenido preexistente que no debe tocarse"


def test_export_out_with_a_file_as_parent_directory_exits_three(tmp_path: Path) -> None:
    # Analogía para export: --out cuyo directorio padre ya existe como un
    # archivo (no se puede crear ese "directorio" para alojar seed.sql).
    blocker = tmp_path / "blocker"
    blocker.write_text("no soy un directorio", encoding="utf-8")
    seed = blocker / "seed.sql"
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
    assert result.exit_code == 3, result.output
    assert "Traceback" not in result.output
    assert not seed.exists()
    assert blocker.read_text(encoding="utf-8") == "no soy un directorio"


# --- Seguridad (revisión PR #42): saltos de línea reproducibles en export ---


def test_export_writes_seed_sql_with_fixed_newline_even_on_windows(tmp_path: Path) -> None:
    # Revisión PR #42, hallazgo 6: seed.sql lleva muchos '\n' entre sentencias
    # (BEGIN/INSERT/COMMIT); out.write_text sin newline="" los traduciría a
    # '\r\n' en Windows, rompiendo la reproducibilidad byte a byte entre
    # plataformas del propio criterio de aceptación del Hito 2 (T2.16).
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
    raw = seed.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") > 1


# --- Seguridad (revisión PR #42, R3-1): colisión de nombres → CLI sin traceback ---


def test_generate_maps_emit_path_error_to_exit_four_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Una colisión/contención de nombres de archivo (EmitPathError) no puede
    # escaparse como ValueError con traceback: la CLI la mapea a código 4 con
    # mensaje accionable. Se fuerza el error en la frontera (generate_files),
    # ya que con la codificación nueva ningún esquema válido lo produce.
    from synthdb.emit import EmitPathError

    def boom(*_args: object, **_kwargs: object) -> list[Path]:
        raise EmitPathError("las tablas 'a' y 'A' producirían el mismo archivo de salida (…).")

    monkeypatch.setattr("synthdb.cli.generate_files", boom)
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(tmp_path / "out"),
    )
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    assert "mismo archivo de salida" in result.output


# --- Seguridad (revisión PR #42, R3-2): la cuarentena se informa SIEMPRE -----


_SERIAL_GAP_SCHEMA = (
    "CREATE TABLE parent (id SERIAL PRIMARY KEY, value INT NOT NULL);"
    "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL REFERENCES parent(id));"
)
_SERIAL_GAP_CFG = (
    "seed: 7\ntables:\n  parent: {rows: 3}\n"
    "  child: {rows: 6, fk: {parent_id: {strategy: uniform}}}\n"
)


def _quarantine_parent_row_two(batch: list[dict[str, Any]]) -> None:
    for row in batch:
        if "value" in row and row.get("id") == 2:
            row["value"] = None  # NOT NULL a NULL ⇒ cuarentena de la fila 2


def test_export_serial_gap_reports_quarantine_before_exiting_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ExportIntegrityError (hueco SERIAL) aborta export con código 4, pero la
    # cuarentena que lo causó debe informarse igualmente (R3-2).
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(_SERIAL_GAP_SCHEMA, encoding="utf-8")
    monkeypatch.setattr(engine, "complete_batch", _quarantine_parent_row_two)
    seed = tmp_path / "seed.sql"
    result = _invoke(
        "export",
        str(schema_path),
        "-c",
        _config(tmp_path, _SERIAL_GAP_CFG),
        "--format",
        "sql",
        "-o",
        str(seed),
    )
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    assert not seed.exists()
    # La cuarentena se informa exactamente una vez, con tabla y primer motivo.
    assert result.output.count("Cuarentena:") == 1
    assert "parent" in result.output
    assert "parent: 1 fila(s)" in result.output
    assert "Primer motivo:" in result.output


def test_generate_io_error_still_reports_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Un fallo de E/S al escribir (--out es un archivo existente) no debe
    # tragarse el informe de cuarentena (R3-2): exit 3 + cuarentena informada.
    monkeypatch.setattr(engine, "complete_batch", _quarantine_pedidos_row_one)
    blocker = tmp_path / "blocker"
    blocker.write_text("preexistente", encoding="utf-8")
    result = _invoke(
        "generate",
        _fixture("ciclos_nullable"),
        "-c",
        _config(tmp_path, _CICLOS_CFG),
        "-o",
        str(blocker),
    )
    assert result.exit_code == 3, result.output
    assert "Traceback" not in result.output
    assert result.output.count("Cuarentena:") == 1
    assert "pedidos" in result.output


def test_export_io_error_still_reports_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La escritura fallida posterior a `Dataset` no pierde el informe."""

    def quarantine_first_date_row(batch: list[dict[str, Any]]) -> None:
        for row in batch[:1]:
            row["fecha"] = None

    monkeypatch.setattr(engine, "complete_batch", quarantine_first_date_row)
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(
        "CREATE TABLE pedidos (id INT PRIMARY KEY, fecha DATE NOT NULL);", encoding="utf-8"
    )
    config_path = Path(_config(tmp_path, "seed: 3\ntables: {pedidos: {rows: 3}}\n"))

    def fail_write_bytes(_path: Path, _data: bytes) -> int:
        raise OSError("disco lleno")

    monkeypatch.setattr(Path, "write_bytes", fail_write_bytes)
    result = _invoke(
        "export",
        str(schema_path),
        "-c",
        str(config_path),
        "--format",
        "sql",
        "-o",
        str(tmp_path / "seed.sql"),
    )

    assert result.exit_code == 3, result.output
    assert "Traceback" not in result.output
    assert result.output.count("Cuarentena:") == 1
    assert "pedidos: 1 fila(s)" in result.output
    assert "Primer motivo:" in result.output


def _quarantine_pedidos_row_one(batch: list[dict[str, Any]]) -> None:
    for row in batch:
        if "fecha" in row and row.get("id") == 1:
            row["fecha"] = None
