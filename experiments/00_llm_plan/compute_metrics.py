"""TH0.6 - Metricas del experimento del Hito 0.

Lee runs/*.json (salida del runner) y labels/*.yaml (TH0.5), y calcula:
validez JSON/schema, exactitud de rol (heuristica de solapamiento de
palabras clave - ver nota mas abajo), exactitud de generador (pertenencia
al conjunto aceptable), calibracion de confianza en columnas ambiguas, y
estabilidad entre repeticiones. Escribe RESULTS.md.

Nota sobre "exactitud de rol": no hay forma barata de comparar strings
libres tipo "identificador del empleado" contra "identificador" con
igualdad exacta. Se usa solapamiento de palabras clave (excluyendo
stopwords) entre el `role` esperado y el `semantic_role` de la respuesta.
Es una aproximacion, no una metrica formal - documentado tambien en
RESULTS.md para que no se lea como mas precisa de lo que es.

Uso:
    uv run python experiments/00_llm_plan/compute_metrics.py
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

import yaml

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
LABELS_DIR = BASE_DIR / "labels"
RESULTS_PATH = BASE_DIR / "RESULTS.md"

STOPWORDS = {"de", "del", "la", "el", "en", "y", "o", "un", "una", "para", "que"}
LOW_CONFIDENCE_THRESHOLD = 0.6


def _keywords(text: str) -> set[str]:
    words = re.split(r"[^a-záéíóúñ0-9]+", text.lower())
    return {w for w in words if w and w not in STOPWORDS}


def load_labels() -> dict[str, dict]:
    labels = {}
    for path in sorted(LABELS_DIR.glob("*.yaml")):
        labels[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return labels


def load_runs() -> list[dict]:
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        if path.name == "_summary.json":
            continue
        runs.append(json.loads(path.read_text(encoding="utf-8")))
    return runs


def column_label(labels: dict, fixture: str, table: str, column: str) -> dict | None:
    fx = labels.get(fixture)
    if not fx:
        return None
    tbl = fx.get("tables", {}).get(table)
    if not tbl:
        return None
    return tbl.get("columns", {}).get(column)


def evaluate_response(run: dict, labels: dict) -> dict | None:
    """Compara una respuesta valida contra las labels. None si no aplica."""
    if not run["schema_valid"]:
        return None
    try:
        parsed = json.loads(run["raw_content"])
    except json.JSONDecodeError:
        return None

    fixture = run["fixture"]
    col_results = []
    for table in parsed.get("tables", []):
        tname = table.get("table_name")
        for col in table.get("columns", []):
            cname = col.get("column_name")
            label = column_label(labels, fixture, tname, cname)
            if label is None:
                continue

            gen_type = (col.get("generator") or {}).get("type")
            gen_ok = gen_type in label.get("acceptable_generators", [])

            expected_kw = _keywords(label.get("role", ""))
            got_kw = _keywords(col.get("semantic_role", ""))
            role_ok = bool(expected_kw & got_kw) if label.get("role") != "desconocido" else None

            confidence = col.get("confidence")
            low_conf_expected = label.get("low_confidence_expected", False)
            calibration_ok = None
            if low_conf_expected and confidence is not None:
                calibration_ok = confidence < LOW_CONFIDENCE_THRESHOLD

            col_results.append(
                {
                    "table": tname,
                    "column": cname,
                    "generator_ok": gen_ok,
                    "role_ok": role_ok,
                    "confidence": confidence,
                    "calibration_ok": calibration_ok,
                }
            )
    return {
        "fixture": fixture,
        "model": run["model"],
        "repetition": run["repetition"],
        "columns": col_results,
    }


def stability(runs: list[dict]) -> dict[tuple[str, str], float]:
    """Por (fixture, modelo): fracción de columnas donde las 3 repeticiones coinciden.

    Coinciden en (role, generator.type).
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        if not run["schema_valid"]:
            continue
        try:
            parsed = json.loads(run["raw_content"])
        except json.JSONDecodeError:
            continue
        groups[(run["fixture"], run["model"])].append(parsed)

    result = {}
    for key, parses in groups.items():
        if len(parses) < 2:
            continue
        per_col: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for parsed in parses:
            for table in parsed.get("tables", []):
                for col in table.get("columns", []):
                    k = (table.get("table_name"), col.get("column_name"))
                    gen = (col.get("generator") or {}).get("type")
                    per_col[k].append((col.get("semantic_role"), gen))
        if not per_col:
            continue
        stable = sum(1 for values in per_col.values() if len(set(values)) == 1)
        result[key] = stable / len(per_col)
    return result


