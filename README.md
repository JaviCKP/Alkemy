# SynthDB

> Generador local-first de datos sintéticos semánticamente coherentes para bases de datos relacionales.

**Estado: en desarrollo — sin release todavía.** SynthDB parte de tu `schema.sql` (PostgreSQL) y produce datos de prueba realistas y **estructuralmente válidos**: respeta claves primarias y foráneas, `CHECK`, `UNIQUE` y `NOT NULL`, resuelve ciclos y autorreferencias, y usa un modelo de lenguaje local (opcional) como *compilador semántico* que propone un plan auditable — nunca filas directamente. Funciona también con `--no-llm`, usando solo heurísticas deterministas.

No hay paquete publicado todavía; este repositorio sigue el [plan de ejecución del MVP](plan-ejecucion-mvp.md). El quickstart de instalación (`pipx install synthdb`) se documentará aquí en cuanto exista una release.

## Estado actual

Al cierre del **Hito 2**, SynthDB ya **genera datos deterministas sin LLM** y los
emite a CSV, JSON y `seed.sql` de PostgreSQL, además de la inspección estructural
del Hito 1. Cuatro comandos, todos sobre un `schema.sql` (ver [docs/cli.md](docs/cli.md)
y [docs/configuration.md](docs/configuration.md)):

```bash
uv run synthdb analyze  tests/schemas/inmobiliaria.sql
uv run synthdb plan     tests/schemas/inmobiliaria.sql -c config.yaml
uv run synthdb generate tests/schemas/inmobiliaria.sql -c config.yaml -o out/ --format csv
uv run synthdb export   tests/schemas/inmobiliaria.sql -c config.yaml --format sql -o seed.sql
```

- **`analyze`** parsea el esquema a la IR, interpreta los `CHECK`, planifica las
  fases y muestra columnas, claves, restricciones, fases y avisos, sin generar
  nada.
- **`plan`** muestra el plan de generación por columna (generador, fuente,
  confianza, reglas, avisos) y las fases.
- **`generate`** escribe un CSV o JSON por tabla, respetando el orden de columnas,
  con `NULL` como campo vacío y arrays serializados.
- **`export`** escribe un `seed.sql` cargable en PostgreSQL con `psql`: `INSERT`
  multi-fila por fases, `UPDATE` para cerrar ciclos y literales escalares
  construidos por sqlglot, que escapa el literal SQL exterior. Los arrays se
  codifican por dentro con el formato textual nativo de PostgreSQL (§8.15.2,
  `Array Value Input`) y ese texto completo vuelve a pasar por sqlglot; así el
  tipo real de la columna, también para `enum[]`, se resuelve al cargarlo. Si la
  cuarentena deja un hueco en una secuencia `SERIAL`, `export` lo rechaza
  (código 4) en vez de emitir un `seed.sql` con integridad referencial rota;
  `generate` CSV/JSON continúa con las filas aceptadas.

`generate` y `export` aceptan `--dry-run` (ejecutan el pipeline, muestran el plan
y 10 filas por tabla, y no escriben nada). Misma semilla ⇒ misma salida byte a
byte. Códigos de salida: `0` correcto, `1` sintaxis, `2` ciclo irrompible, `3`
archivo ilegible, `4` error de plan/configuración, `5` generación abortada.

Qué DDL se soporta, qué genera hoy el motor y qué queda fuera (arrays 0–5,
`sum_over_group` v1.0, reparación en el Hito 4…) está en
[docs/limitations.md](docs/limitations.md). La capa semántica con LLM y la
inserción transaccional en base de datos llegan en los Hitos 3 y 4.

## Documentación

- [Guía de la CLI](docs/cli.md) — `analyze`, `plan`, `generate` y `export`, con salidas reales.
- [Referencia de configuración](docs/configuration.md) — el `config.yaml`, opción por opción.
- [Mini-DSL de reglas](docs/dsl.md) — gramática de las reglas por fila.
- [Especificación técnica](especificacion.md) — arquitectura, IR, algoritmo de planificación, contrato con el LLM, riesgos y alcance.
- [Plan de ejecución del MVP](plan-ejecucion-mvp.md) — hitos, tareas, calendario y checklist de release.
- [Limitaciones conocidas](docs/limitations.md) — qué DDL y qué `CHECK` se soportan hoy, qué genera el motor y qué queda fuera.
- [ADRs](docs/adr/) — decisiones de diseño no triviales, una por archivo.

## Licencia

[Apache License 2.0](LICENSE) ([ADR-001](docs/adr/001-licencia.md)).
