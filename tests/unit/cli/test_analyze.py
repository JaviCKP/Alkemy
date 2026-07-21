"""Tests de la CLI `synthdb analyze` (T1.8, cierre del Hito 1).

`synthdb analyze RUTA.sql` ejecuta el pipeline estructural completo
(`parse_ddl` → `interpret_checks` → `analyze_structure` → `resolve_cycles`)
y lo presenta al usuario, con salida Rich por defecto y `--json` como
alternativa serializable. Estos tests parsean SQL real de los fixtures de
`tests/schemas/` (la definición operativa de «correcto», CLAUDE.md) a
través de `typer.testing.CliRunner`: comprueban códigos de salida, avisos y
determinismo, no un `SchemaSpec` construido a mano.

`CliRunner` (Click ≥ 8.2) separa los flujos: `result.output` contiene tanto
stdout (la presentación / el JSON) como stderr (los diagnósticos de error),
así que basta con inspeccionar `result.output` para cualquier aserción de
contenido, venga del flujo que venga.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from synthdb.cli import app

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
_runner = CliRunner()

_FIXTURES = sorted(path.stem for path in _SCHEMAS_DIR.glob("*.sql"))

# Único fixture sin ninguna secuencia de carga válida sin tocar el DDL: FK
# mutuas NOT NULL y ninguna diferible (especificacion.md §6.2, opción 3). La
# CLI debe salir con 2 (UnbreakableCycle), no con 0.
_UNBREAKABLE = "ciclos_unbreakable"


def _analyze(*args: str) -> Result:
    """Invoca `synthdb analyze` con `args` y devuelve el resultado del runner."""
    return _runner.invoke(app, ["analyze", *args])


def _fixture(name: str) -> str:
    """Ruta (como str) del fixture `name` de `tests/schemas/`."""
    return str(_SCHEMAS_DIR / f"{name}.sql")


def test_there_are_exactly_fourteen_fixtures() -> None:
    # Guarda de que el parametrizado de abajo cubre de verdad LOS 14 fixtures
    # y no un subconjunto por un glob que se quedó corto. Los dos últimos son
    # las variantes UUID de la autorreferencia compuesta multi-tenant de #44.
    assert len(_FIXTURES) == 14


@pytest.mark.parametrize("fixture", _FIXTURES)
def test_analyze_over_every_fixture_exits_with_its_code(fixture: str) -> None:
    # analyze sobre los 14 fixtures: 13 terminan el pipeline (exit 0) y
    # ciclos_unbreakable se detiene con diagnóstico (exit 2).
    result = _analyze(_fixture(fixture))
    expected = 2 if fixture == _UNBREAKABLE else 0
    assert result.exit_code == expected, result.output


def test_opaco_analyzes_cleanly_and_reports_no_structural_warnings() -> None:
    # opaco.sql es estructuralmente impecable en el Hito 1: sus nombres
    # opacos son ambigüedad SEMÁNTICA (capa LLM/heurísticas, H3), no un aviso
    # estructural. El pipeline (parser + checks + grafo) no emite ninguno, así
    # que la CLI debe presentarlo honestamente como «sin avisos», nunca
    # inventar uno con confianza (CLAUDE.md: nada se inventa en silencio).
    result = _analyze(_fixture("opaco"))

    assert result.exit_code == 0
    assert "t1" in result.output
    assert "t2" in result.output
    assert "Sin avisos" in result.output


def test_structural_warnings_are_surfaced_grouped_by_origin() -> None:
    # rrhh_autoref_notnull es el único fixture que produce un aviso
    # estructural en H1 (autorreferencia NOT NULL no diferible → las raíces
    # se referencian a sí mismas). La CLI debe mostrarlo, agrupado por origen.
    result = _analyze(_fixture("rrhh_autoref_notnull"))

    assert result.exit_code == 0
    out = result.output
    assert "Grafo" in out  # el bloque de avisos va agrupado por origen
    assert "empleados" in out
    assert "manager_id" in out


def test_unbreakable_cycle_exits_two_with_actionable_diagnostic() -> None:
    result = _analyze(_fixture("ciclos_unbreakable"))

    assert result.exit_code == 2
    out = result.output
    # el diagnóstico menciona las tablas implicadas en el ciclo
    assert "facturas" in out
    assert "pedidos" in out
    # y ninguna de las tres salidas se escapa a un traceback crudo
    assert "Traceback" not in out


def test_syntax_error_exits_one_without_traceback(tmp_path: Path) -> None:
    bad = tmp_path / "roto.sql"
    bad.write_text("CREATE TABLE t (id INT PRIMARY KEY", encoding="utf-8")

    result = _analyze(str(bad))

    assert result.exit_code == 1
    out = result.output
    assert "Traceback" not in out  # mensaje del parser tal cual, sin traceback
    assert "ParseError" not in out  # ni el nombre del tipo interno se filtra


def test_missing_file_exits_three_without_traceback(tmp_path: Path) -> None:
    result = _analyze(str(tmp_path / "no_existe.sql"))

    assert result.exit_code == 3
    assert "Traceback" not in result.output


def test_human_output_shows_phases_and_check_bounds() -> None:
    result = _analyze(_fixture("inmobiliaria"))

    assert result.exit_code == 0
    out = result.output
    assert "Fases" in out  # la sección de fases de generación
    assert "Insert" in out  # inmobiliaria no tiene ciclos: todo Insert
    assert "clientes" in out
    assert "viviendas" in out
    # CHECK (anio_construccion BETWEEN 1900 AND 2026) interpretado a cotas
    assert "1900" in out
    assert "2026" in out


def test_human_output_shows_arrays_and_directed_set_null_columns() -> None:
    # crm_real_minimo (ADR-004) ejercita las tres novedades en la salida Rich:
    # una columna text[] (roles), ON DELETE SET NULL acotado a una columna
    # (match_id) y la rotura del ciclo anulando SOLO esa columna.
    result = _analyze(_fixture("crm_real_minimo"))

    assert result.exit_code == 0
    out = result.output
    assert "text[]" in out  # el array se representa con su sufijo
    assert "set_null (match_id)" in out  # la acción dirigida a una columna
    assert "Update" in out  # el ciclo se rompe (Insert + Update), no es irrompible
    assert "Sin avisos" in out  # y no hay avisos estructurales


def test_json_output_is_parseable_and_has_expected_keys() -> None:
    result = _analyze(_fixture("inmobiliaria"), "--json")

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert {"schema", "phases", "warnings"} <= data.keys()
    assert data["schema"]["tables"]  # el esquema serializado no está vacío
    assert isinstance(data["phases"], list)
    assert data["phases"]  # inmobiliaria tiene fases
    assert isinstance(data["warnings"], list)


def test_json_phases_carry_their_discriminating_kind() -> None:
    # La serialización canónica de cada Phase incluye su `kind`: es lo que un
    # consumidor de JSON usa para distinguir Insert/InsertLeveled/Update/Deferred.
    result = _analyze(_fixture("inmobiliaria"), "--json")

    data = json.loads(result.output)
    assert all("kind" in phase for phase in data["phases"])


def test_json_output_is_byte_for_byte_deterministic() -> None:
    first = _analyze(_fixture("inmobiliaria"), "--json")
    second = _analyze(_fixture("inmobiliaria"), "--json")

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output
