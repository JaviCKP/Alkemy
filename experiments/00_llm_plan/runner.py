"""TH0.4 - Runner: fixtures x modelos x repeticiones.

Llama a cada modelo local via Ollama para cada fixture, con salida
restringida al JSON Schema del contrato v0, temperatura 0, 3 repeticiones.
Guarda la respuesta cruda, el resultado de validacion contra el contrato y
la latencia. No reintenta ante JSON invalido a proposito: TH0.6 mide
"% JSON valido a la primera", y reintentar contaminaria esa metrica (los
reintentos acotados son una barrera de contencion de T3.1, no de este
experimento).

Reanudable: si ya existe el archivo de salida de una combinación
(fixture, modelo, repetición) se salta, así que se puede volver a
lanzar tras una interrupción sin perder ni repetir trabajo.

Uso:
    uv run python experiments/00_llm_plan/runner.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import SemanticPlanResponse  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
IR_DIR = BASE_DIR / "ir"
RUNS_DIR = BASE_DIR / "runs"
PROMPT_PATH = BASE_DIR / "prompts" / "v0.md"
OLLAMA_URL = "http://localhost:11434/api/chat"

MODELS = ["qwen2.5:7b-instruct", "llama3.1:8b", "qwen2.5:3b-instruct"]
REPETITIONS = 3
TIMEOUT_S = 300.0


def _safe(name: str) -> str:
    return name.replace(":", "_").replace("/", "_")


def call_model(model: str, system_prompt: str, ir: dict, schema: dict) -> tuple[dict, float]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(ir, ensure_ascii=False)},
        ],
        "format": schema,
        "options": {"temperature": 0, "seed": 0},
        "stream": False,
    }
    t0 = time.perf_counter()
    response = httpx.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_S)
    latency = time.perf_counter() - t0
    response.raise_for_status()
    return response.json(), latency


def run_one(
    fixture: str, model: str, repetition: int, system_prompt: str, ir: dict, schema: dict
) -> dict:
    out_name = f"{fixture}__{_safe(model)}__rep{repetition}.json"
    out_path = RUNS_DIR / out_name

    result = {
        "fixture": fixture,
        "model": model,
        "repetition": repetition,
        "timestamp": datetime.now(UTC).isoformat(),
        "latency_s": None,
        "json_valid": False,
        "schema_valid": False,
        "error": None,
        "raw_content": None,
    }

    try:
        data, latency = call_model(model, system_prompt, ir, schema)
        result["latency_s"] = round(latency, 3)
        content = data.get("message", {}).get("content", "")
        result["raw_content"] = content
        try:
            parsed = json.loads(content)
            result["json_valid"] = True
        except json.JSONDecodeError as exc:
            result["error"] = f"json_decode_error: {exc}"
            parsed = None

        if parsed is not None:
            try:
                SemanticPlanResponse.model_validate(parsed)
                result["schema_valid"] = True
            except ValidationError as exc:
                result["error"] = f"schema_validation_error: {exc.error_count()} errores"
    except httpx.HTTPError as exc:
        result["error"] = f"http_error: {exc}"

    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    schema = SemanticPlanResponse.model_json_schema()
    fixtures = sorted(p.stem for p in IR_DIR.glob("*.json"))

    if not fixtures:
        raise SystemExit(f"No hay IR extraida en {IR_DIR}; ejecuta antes extract_ir.py")

    total = len(fixtures) * len(MODELS) * REPETITIONS
    done = 0
    skipped = 0

    for model in MODELS:
        for fixture in fixtures:
            ir = json.loads((IR_DIR / f"{fixture}.json").read_text(encoding="utf-8"))
            for repetition in range(1, REPETITIONS + 1):
                done += 1
                out_path = RUNS_DIR / f"{fixture}__{_safe(model)}__rep{repetition}.json"
                if out_path.exists():
                    skipped += 1
                    continue
                print(
                    f"[{done}/{total}] {model} · {fixture} · rep{repetition}...",
                    end=" ",
                    flush=True,
                )
                result = run_one(fixture, model, repetition, system_prompt, ir, schema)
                status = "OK" if result["schema_valid"] else f"FALLO ({result['error']})"
                print(f"{status} ({result['latency_s']}s)")

    if skipped:
        print(f"({skipped} combinaciones ya existían de una ejecución anterior; omitidas)")

    keys = ("fixture", "model", "repetition", "latency_s", "json_valid", "schema_valid", "error")
    summary = []
    for result_path in sorted(RUNS_DIR.glob("*.json")):
        if result_path.name == "_summary.json":
            continue
        record = json.loads(result_path.read_text(encoding="utf-8"))
        summary.append({k: record[k] for k in keys})

    summary_path = RUNS_DIR / "_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    n_json_valid = sum(1 for s in summary if s["json_valid"])
    n_schema_valid = sum(1 for s in summary if s["schema_valid"])
    pct_json = 100 * n_json_valid / total
    pct_schema = 100 * n_schema_valid / total
    print()
    print(
        f"Total: {total} | JSON válido: {n_json_valid} ({pct_json:.1f}%) | "
        f"Schema válido: {n_schema_valid} ({pct_schema:.1f}%)"
    )
    print(f"Resumen: {summary_path}")


if __name__ == "__main__":
    main()
