"""Ruta común y determinista para comparar heurísticas y respuestas del H0.

Este módulo de soporte de tests lee los artefactos históricos sin modificarlos.
La población canónica se construye desde la intersección entre la IR real de
cada fixture y sus labels. Tanto las heurísticas como cada repetición/modelo H0
se evalúan sobre esas mismas claves; una predicción ausente o duplicada cuenta
como fallo, nunca desaparece del denominador.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from synthdb.constraints.check_interp import interpret_checks
from synthdb.ir.schema import ColumnSpec, TableSpec
from synthdb.parsing.ddl import parse_ddl
from synthdb.semantic.heuristics import infer_column

BASELINE_VERSION = "h3-r1-baseline/1"
FIXTURES = (
    "inmobiliaria",
    "cementerio",
    "taller",
    "ecommerce",
    "rrhh_autoref_nullable",
    "rrhh_autoref_notnull",
)
_STOPWORDS = {"de", "del", "la", "el", "en", "y", "o", "un", "una", "para", "que"}
_ROOT = Path(__file__).resolve().parents[4]
_SCHEMAS = _ROOT / "tests" / "schemas"
_LABELS = _ROOT / "experiments" / "00_llm_plan" / "labels"
_RUNS = _ROOT / "experiments" / "00_llm_plan" / "runs"


@dataclass(frozen=True)
class PopulationColumn:
    """Una columna exacta de la población de evaluación."""

    fixture: str
    table_name: str
    column_name: str
    table: TableSpec
    column: ColumnSpec
    label: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        """Identidad de la columna dentro de un fixture."""

        return self.table_name, self.column_name


def _keywords(text: str) -> set[str]:
    words = re.split(r"[^a-záéíóúñ0-9]+", text.lower())
    return {word for word in words if word and word not in _STOPWORDS}


def _load_population() -> dict[str, list[PopulationColumn]]:
    yaml = YAML(typ="safe")
    population: dict[str, list[PopulationColumn]] = {}
    for fixture in FIXTURES:
        labels: dict[str, Any] = yaml.load((_LABELS / f"{fixture}.yaml").read_text("utf-8"))
        spec = interpret_checks(parse_ddl((_SCHEMAS / f"{fixture}.sql").read_text("utf-8")))
        columns: list[PopulationColumn] = []
        for table in spec.tables:
            table_labels = labels.get("tables", {}).get(table.name, {}).get("columns", {})
            for column in table.columns:
                label = table_labels.get(column.name)
                if label is None:
                    continue
                columns.append(
                    PopulationColumn(
                        fixture=fixture,
                        table_name=table.name,
                        column_name=column.name,
                        table=table,
                        column=column,
                        label=label,
                    )
                )
        population[fixture] = columns
    return population


def _empty_metric() -> dict[str, int]:
    return {"hits": 0, "total": 0}


def _score(
    *,
    expected: dict[str, Any],
    proposed_role: str | None,
    proposed_generator: str | None,
    role_metric: dict[str, int],
    generator_metric: dict[str, int],
) -> None:
    generator_metric["total"] += 1
    generator_metric["hits"] += int(proposed_generator in expected.get("acceptable_generators", []))
    if expected.get("role") == "desconocido":
        return
    role_metric["total"] += 1
    role_metric["hits"] += int(
        bool(_keywords(expected.get("role", "")) & _keywords(proposed_role or ""))
    )


def _heuristic_metrics(
    population: dict[str, list[PopulationColumn]],
) -> dict[str, dict[str, int]]:
    role = _empty_metric()
    generator = _empty_metric()
    for fixture in FIXTURES:
        for item in population[fixture]:
            result = infer_column(item.table, item.column)
            _score(
                expected=item.label,
                proposed_role=result.role if result is not None else None,
                proposed_generator=(result.generator.type if result is not None else None),
                role_metric=role,
                generator_metric=generator,
            )
    return {"generator": generator, "role": role}


def _predictions(run: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    if not run.get("schema_valid"):
        return {}
    parsed: dict[str, Any] = json.loads(run["raw_content"])
    predictions: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: set[tuple[str, str]] = set()
    for table in parsed.get("tables", []):
        for column in table.get("columns", []):
            key = table.get("table_name"), column.get("column_name")
            if key in predictions:
                duplicates.add(key)
            else:
                predictions[key] = column
    for key in duplicates:
        predictions.pop(key, None)
    return predictions


def _h0_metrics(
    population: dict[str, list[PopulationColumn]],
) -> dict[str, dict[str, dict[str, int]]]:
    by_model: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {"generator": _empty_metric(), "role": _empty_metric()}
    )
    paths = sorted(_RUNS.glob("*.json"))
    for path in paths:
        if path.name == "_summary.json":
            continue
        run: dict[str, Any] = json.loads(path.read_text("utf-8"))
        fixture = run["fixture"]
        if fixture not in population:
            continue
        metrics = by_model[run["model"]]
        predictions = _predictions(run)
        for item in population[fixture]:
            prediction = predictions.get(item.key)
            generator = (
                (prediction.get("generator") or {}).get("type") if prediction is not None else None
            )
            role = prediction.get("semantic_role") if prediction is not None else None
            _score(
                expected=item.label,
                proposed_role=role,
                proposed_generator=generator,
                role_metric=metrics["role"],
                generator_metric=metrics["generator"],
            )
    return {model: by_model[model] for model in sorted(by_model)}


def build_baseline() -> dict[str, Any]:
    """Recalcula la baseline versionada sin escribir ningún artefacto histórico."""

    population = _load_population()
    columns_by_fixture = {fixture: len(population[fixture]) for fixture in FIXTURES}
    unique_columns = sum(columns_by_fixture.values())
    h0 = _h0_metrics(population)
    observations = {metrics["generator"]["total"] for metrics in h0.values()}
    if len(observations) != 1:
        raise AssertionError("los modelos H0 no se evaluaron sobre el mismo número de columnas.")
    return {
        "version": BASELINE_VERSION,
        "population": {
            "fixtures": list(FIXTURES),
            "columns_by_fixture": columns_by_fixture,
            "unique_columns": unique_columns,
            "h0_repetitions_per_model": 3,
            "observations_per_h0_model": observations.pop(),
        },
        "metrics": {
            "heuristic": _heuristic_metrics(population),
            "h0": h0,
        },
        "labels": {
            "status": "pending_human_second_review",
            "review_manifest": "labels_review_v1.yaml",
        },
    }
