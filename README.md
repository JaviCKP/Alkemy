# SynthDB

> Generador local-first de datos sintéticos semánticamente coherentes para bases de datos relacionales.

**Estado: en desarrollo — sin release todavía.** SynthDB parte de tu `schema.sql` (PostgreSQL) y produce datos de prueba realistas y **estructuralmente válidos**: respeta claves primarias y foráneas, `CHECK`, `UNIQUE` y `NOT NULL`, resuelve ciclos y autorreferencias, y usa un modelo de lenguaje local (opcional) como *compilador semántico* que propone un plan auditable — nunca filas directamente. Funciona también con `--no-llm`, usando solo heurísticas deterministas.

No hay paquete publicado todavía; este repositorio sigue el [plan de ejecución del MVP](plan-ejecucion-mvp.md). El quickstart de instalación (`pipx install synthdb`) se documentará aquí en cuanto exista una release.

## Estado actual

El núcleo estructural (Hito 1) ya funciona de punta a punta y se puede inspeccionar con la CLI. `synthdb analyze` parsea un esquema de PostgreSQL a la IR, interpreta los `CHECK` que caen en su subconjunto, planifica el orden de generación (fases, ciclos y autorreferencias) y lo presenta — **sin generar todavía ningún dato**:

```bash
uv run synthdb analyze tests/schemas/inmobiliaria.sql
```

Por cada tabla muestra sus columnas (tipo canónico, nulabilidad, default), claves y restricciones, y los `CHECK` interpretados con sus cotas; después, las fases de generación en orden y todos los avisos agrupados por origen. Con `--json` vuelca el mismo análisis en forma serializable y determinista, y `--dialect` fija el dialecto del parser (el MVP solo valida PostgreSQL).

Códigos de salida: `0` correcto (con o sin avisos), `1` error de sintaxis, `2` ciclo irrompible, `3` archivo inexistente o ilegible.

Qué DDL se soporta hoy, qué construcciones generan un aviso en vez de fallar y qué subconjunto de `CHECK` se interpreta está en [docs/limitations.md](docs/limitations.md). La generación e inserción de datos (motor, generadores y compilador semántico LLM) llega en hitos posteriores.

## Documentación

- [Especificación técnica](especificacion.md) — arquitectura, IR, algoritmo de planificación, contrato con el LLM, riesgos y alcance.
- [Plan de ejecución del MVP](plan-ejecucion-mvp.md) — hitos, tareas, calendario y checklist de release.
- [Limitaciones conocidas](docs/limitations.md) — qué DDL y qué `CHECK` se soportan hoy, y qué genera un aviso.
- [ADRs](docs/adr/) — decisiones de diseño no triviales, una por archivo.

## Licencia

[Apache License 2.0](LICENSE) ([ADR-001](docs/adr/001-licencia.md)).
