"""TH0.1 — Extractor de IR minima: sqlglot -> JSON por fixture.

Codigo de usar y tirar para el experimento del Hito 0 (fuera de src/).
No es la IR completa de la especificacion (eso es T1.1); solo lo
suficiente para que el prompt del LLM vea tablas, columnas, tipos,
restricciones y comentarios reales de COMMENT ON.

Deliberadamente NO se propagan los comentarios de cabecera `--` de los
fixtures (el "Riesgo cubierto: ..."): eso filtraria la respuesta esperada
al modelo y invalidaria el experimento. Solo se extraen sentencias
COMMENT ON reales, que es la unica fuente de comentarios que tendria un
usuario real.

Uso:
    uv run python experiments/00_llm_plan/extract_ir.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "tests" / "schemas"
OUTPUT_DIR = Path(__file__).resolve().parent / "ir"


def _column_from_def(col_def: exp.ColumnDef) -> dict[str, Any]:
    column: dict[str, Any] = {
        "name": col_def.name,
        "type": col_def.kind.sql(dialect="postgres") if col_def.kind else None,
        "nullable": True,
        "primary_key": False,
        "unique": False,
        "default": None,
        "checks": [],
        "references": None,
        "comment": None,
    }
    for constraint in col_def.constraints or []:
        kind = constraint.kind
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            column["primary_key"] = True
            column["nullable"] = False
        elif isinstance(kind, exp.NotNullColumnConstraint):
            column["nullable"] = False
        elif isinstance(kind, exp.UniqueColumnConstraint):
            column["unique"] = True
        elif isinstance(kind, exp.CheckColumnConstraint):
            column["checks"].append(kind.this.sql(dialect="postgres"))
        elif isinstance(kind, exp.DefaultColumnConstraint):
            column["default"] = kind.this.sql(dialect="postgres")
        elif isinstance(kind, exp.Reference):
            ref = kind.this
            column["references"] = {
                "table": ref.this.name,
                "columns": [c.name for c in ref.expressions] or None,
            }
    return column


def _table_constraint(node: exp.Expression, table: dict[str, Any]) -> None:
    """Procesa una restriccion a nivel de tabla y la aplica sobre `table`."""
    if isinstance(node, exp.Constraint):
        # ALTER TABLE ... ADD CONSTRAINT <nombre> <restricción>: la
        # restricción real vive un nivel más adentro.
        for sub in node.expressions:
            _table_constraint(sub, table)
    elif isinstance(node, exp.PrimaryKey):
        table["primary_key"] = [c.name for c in node.expressions]
        names = set(table["primary_key"])
        for col in table["columns"]:
            if col["name"] in names:
                col["nullable"] = False
    elif isinstance(node, exp.UniqueColumnConstraint):
        target = node.this
        cols = [c.name for c in target.expressions] if target is not None else []
        if cols:
            table["unique_constraints"].append(cols)
    elif isinstance(node, exp.CheckColumnConstraint):
        table["checks"].append(node.this.sql(dialect="postgres"))
    elif isinstance(node, exp.ForeignKey):
        cols = [c.name for c in node.expressions]
        ref = node.args.get("reference")
        if ref is not None:
            ref_expr = ref.this
            ref_table = ref_expr.this.name
            ref_cols = [c.name for c in ref_expr.expressions] or None
            for col_name in cols:
                for col in table["columns"]:
                    if col["name"] == col_name:
                        col["references"] = {"table": ref_table, "columns": ref_cols}


def _new_table(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "comment": None,
        "columns": [],
        "primary_key": [],
        "unique_constraints": [],
        "checks": [],
    }


def extract(sql_text: str) -> dict[str, Any]:
    statements = sqlglot.parse(sql_text, read="postgres")
    tables: dict[str, dict[str, Any]] = {}
    table_order: list[str] = []

    for stmt in statements:
        if isinstance(stmt, exp.Create) and str(stmt.kind).upper() == "TABLE":
            name = stmt.this.this.name
            table = _new_table(name)
            for item in stmt.this.expressions:
                if isinstance(item, exp.ColumnDef):
                    table["columns"].append(_column_from_def(item))
                else:
                    _table_constraint(item, table)
            if not table["primary_key"]:
                pk_cols = [c["name"] for c in table["columns"] if c["primary_key"]]
                if pk_cols:
                    table["primary_key"] = pk_cols
            tables[name] = table
            table_order.append(name)

    for stmt in statements:
        if isinstance(stmt, exp.Alter):
            table = tables.get(stmt.this.name)
            if table is None:
                continue
            for action in stmt.args.get("actions", []):
                if isinstance(action, exp.ForeignKey):
                    _table_constraint(action, table)
                elif isinstance(action, exp.AddConstraint):
                    for sub in action.expressions:
                        _table_constraint(sub, table)

        elif isinstance(stmt, exp.Comment):
            kind = str(stmt.args.get("kind", "")).upper()
            this = stmt.this
            expression = stmt.args.get("expression")
            comment_text = expression.this if expression is not None else None
            if kind == "TABLE":
                table = tables.get(this.name)
                if table:
                    table["comment"] = comment_text
            elif kind == "COLUMN":
                table = tables.get(this.table)
                if table:
                    for c in table["columns"]:
                        if c["name"] == this.name:
                            c["comment"] = comment_text

    return {"dialect": "postgres", "tables": [tables[name] for name in table_order]}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = sorted(SCHEMAS_DIR.glob("*.sql"))
    if not fixtures:
        raise SystemExit(f"No se encontraron fixtures en {SCHEMAS_DIR}")

    for fixture in fixtures:
        sql_text = fixture.read_text(encoding="utf-8")
        ir = extract(sql_text)
        out_path = OUTPUT_DIR / f"{fixture.stem}.json"
        out_path.write_text(
            json.dumps(ir, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        n_tables = len(ir["tables"])
        n_cols = sum(len(t["columns"]) for t in ir["tables"])
        print(f"{fixture.name:30s} -> {n_tables} tablas, {n_cols} columnas")


if __name__ == "__main__":
    main()
