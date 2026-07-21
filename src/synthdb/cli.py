"""Punto de entrada de la CLI (Typer + Rich). Los subcomandos se añaden hito a hito.

El Hito 1 estrena `synthdb analyze RUTA.sql`, que ejecuta el pipeline
estructural completo — `parse_ddl` (T1.3) → `interpret_checks` (T1.4) →
`analyze_structure` (T1.6) → `resolve_cycles` (T1.7) — y lo presenta al
usuario sin generar todavía ni una fila: el resumen de cada tabla, las fases
de generación en orden y TODOS los avisos acumulados, agrupados por origen.
Con `--json` vuelca el mismo análisis en la serialización canónica de los
modelos de la IR, apto para consumo programático.

El Hito 2 cierra con tres comandos que ya generan datos deterministas sin LLM
(especificacion.md §11):

- `synthdb plan RUTA.sql [-c config.yaml] [--json] [--no-llm]` muestra el plan
  de generación por columna (generador, fuente, confianza, reglas y avisos) y
  las fases, sin generar nada. `--no-llm` es el estado natural del H2 (aún no
  hay modelo) y queda declarado como no-op hasta el Hito 3.
- `synthdb generate RUTA.sql -c config.yaml -o DIR [--format csv|json]` genera
  y escribe un archivo por tabla.
- `synthdb export RUTA.sql -c config.yaml --format sql -o seed.sql` genera y
  escribe un `seed.sql` de PostgreSQL.

`generate` y `export` aceptan `--dry-run`: ejecutan el pipeline completo,
imprimen el plan y 10 filas de muestra por tabla, y **no escriben nada**.

Códigos de salida (sin traceback nunca, CLAUDE.md): `0` correcto (con o sin
avisos), `1` error de sintaxis SQL (`ParseError`), `2` ciclo irrompible
(`UnbreakableCycle`), `3` archivo inexistente o ilegible, `4` error de plan o
de configuración (`PlanError`/`ConfigError`, con el mensaje completo), `5`
generación abortada por `output.on_error=abort` ante una fila inválida. La
cuarentena no vacía se informa SIEMPRE al final (tabla, nº de filas y primer
motivo). Los diagnósticos de error van a stderr como texto plano; la
presentación y el JSON, a stdout.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from synthdb.config.loader import ConfigError, load_config
from synthdb.config.models import Config
from synthdb.constraints.check_interp import interpret_checks
from synthdb.emit import ExportIntegrityError, generate_files, render_sql
from synthdb.generation.engine import Dataset, GenerationError, generate_dataset
from synthdb.generation.engine import PlanError as EnginePlanError
from synthdb.graph.dependency import analyze_structure
from synthdb.graph.strategies import UnbreakableCycle, resolve_cycles
from synthdb.ir.plans import (
    ColumnPlan,
    InsertLeveledPhase,
    InsertPhase,
    Phase,
    TablePlan,
    TablePlans,
    UpdatePhase,
)
from synthdb.ir.schema import CheckSpec, RelationshipSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.parsing.ddl import ParseError, parse_ddl
from synthdb.rules import RuleParseError, as_bound, as_derivation, parse_rule
from synthdb.semantic.merge import PlanError as MergePlanError
from synthdb.semantic.merge import build_plan

app = typer.Typer(name="synthdb", no_args_is_help=True)


@app.callback()
def main() -> None:
    """SynthDB: datos sintéticos coherentes desde un esquema SQL relacional.

    El callback existe para que `analyze` sea un subcomando con nombre propio
    (`synthdb analyze ...`) en lugar de colapsarse en la raíz de la CLI, que
    es lo que Typer haría con un único comando sin callback. Los siguientes
    hitos añaden aquí sus propios subcomandos.
    """


_EXIT_PARSE_ERROR = 1
_EXIT_UNBREAKABLE_CYCLE = 2
_EXIT_IO_ERROR = 3
_EXIT_PLAN_OR_CONFIG_ERROR = 4
_EXIT_ABORT = 5

_PHASE_LABELS: dict[str, str] = {
    "insert": "Insert",
    "insert_leveled": "InsertLeveled",
    "update": "Update",
    "deferred": "Deferred",
}
"""Etiqueta legible de cada `Phase` por su `kind` (el discriminante de la unión)."""


@app.command()
def analyze(
    path: Annotated[
        Path,
        typer.Argument(metavar="RUTA.sql", help="Archivo .sql con el DDL a analizar."),
    ],
    dialect: Annotated[
        str,
        typer.Option(help="Dialecto SQL para el parser (el MVP solo promete PostgreSQL)."),
    ] = "postgres",
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Vuelca el análisis como JSON en vez de la tabla Rich."),
    ] = False,
) -> None:
    """Analiza un esquema SQL: IR, fases de generación y avisos, sin generar datos.

    Ejecuta el pipeline estructural del Hito 1 de principio a fin y lo
    presenta, en formato Rich o `--json`. No inserta ni escribe nada: es una
    inspección de solo lectura del esquema.

    Args:
        path: Ruta al archivo `.sql` con el DDL. No hace falta que exista aún
            al declarar el comando; su ausencia se traduce en salida 3.
        dialect: Dialecto que usa el parser para tokenizar el DDL.
        as_json: Si es `True`, emite el análisis como un único documento JSON
            determinista en stdout en lugar de la presentación Rich.
    """
    _force_utf8_output()
    stderr = Console(stderr=True, highlight=False)

    sql = _read_sql(path, stderr)

    try:
        spec = parse_ddl(sql, dialect)
    except ParseError as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PARSE_ERROR) from exc

    # Los avisos se acumulan en dos modelos distintos (spec: parser + checks;
    # plan: grafo + estrategias). Se capturan por tramos para poder agruparlos
    # por origen sin perder de qué fase vienen.
    parser_warnings = list(spec.warnings)
    spec = interpret_checks(spec)
    check_warnings = spec.warnings[len(parser_warnings) :]

    plan = analyze_structure(spec)
    graph_warnings = list(plan.warnings)
    try:
        phases = resolve_cycles(plan, spec)
    except UnbreakableCycle as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_UNBREAKABLE_CYCLE) from exc
    grafo_warnings = graph_warnings + plan.warnings[len(graph_warnings) :]

    if as_json:
        all_warnings = parser_warnings + check_warnings + grafo_warnings
        typer.echo(_json_payload(spec, phases, all_warnings))
        return

    _render(spec, phases, parser_warnings, check_warnings, grafo_warnings)


def _force_utf8_output() -> None:
    """Reconfigura stdout/stderr a UTF-8 para no romper en consolas cp1252.

    En Windows, `sys.stdout` suele ser cp1252 y Rich aborta con
    `UnicodeEncodeError` en cuanto emite un carácter como `→`, `⇒` o los
    bordes de tabla (`─`). Forzar UTF-8 (con `errors="replace"` como red de
    seguridad) evita ese fallo sin renunciar a la presentación. Si el flujo no
    admite `reconfigure` (p. ej. ya envuelto por un runner de tests), se deja
    tal cual: ese caso ya maneja Unicode de por sí.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _read_sql(path: Path, stderr: Console) -> str:
    """Lee el DDL de `path`, o sale con código 3 si no existe o no se puede leer."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "strerror", None) or str(exc)
        stderr.print(
            f"No se puede leer el archivo {str(path)!r}: {detail}. "
            "Comprueba la ruta y los permisos.",
            markup=False,
        )
        raise typer.Exit(_EXIT_IO_ERROR) from exc


def _io_error_message(exc: OSError | UnicodeError, path: Path) -> str:
    """Mensaje accionable de un fallo al escribir en `path` (mismo código 3 que `_read_sql`).

    Cubre `generate`/`export` (revisión PR #42, hallazgo 5): `--out` apunta a
    un archivo ya existente en vez de un directorio (`FileExistsError`/
    `NotADirectoryError` al crearlo), su directorio padre no existe o no se
    puede crear, los permisos deniegan la escritura, o la propia escritura
    falla (disco lleno, ruta demasiado larga...). `UnicodeError` se cubre por
    homogeneidad con `_read_sql`, aunque en la práctica no debería producirse
    escribiendo UTF-8 desde cadenas Python ya válidas.
    """
    detail = getattr(exc, "strerror", None) or str(exc)
    return (
        f"No se pudo escribir en {str(path)!r}: {detail}. Comprueba que la ruta no exista ya "
        "como un archivo (o como un directorio, según el caso), que su directorio padre se "
        "pueda crear, y los permisos de escritura."
    )


def _json_payload(spec: SchemaSpec, phases: list[Phase], warnings: list[str]) -> str:
    """Serializa el análisis a JSON canónico (`schema`/`phases`/`warnings`).

    Usa la serialización propia de cada modelo Pydantic (`model_dump`), nunca
    dicts construidos a mano, y ordena las claves para que dos ejecuciones con
    la misma entrada produzcan bytes idénticos (CLAUDE.md: determinismo).

    Args:
        spec: Esquema ya parseado, con checks interpretados y campos derivados
            (`kind`, `cardinality_hint`) rellenados por el grafo.
        phases: Secuencia de fases de generación de `resolve_cycles`.
        warnings: Todos los avisos acumulados (parser + checks + grafo), en
            orden de pipeline.

    Returns:
        El documento JSON como texto, con `schema` (la IR), `phases` (la lista
        de fases) y `warnings` (la lista completa de avisos).
    """
    payload: dict[str, Any] = {
        "schema": spec.model_dump(mode="json", by_alias=True),
        "phases": [phase.model_dump(mode="json") for phase in phases],
        "warnings": warnings,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)


def _render(
    spec: SchemaSpec,
    phases: list[Phase],
    parser_warnings: list[str],
    check_warnings: list[str],
    grafo_warnings: list[str],
) -> None:
    """Presenta el análisis en Rich: tablas, fases y avisos agrupados."""
    console = Console(highlight=False)
    console.rule(Text(f"Esquema ({spec.dialect}) · {len(spec.tables)} tablas"))
    for table in spec.tables:
        _render_table(console, table)

    _render_phases(console, phases)
    _render_warnings(console, parser_warnings, check_warnings, grafo_warnings)


def _render_table(console: Console, table: TableSpec) -> None:
    """Resumen de una tabla: columnas, PK, FKs, uniques, checks, kind y comentario."""
    qualified = f"{table.schema_}.{table.name}" if table.schema_ else table.name
    console.print()
    console.rule(Text(f"Tabla {qualified}  ·  kind={table.kind}"))
    if table.comment:
        console.print(Text(f"  «{table.comment}»", style="italic"))

    columns = Table(show_edge=False, pad_edge=False, expand=False)
    columns.add_column("Columna")
    columns.add_column("Tipo canónico")
    columns.add_column("Nulo")
    columns.add_column("Default")
    for column in table.columns:
        default = column.default.sql_text if column.default is not None else "—"
        columns.add_row(
            Text(column.name),
            Text(_format_type(column.type, column.enum_values)),
            Text("sí" if column.nullable else "no"),
            Text(default),
        )
    console.print(columns)

    if table.primary_key:
        console.print(f"  PK: {', '.join(table.primary_key)}", markup=False, style="bold")
    for fk in table.foreign_keys:
        console.print(f"  FK: {_format_fk(fk)}", markup=False)
    for unique in table.uniques:
        console.print(f"  UNIQUE: ({', '.join(unique)})", markup=False)
    # Checks de columna (los que solo la afectan a ella) y de tabla
    # (potencialmente multi-columna): ambos se listan aquí, con sus cotas si
    # `interpret_checks` los reconoció y marcando los que no.
    column_checks = [check for column in table.columns for check in column.checks]
    for check in column_checks + list(table.checks):
        console.print(f"  CHECK: {_format_check(check)}", markup=False)


def _render_phases(console: Console, phases: list[Phase]) -> None:
    """Lista las fases de generación en orden, con su tipo y sus tablas."""
    console.print()
    console.rule(Text(f"Fases de generación ({len(phases)})"))
    for index, phase in enumerate(phases, start=1):
        label, detail = _describe_phase(phase)
        console.print(f"  {index}. {label:<14} {detail}", markup=False)


def _render_warnings(
    console: Console,
    parser_warnings: list[str],
    check_warnings: list[str],
    grafo_warnings: list[str],
) -> None:
    """Muestra todos los avisos acumulados, agrupados por origen, al final."""
    groups = [
        ("Parser DDL", parser_warnings),
        ("Interpretación de CHECKs", check_warnings),
        ("Grafo de dependencias", grafo_warnings),
    ]
    total = sum(len(warnings) for _, warnings in groups)

    console.print()
    console.rule(Text(f"Avisos ({total})"))
    if total == 0:
        console.print("  Sin avisos estructurales.", markup=False, style="green")
        return

    for origin, warnings in groups:
        if not warnings:
            continue
        console.print(f"  {origin} ({len(warnings)}):", markup=False, style="bold yellow")
        for warning in warnings:
            console.print(Text(f"    • {warning}", style="yellow"))


def _format_type(type_spec: TypeSpec, enum_values: list[str] | None) -> str:
    """Tipo canónico de una columna como texto legible; añade `[]` si es un array."""
    base = _format_scalar_type(type_spec, enum_values)
    return f"{base}[]" if type_spec.is_array else base


def _format_scalar_type(type_spec: TypeSpec, enum_values: list[str] | None) -> str:
    """Tipo del elemento como texto legible (kind más sus parámetros), sin la dimensión."""
    kind = type_spec.kind
    if kind == "enum" and enum_values:
        return f"enum({', '.join(enum_values)})"
    if kind == "numeric" and type_spec.precision is not None:
        if type_spec.scale is not None:
            return f"numeric({type_spec.precision}, {type_spec.scale})"
        return f"numeric({type_spec.precision})"
    if kind in ("varchar", "char") and type_spec.length is not None:
        return f"{kind}({type_spec.length})"
    if kind == "timestamp" and type_spec.with_timezone:
        return "timestamp (con tz)"
    if kind == "integer" and type_spec.autoincrement:
        return "integer (serial)"
    return kind


def _format_fk(fk: RelationshipSpec) -> str:
    """FK como texto: columnas → destino, con cardinalidad, deferrable y acciones."""
    ref_columns = f"({', '.join(fk.ref_columns)})" if fk.ref_columns else "(PK)"
    parts = [f"({', '.join(fk.columns)}) → {fk.ref_table}{ref_columns}"]
    if fk.cardinality_hint is not None:
        parts.append(fk.cardinality_hint)
    if fk.match_full:
        parts.append("MATCH FULL")
    if fk.deferrable:
        parts.append("DEFERRABLE")
    if fk.on_delete is not None:
        detail = f"ON DELETE {fk.on_delete}"
        if fk.on_delete_set_columns:
            detail += f" ({', '.join(fk.on_delete_set_columns)})"
        parts.append(detail)
    if fk.on_update is not None:
        parts.append(f"ON UPDATE {fk.on_update}")
    return "  ·  ".join(parts)


def _format_check(check: CheckSpec) -> str:
    """CHECK como texto: el predicado y, si se interpretó, sus cotas derivadas."""
    if check.ast_supported:
        return f"{check.sql_text}  ⇒ cotas {check.bounds_derived}"
    return f"{check.sql_text}  (no interpretado como cota)"


def _describe_phase(phase: Phase) -> tuple[str, str]:
    """Etiqueta legible y detalle (tablas y particularidades) de una fase."""
    label = _PHASE_LABELS[phase.kind]
    if isinstance(phase, InsertPhase):
        detail = ", ".join(phase.tables)
        if phase.null_fks:
            nulls = "; ".join(
                f"{fk.table}({', '.join(fk.null_columns)})→{fk.ref_table}" for fk in phase.null_fks
            )
            detail += f"  [columnas a NULL para romper el ciclo: {nulls}]"
        return label, detail
    if isinstance(phase, InsertLeveledPhase):
        detail = f"{phase.table}  [autoref por niveles: {', '.join(phase.self_fk_columns)}"
        if phase.roots_point_to_self:
            detail += "; raíces a sí mismas"
        detail += "]"
        return label, detail
    if isinstance(phase, UpdatePhase):
        return label, f"{phase.table}  [columnas: {', '.join(phase.columns)}]"
    return label, ", ".join(phase.tables)


# --- Comandos del Hito 2: plan / generate / export --------------------------


@app.command()
def plan(
    path: Annotated[
        Path,
        typer.Argument(metavar="RUTA.sql", help="Archivo .sql con el DDL a planificar."),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option("-c", "--config", metavar="config.yaml", help="Configuración del usuario."),
    ] = None,
    dialect: Annotated[
        str,
        typer.Option(help="Dialecto SQL para el parser (el MVP solo promete PostgreSQL)."),
    ] = "postgres",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Vuelca el plan como JSON determinista en vez de la tabla Rich."
        ),
    ] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Solo heurísticas (no-op en el H2: aún no hay LLM; ver H3)."),
    ] = False,
) -> None:
    """Muestra el plan de generación por columna y las fases, sin generar datos.

    Presenta, por tabla y columna, el generador elegido, su fuente (`user`/`ir`/
    `heuristic`/`fallback`), la confianza, las reglas del YAML que fijan esa
    columna y los avisos del fusor; después, las fases de generación en orden.
    Es una inspección de solo lectura: no escribe ni genera ninguna fila.

    Args:
        path: Ruta al archivo `.sql`. Su ausencia se traduce en salida 3.
        config_path: Configuración opcional; sin ella se usan los valores por
            defecto (heurísticas y `defaults`).
        dialect: Dialecto que usa el parser para tokenizar el DDL.
        as_json: Si es `True`, emite el plan como JSON determinista en stdout.
        no_llm: Declarado para estabilidad de la interfaz; sin efecto en el H2
            (la capa LLM llega en el H3, ADR-002).
    """
    _force_utf8_output()
    stderr = Console(stderr=True, highlight=False)

    sql = _read_sql(path, stderr)
    spec = _parse_or_exit(sql, dialect, stderr)
    config = _load_config_or_exit(config_path, stderr)
    plans, phases = _plan_and_phases_or_exit(spec, config, stderr)

    if as_json:
        typer.echo(_plan_json(plans, phases, config))
        return
    _render_plan(Console(highlight=False), spec, config, plans, phases)


@app.command()
def generate(
    path: Annotated[
        Path,
        typer.Argument(metavar="RUTA.sql", help="Archivo .sql con el DDL a generar."),
    ],
    config_path: Annotated[
        Path,
        typer.Option("-c", "--config", metavar="config.yaml", help="Configuración del usuario."),
    ],
    out_dir: Annotated[
        Path,
        typer.Option("-o", "--out", metavar="DIR", help="Directorio destino de los archivos."),
    ],
    fmt: Annotated[
        str,
        typer.Option("--format", help="Formato de salida: 'csv' o 'json'."),
    ] = "csv",
    dialect: Annotated[
        str,
        typer.Option(help="Dialecto SQL para el parser (el MVP solo promete PostgreSQL)."),
    ] = "postgres",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Ejecuta el pipeline, imprime el plan y 10 filas/tabla, no escribe nada.",
        ),
    ] = False,
) -> None:
    """Genera datos y escribe un archivo CSV o JSON por tabla en `DIR`.

    Con `--dry-run` ejecuta todo el pipeline y muestra el plan y 10 filas de
    muestra por tabla, pero no crea el directorio ni escribe ningún archivo.

    Args:
        path: Ruta al archivo `.sql`.
        config_path: Configuración del usuario (obligatoria).
        out_dir: Directorio destino; se crea si no existe (salvo `--dry-run`).
        fmt: `csv` (por defecto) o `json`.
        dialect: Dialecto del parser.
        dry_run: Si es `True`, no escribe ningún archivo ni crea efectos laterales.
    """
    _force_utf8_output()
    stderr = Console(stderr=True, highlight=False)

    if fmt not in {"csv", "json"}:
        stderr.print(
            f"formato de salida no soportado: {fmt!r}. Usa --format csv o --format json.",
            markup=False,
        )
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR)

    sql = _read_sql(path, stderr)
    spec = _parse_or_exit(sql, dialect, stderr)
    config = _load_config_or_exit(config_path, stderr)
    dataset = _run_engine_or_exit(spec, config, stderr)

    console = Console(highlight=False)
    if dry_run:
        _render_dry_run(console, spec, config, dataset)
        _report_quarantine(spec, dataset, stderr)
        return

    try:
        paths = generate_files(spec, dataset, out_dir, fmt)
    except (OSError, UnicodeError) as exc:
        stderr.print(_io_error_message(exc, out_dir), markup=False)
        raise typer.Exit(_EXIT_IO_ERROR) from exc
    console.print(f"Escritos {len(paths)} archivo(s) {fmt.upper()} en {out_dir}:", markup=False)
    for written in paths:
        console.print(f"  {written}", markup=False)
    _report_quarantine(spec, dataset, stderr)


@app.command()
def export(
    path: Annotated[
        Path,
        typer.Argument(metavar="RUTA.sql", help="Archivo .sql con el DDL a exportar."),
    ],
    config_path: Annotated[
        Path,
        typer.Option("-c", "--config", metavar="config.yaml", help="Configuración del usuario."),
    ],
    out: Annotated[
        Path,
        typer.Option("-o", "--out", metavar="seed.sql", help="Archivo SQL destino."),
    ],
    fmt: Annotated[
        str,
        typer.Option("--format", help="Formato de exportación (solo 'sql' en el MVP)."),
    ] = "sql",
    dialect: Annotated[
        str,
        typer.Option(help="Dialecto SQL para el parser (el MVP solo promete PostgreSQL)."),
    ] = "postgres",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Ejecuta el pipeline, imprime el plan y 10 filas/tabla, no escribe nada.",
        ),
    ] = False,
) -> None:
    """Genera datos y escribe un `seed.sql` de PostgreSQL cargable con `psql`.

    Con `--dry-run` ejecuta todo el pipeline y muestra el plan y 10 filas de
    muestra por tabla, pero no escribe el archivo.

    Args:
        path: Ruta al archivo `.sql`.
        config_path: Configuración del usuario (obligatoria).
        out: Ruta del `seed.sql` a escribir; su directorio se crea si no existe.
        fmt: Solo `sql` en el MVP.
        dialect: Dialecto del parser.
        dry_run: Si es `True`, no escribe el archivo ni crea efectos laterales.
    """
    _force_utf8_output()
    stderr = Console(stderr=True, highlight=False)

    if fmt != "sql":
        stderr.print(
            f"formato de exportación no soportado: {fmt!r}. El MVP solo exporta --format sql.",
            markup=False,
        )
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR)

    sql = _read_sql(path, stderr)
    spec = _parse_or_exit(sql, dialect, stderr)
    config = _load_config_or_exit(config_path, stderr)
    dataset = _run_engine_or_exit(spec, config, stderr)

    console = Console(highlight=False)
    if dry_run:
        _render_dry_run(console, spec, config, dataset)
        _report_quarantine(spec, dataset, stderr)
        return

    script = _render_sql_or_exit(spec, dataset, config, stderr)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        # `write_bytes`, no `write_text`: `script` lleva `\n` entre sentencias
        # y `Path.write_text` sin `newline=""` los traduciría a `\r\n` en
        # Windows, rompiendo la reproducibilidad byte a byte (revisión PR
        # #42, hallazgo 6).
        out.write_bytes(script.encode("utf-8"))
    except (OSError, UnicodeError) as exc:
        stderr.print(_io_error_message(exc, out), markup=False)
        raise typer.Exit(_EXIT_IO_ERROR) from exc
    console.print(f"Escrito {out} ({len(script)} bytes).", markup=False)
    _report_quarantine(spec, dataset, stderr)


# --- Ayudantes compartidos de los comandos de generación --------------------


def _render_sql_or_exit(spec: SchemaSpec, dataset: Dataset, config: Config, stderr: Console) -> str:
    """Renderiza el `seed.sql` o sale con código 4 si el `Dataset` no es exportable.

    `render_sql` puede rechazar un `Dataset` con cuarentena en una tabla
    autoincremental (`ExportIntegrityError`): cargarlo desalinearía la
    secuencia `SERIAL` de PostgreSQL frente a los ids que el `Dataset` en
    memoria registró. Se reporta sin traceback, con el mensaje completo (tabla,
    columna y causa) que ya construye `render_sql`.
    """
    try:
        return render_sql(spec, dataset, config)
    except ExportIntegrityError as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR) from exc


def _parse_or_exit(sql: str, dialect: str, stderr: Console) -> SchemaSpec:
    """Parsea el DDL o sale con código 1 (`ParseError`), sin traceback."""
    try:
        return parse_ddl(sql, dialect)
    except ParseError as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PARSE_ERROR) from exc


def _load_config_or_exit(config_path: Path | None, stderr: Console) -> Config:
    """Carga la configuración (o los valores por defecto) o sale con código 4."""
    if config_path is None:
        return Config()
    try:
        return load_config(config_path)
    except ConfigError as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR) from exc


def _plan_and_phases_or_exit(
    spec: SchemaSpec, config: Config, stderr: Console
) -> tuple[TablePlans, list[Phase]]:
    """Calcula el plan de columnas y las fases, mapeando los errores a su código."""
    interpreted = interpret_checks(spec)
    structural = analyze_structure(interpreted)
    try:
        phases = resolve_cycles(structural, interpreted)
    except UnbreakableCycle as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_UNBREAKABLE_CYCLE) from exc
    try:
        plans = build_plan(interpreted, config)
    except (MergePlanError, ConfigError) as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR) from exc
    return plans, phases


def _run_engine_or_exit(spec: SchemaSpec, config: Config, stderr: Console) -> Dataset:
    """Ejecuta el motor de generación, traduciendo cada error a su código de salida.

    Los errores previstos llevan su código propio (2 ciclo irrompible, 4 plan o
    configuración, 5 abortado). Cualquier otro error se presenta como un mensaje
    accionable sin traceback (CLAUDE.md: la CLI nunca vuelca una traza), con
    código 4: la causa típica es una regla o cota incompatible con el rango de un
    generador (p. ej. una fecha exigida fuera del rango por defecto 2015–2025;
    ver `docs/limitations.md`).
    """
    try:
        return generate_dataset(spec, config)
    except UnbreakableCycle as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_UNBREAKABLE_CYCLE) from exc
    except (EnginePlanError, ConfigError) as exc:
        stderr.print(str(exc), markup=False)
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR) from exc
    except GenerationError as exc:
        stderr.print(
            f"Generación abortada (output.on_error=abort): {exc}. "
            "Corrige el dato o usa on_error=quarantine para apartar la fila y continuar.",
            markup=False,
        )
        raise typer.Exit(_EXIT_ABORT) from exc
    except Exception as exc:  # noqa: BLE001 -- frontera de la CLI: nunca un traceback crudo
        stderr.print(
            f"Error no controlado durante la generación: {exc}. Puede deberse a una regla o "
            "cota incompatible con el rango de un generador (p. ej. una fecha exigida fuera de "
            "2015–2025); revisa las reglas y los rangos del YAML. Si el esquema parece correcto, "
            "abre una incidencia con un caso mínimo reproducible.",
            markup=False,
        )
        raise typer.Exit(_EXIT_PLAN_OR_CONFIG_ERROR) from exc


# --- Presentación del plan --------------------------------------------------


def _rules_by_column(table: TableSpec, config: Config) -> dict[str, list[str]]:
    """Mapea cada columna a las reglas del YAML que la fijan (bound o derivación).

    Solo asocia reglas parseables cuyo objetivo es una columna concreta; las
    reglas que no compilan o que son aserciones puras no se atribuyen a ninguna
    columna aquí (se listan íntegras aparte). `plan` es un inspector tolerante:
    no falla ante una regla inválida (eso lo hace `generate`, que sí compila).
    """
    tconf = config.tables.get(table.name)
    by_column: dict[str, list[str]] = {}
    if tconf is None:
        return by_column
    for text in tconf.rules:
        try:
            rule = parse_rule(text)
        except RuleParseError:
            continue
        bound = as_bound(rule)
        derivation = as_derivation(rule)
        target = (
            bound.column
            if bound is not None
            else (derivation.column if derivation is not None else None)
        )
        if target is not None:
            by_column.setdefault(target, []).append(text)
    return by_column


def _render_plan(
    console: Console,
    spec: SchemaSpec,
    config: Config,
    plans: TablePlans,
    phases: list[Phase],
) -> None:
    """Presenta el plan en Rich: por tabla, la rejilla de columnas y las fases."""
    console.rule(Text(f"Plan de generación · {len(plans.tables)} tablas"))
    by_spec = {table.name: table for table in spec.tables}
    for table_plan in plans.tables:
        _render_table_plan(console, by_spec[table_plan.table], config, table_plan)
    _render_phases(console, phases)
    _render_plan_summary(console, plans)


def _render_table_plan(
    console: Console, table: TableSpec, config: Config, table_plan: TablePlan
) -> None:
    """Rejilla de una tabla: generador, fuente, confianza, rol, reglas y avisos."""
    qualified = f"{table.schema_}.{table.name}" if table.schema_ else table.name
    console.print()
    console.rule(Text(f"Tabla {qualified}"))

    rules = _rules_by_column(table, config)
    grid = Table(show_edge=False, pad_edge=False, expand=False)
    grid.add_column("Columna")
    grid.add_column("Generador")
    grid.add_column("Fuente")
    grid.add_column("Confianza")
    grid.add_column("Rol")
    grid.add_column("Reglas")
    grid.add_column("Avisos")
    for column_plan in table_plan.columns:
        grid.add_row(
            Text(column_plan.column),
            Text(_generator_label(column_plan)),
            Text(column_plan.source),
            Text(f"{column_plan.confidence:.2f}"),
            Text(column_plan.role or "—"),
            Text("; ".join(rules.get(column_plan.column, [])) or "—"),
            Text(
                "\n".join(column_plan.warnings) or "—",
                style="yellow" if column_plan.warnings else "",
            ),
        )
    console.print(grid)
    for warning in table_plan.warnings:
        console.print(Text(f"  • {warning}", style="yellow"))


def _generator_label(column_plan: ColumnPlan) -> str:
    """Etiqueta del generador de una columna, o «— (BD)» si lo asigna la base."""
    if column_plan.generator is None:
        return "— (BD)"
    generator = column_plan.generator
    if generator.type == "fk":
        strategy = generator.params.get("strategy")
        return f"fk[{strategy}]" if strategy else "fk"
    return generator.type


def _render_plan_summary(console: Console, plans: TablePlans) -> None:
    """Resumen final del plan: recuento de columnas por fuente."""
    counts: dict[str, int] = {}
    for table_plan in plans.tables:
        for column_plan in table_plan.columns:
            counts[column_plan.source] = counts.get(column_plan.source, 0) + 1
    console.print()
    console.rule(Text("Resumen"))
    detail = ", ".join(f"{source}: {counts[source]}" for source in sorted(counts))
    console.print(f"  Columnas por fuente — {detail or 'sin columnas'}.", markup=False)


def _plan_json(plans: TablePlans, phases: list[Phase], config: Config) -> str:
    """Serializa el plan a JSON canónico y determinista (`tables`/`phases`/`rules`).

    Usa `model_dump` de cada modelo y ordena las claves, de modo que dos
    ejecuciones con la misma entrada producen bytes idénticos (CLAUDE.md).
    """
    payload: dict[str, Any] = {
        "tables": [table_plan.model_dump(mode="json") for table_plan in plans.tables],
        "phases": [phase.model_dump(mode="json") for phase in phases],
        "rules": {name: list(tconf.rules) for name, tconf in config.tables.items() if tconf.rules},
        "warnings": plans.warnings,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)


# --- Dry-run y cuarentena ---------------------------------------------------


def _render_dry_run(console: Console, spec: SchemaSpec, config: Config, dataset: Dataset) -> None:
    """Muestra el plan y 10 filas de muestra por tabla, sin escribir nada."""
    plans = (
        dataset.table_plans
        if dataset.table_plans is not None
        else build_plan(interpret_checks(spec), config)
    )
    _render_plan(console, spec, config, plans, dataset.phases)
    console.print()
    console.rule(Text("Muestra (hasta 10 filas por tabla)"))
    for table in spec.tables:
        _render_sample_rows(console, table, dataset.tables.get(table.name, []))


def _render_sample_rows(console: Console, table: TableSpec, rows: list[dict[str, Any]]) -> None:
    """Rejilla con las primeras 10 filas de una tabla (o «sin filas»)."""
    qualified = f"{table.schema_}.{table.name}" if table.schema_ else table.name
    console.print()
    console.print(Text(f"{qualified}  ({len(rows)} filas)", style="bold"))
    if not rows:
        console.print("  (sin filas)", markup=False)
        return
    grid = Table(show_edge=False, pad_edge=False, expand=False)
    for column in table.columns:
        grid.add_column(column.name)
    for row in rows[:10]:
        grid.add_row(*[_display_cell(row.get(column.name)) for column in table.columns])
    console.print(grid)


def _display_cell(value: Any) -> Text:
    """Representación breve de un valor para la muestra del dry-run."""
    if value is None:
        return Text("NULL", style="dim")
    return Text(str(value))


def _report_quarantine(spec: SchemaSpec, dataset: Dataset, stderr: Console) -> None:
    """Informa al final de la cuarentena no vacía: tabla, filas y primer motivo.

    Recorre las tablas en el orden del esquema para un informe determinista. No
    cambia el código de salida (con `on_error=quarantine` la generación es un
    éxito con filas apartadas); `abort` ya habría salido con código 5 antes.
    """
    if not dataset.quarantine:
        return
    total = sum(len(issues) for issues in dataset.quarantine.values())
    stderr.print()
    stderr.print(
        f"Cuarentena: {total} fila(s) apartada(s) por no cumplir una restricción.",
        markup=False,
        style="yellow",
    )
    for table in spec.tables:
        issues = dataset.quarantine.get(table.name)
        if not issues:
            continue
        _, _, first_reason = issues[0]
        stderr.print(
            f"  {table.name}: {len(issues)} fila(s). Primer motivo: {first_reason}",
            markup=False,
            style="yellow",
        )


if __name__ == "__main__":
    app()
