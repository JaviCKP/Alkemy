# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Este proyecto seguirá [SemVer](https://semver.org/lang/es/) a partir de la
primera release (mientras la versión sea 0.x, la API se considera inestable).

## [Unreleased]

### Added

- Preparación del repositorio (Semana 0 del plan de ejecución del MVP):
  licencia Apache-2.0 (ADR-001), `pyproject.toml` con dependencias núcleo y
  extras `[db]`/`[dev]`, tooling de `ruff`/`mypy`/`pre-commit`, workflow de
  CI, árbol de módulos de `src/synthdb/` y los 10 esquemas fixture en
  `tests/schemas/` (7 dominios, con variantes en `rrhh_autoref` y `ciclos`).
- Milestones, labels y plantillas de issue en GitHub para los 5 hitos y la
  release 0.1.0; issues del Hito 0 creadas.
- Hito 0 — experimento de validación LLM completo (`experiments/00_llm_plan/`):
  extractor de IR, contrato v0, prompt v0, runner (90 llamadas: 3 modelos ×
  10 fixtures × 3 repeticiones), etiquetado y métricas. **Decisión: Go**
  (ADR-002) — `qwen2.5:7b-instruct` como modelo por defecto. Hallazgo
  relevante para T3.7: la confianza autodeclarada por el modelo no está
  calibrada en columnas sin contexto (0% de calibración correcta sobre
  `opaco.sql`), hace falta una señal estructural adicional en el fusor.
