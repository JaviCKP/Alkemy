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
