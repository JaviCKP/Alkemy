"""Punto de entrada de la CLI (Typer + Rich). Los subcomandos se añaden hito a hito.

El Hito 1 estrena `synthdb analyze RUTA.sql`, que ejecuta el pipeline
estructural completo — `parse_ddl` (T1.3) → `interpret_checks` (T1.4) →
`analyze_structure` (T1.6) → `resolve_cycles` (T1.7) — y lo presenta al
usuario sin generar todavía ni una fila: el resumen de cada tabla, las fases
de generación en orden y TODOS los avisos acumulados, agrupados por origen.
Con `--json` vuelca el mismo análisis en la serialización canónica de los
modelos de la IR, apto para consumo programático.

Códigos de salida (sin traceback nunca, CLAUDE.md): `0` análisis correcto
(con o sin avisos), `1` error de sintaxis SQL (`ParseError`), `2` ciclo
irrompible (`UnbreakableCycle`), `3` archivo inexistente o ilegible. Los
diagnósticos de error van a stderr como texto plano; la presentación y el
JSON, a stdout.
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

from synthdb.constraints.check_interp import interpret_checks
from synthdb.graph.dependency import analyze_structure
from synthdb.graph.strategies import UnbreakableCycle, resolve_cycles
from synthdb.ir.plans import InsertLeveledPhase, InsertPhase, Phase, UpdatePhase
from synthdb.ir.schema import CheckSpec, RelationshipSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.parsing.ddl import ParseError, parse_ddl

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


if __name__ == "__main__":
    app()