def main() -> None:
    labels = load_labels()
    runs = load_runs()
    if not runs:
        raise SystemExit(f"No hay resultados en {RUNS_DIR}; ejecuta antes runner.py")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        by_model[run["model"]].append(run)

    stability_by_key = stability(runs)

    lines = ["# RESULTADOS — Hito 0: experimento de validación LLM", ""]
    lines.append(
        "Generado por `compute_metrics.py` a partir de `runs/*.json` (TH0.4) y "
        "`labels/*.yaml` (TH0.5, etiquetado por Claude — ver `labels/README.md` "
        "para la nota metodológica)."
    )
    lines.append("")
    lines.append(
        "**Nota sobre exactitud de rol**: se mide por solapamiento de palabras clave "
        "entre el rol esperado y el propuesto, no por igualdad exacta de texto. Es una "
        "aproximación barata, no una métrica formal."
    )
    lines.append("")
    lines.append("## Resumen por modelo")
    lines.append("")
    lines.append(
        "| Modelo | Llamadas | JSON válido | Schema válido | Exactitud rol | "
        "Exactitud generador | Calibración (baja confianza esperada) | Latencia media (s) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")

    for model, model_runs in by_model.items():
        n = len(model_runs)
        n_json = sum(1 for r in model_runs if r["json_valid"])
        n_schema = sum(1 for r in model_runs if r["schema_valid"])
        latencies = [r["latency_s"] for r in model_runs if r["latency_s"] is not None]

        role_hits, role_total = 0, 0
        gen_hits, gen_total = 0, 0
        cal_hits, cal_total = 0, 0
        for run in model_runs:
            evald = evaluate_response(run, labels)
            if not evald:
                continue
            for col in evald["columns"]:
                gen_total += 1
                gen_hits += int(col["generator_ok"])
                if col["role_ok"] is not None:
                    role_total += 1
                    role_hits += int(col["role_ok"])
                if col["calibration_ok"] is not None:
                    cal_total += 1
                    cal_hits += int(col["calibration_ok"])

        def pct(hits: int, total: int) -> str:
            return f"{100 * hits / total:.1f}% ({hits}/{total})" if total else "n/a"

        lat = f"{mean(latencies):.1f}" if latencies else "n/a"
        lines.append(
            f"| {model} | {n} | {pct(n_json, n)} | {pct(n_schema, n)} | "
            f"{pct(role_hits, role_total)} | {pct(gen_hits, gen_total)} | "
            f"{pct(cal_hits, cal_total)} | {lat} |"
        )

    lines.append("")
    lines.append("## Estabilidad entre repeticiones (temperatura 0)")
    lines.append("")
    lines.append("Fracción de columnas donde las 3 repeticiones coinciden en (rol, generador).")
    lines.append("")
    lines.append("| Fixture | Modelo | Estabilidad |")
    lines.append("|---|---|---|")
    for (fixture, model), frac in sorted(stability_by_key.items()):
        lines.append(f"| {fixture} | {model} | {100 * frac:.0f}% |")

    lines.append("")
    lines.append("## Detalle por fixture y modelo")
    lines.append("")
    lines.append(
        "| Fixture | Modelo | JSON válido | Schema válido | Exactitud rol | Exactitud generador |"
    )
    lines.append("|---|---|---|---|---|---|")
    by_fixture_model: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        by_fixture_model[(run["fixture"], run["model"])].append(run)
    for (fixture, model), fm_runs in sorted(by_fixture_model.items()):
        n = len(fm_runs)
        n_json = sum(1 for r in fm_runs if r["json_valid"])
        n_schema = sum(1 for r in fm_runs if r["schema_valid"])
        role_hits, role_total, gen_hits, gen_total = 0, 0, 0, 0
        for run in fm_runs:
            evald = evaluate_response(run, labels)
            if not evald:
                continue
            for col in evald["columns"]:
                gen_total += 1
                gen_hits += int(col["generator_ok"])
                if col["role_ok"] is not None:
                    role_total += 1
                    role_hits += int(col["role_ok"])

        def pct(hits: int, total: int) -> str:
            return f"{100 * hits / total:.0f}%" if total else "n/a"

        lines.append(
            f"| {fixture} | {model} | {n_json}/{n} | {n_schema}/{n} | "
            f"{pct(role_hits, role_total)} | {pct(gen_hits, gen_total)} |"
        )

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Escrito {RESULTS_PATH}")


if __name__ == "__main__":
    main()
