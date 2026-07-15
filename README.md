# SynthDB

> Generador local-first de datos sintéticos semánticamente coherentes para bases de datos relacionales.

**Estado: en desarrollo — sin release todavía.** SynthDB parte de tu `schema.sql` (PostgreSQL) y produce datos de prueba realistas y **estructuralmente válidos**: respeta claves primarias y foráneas, `CHECK`, `UNIQUE` y `NOT NULL`, resuelve ciclos y autorreferencias, y usa un modelo de lenguaje local (opcional) como *compilador semántico* que propone un plan auditable — nunca filas directamente. Funciona también con `--no-llm`, usando solo heurísticas deterministas.

No hay paquete publicado todavía; este repositorio sigue el [plan de ejecución del MVP](plan-ejecucion-mvp.md). El quickstart de instalación (`pipx install synthdb`) se documentará aquí en cuanto exista una release.

## Documentación

- [Especificación técnica](especificacion-synthdb.md) — arquitectura, IR, algoritmo de planificación, contrato con el LLM, riesgos y alcance.
- [Plan de ejecución del MVP](plan-ejecucion-mvp.md) — hitos, tareas, calendario y checklist de release.
- [ADRs](docs/adr/) — decisiones de diseño no triviales, una por archivo.

## Licencia

[Apache License 2.0](LICENSE) ([ADR-001](docs/adr/001-licencia.md)).
