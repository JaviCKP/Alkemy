# SynthDB — Plan de ejecución del MVP

*Complemento operativo de la especificación técnica v0.1 · 15 de julio de 2026*

Este documento convierte las fases 0–4 de la especificación en un plan de trabajo ejecutable: tareas numeradas listas para convertirse en issues, criterios de aceptación medibles, puertas de decisión, plan de documentación y checklist de release. Supuesto de partida: **una persona a tiempo parcial (~10–12 h/semana)**; con más dedicación las semanas se comprimen, pero el orden y las puertas no cambian.

---

## 0. Definición de "MVP listo"

El MVP está terminado cuando todas estas afirmaciones son verificables por un tercero sin ayuda:

1. `pipx install synthdb` (desde PyPI) funciona en Linux/macOS con Python 3.11+.
2. Un usuario nuevo completa el *quickstart* del README (esquema inmobiliaria → `plan` → `populate` contra un PostgreSQL local) en menos de 10 minutos.
3. `synthdb populate` sobre los fixtures 1–5 termina con **0 violaciones de integridad** y produce `report.json`.
4. `synthdb generate --no-llm` funciona sin ningún modelo instalado (promesa local-first en su versión mínima).
5. Misma semilla + mismo plan ⇒ salida byte a byte idéntica (test automático en CI).
6. `ciclos.sql` (variante irrompible) y `opaco.sql` producen los diagnósticos y avisos esperados, no errores crípticos ni datos inventados con confianza alta.
7. La documentación listada en §7 existe, está enlazada desde el README y refleja el comportamiento real.
8. CI verde en `main`: lint, tipos, tests unitarios e integración con PostgreSQL efímero.

Todo lo que no contribuye a estas ocho líneas queda fuera del MVP (ya está delimitado en la especificación, §18).

### Calendario orientativo

| Semanas | Hito | Puerta de salida |
|---|---|---|
| 0 | Preparación del repositorio | CI verde con test trivial |
| 1–2 | **H0** Experimento LLM | Decisión Go/No-Go documentada |
| 3–5 | **H1** Núcleo estructural | `analyze` correcto sobre los 7 fixtures |
| 5–8 | **H2** Generación determinista | `generate --no-llm` reproducible sobre fixtures 1, 3, 5 |
| 8–10 | **H3** Capa semántica LLM | `plan` completo, cacheado y auditado |
| 10–12 | **H4** E2E + inserción + validación | `populate` con 0 violaciones en testcontainers |
| 12 | Release 0.1.0 | Checklist de §8 completa |

Los hitos H1→H2→H3→H4 son estrictamente secuenciales en sus entregas, pero la documentación (§7) y los fixtures se trabajan en paralelo desde el primer día. H2 solapa con H1 (semana 5) porque los generadores no dependen del parser: dependen solo de la IR, que se congela al final de la semana 4.

### Convenciones del plan

- Cada tarea tiene ID (`T<hito>.<n>`), tamaño orientativo (**S** ≤ 2 h, **M** ≤ 6 h, **L** ≤ 12 h) y criterio de aceptación (CA). Un ID = un issue de GitHub; los hitos = *milestones*.
- Regla de trabajo transversal: **ninguna tarea se cierra sin sus tests y sin docstring/documentación asociada**. Es lo que hace que "documentado" no sea una fase final maratoniana sino un residuo del trabajo diario.
- Decisiones de diseño no triviales → ADR corto en `docs/adr/` (plantilla en §7). Quince minutos por decisión evitan re-litigarlas en el futuro.

---

## 1. Semana 0 — Preparación del repositorio

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T0.1 | Crear repo público, licencia (MIT o Apache-2.0; decidir y registrar como ADR-001), README esqueleto con la promesa del proyecto, estado "en desarrollo" y enlace a la especificación | S | Repo público con licencia y README |
| T0.2 | `pyproject.toml` (hatchling): deps núcleo `sqlglot`, `pydantic>=2`, `typer`, `rich`, `faker`, `networkx`, `ruamel.yaml`, `httpx`; extras `[db]` (`sqlalchemy>=2`, `psycopg[binary]`), `[dev]` (`pytest`, `pytest-cov`, `mypy`, `ruff`, `pre-commit`, `testcontainers`, `syrupy`) | S | `uv sync` / `pip install -e .[dev]` limpio |
| T0.3 | Tooling: `uv` como gestor, `ruff` (lint+format), `mypy` estricto sobre `src/`, `pre-commit` con ambos | S | `pre-commit run --all-files` verde |
| T0.4 | CI (GitHub Actions): matriz 3.11/3.12 con lint+mypy+pytest; job `integration` con servicio PostgreSQL; job `llm` de disparo manual (`workflow_dispatch`) | M | CI verde en `main` con un test trivial |
| T0.5 | Crear el árbol de `src/synthdb/` de la especificación (§12) con módulos vacíos tipados | S | `import synthdb` funciona; mypy verde |
| T0.6 | **Escribir los 7 esquemas fixture** (`tests/schemas/`): inmobiliaria, cementerio, taller, ecommerce, rrhh_autoref (2 variantes), ciclos (3 variantes), opaco. Cada uno con un comentario de cabecera que declare qué riesgo cubre | M | Los 7 archivos cargan en un PostgreSQL real (`psql -f`) sin errores |
| T0.7 | GitHub: milestones M0–M4 + Release, labels (`hito:*`, `tipo:*`, `tam:*`), plantilla de issue con campo "esquema mínimo reproducible" | S | Issues del H0 creadas y asignadas al milestone |

Los fixtures se escriben ya (T0.6) porque son la brújula del proyecto entero: definen qué significa "funciona" en cada hito, alimentan el experimento del H0 y acabarán siendo los ejemplos de la documentación.

---

## 2. Hito 0 — Experimento de validación LLM (semanas 1–2)

Objetivo: responder la pregunta falsable de la especificación (§18) antes de construir nada alrededor: *¿puede un modelo local ≤ 8B, forzado por JSON Schema, clasificar columnas y proponer generadores con ≥ 80 % de exactitud sobre esquemas realistas?* Todo vive en `experiments/00_llm_plan/`, fuera de `src/` — es código de usar y tirar cuyos **resultados** (no el código) se conservan.

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| TH0.1 | Extractor de IR mínima: script con sqlglot que produce, por fixture, el JSON de tablas/columnas/tipos/restricciones/comentarios que verá el prompt (no hace falta la IR completa todavía) | M | JSON por fixture revisado a mano y correcto |
| TH0.2 | Contrato v0: subconjunto del JSON Schema de la especificación (§8) como modelos Pydantic; `model_json_schema()` exportado | S | Esquema validado con un ejemplo a mano |
| TH0.3 | Prompt v0 versionado (`prompts/v0.md`): rol, catálogo cerrado de generadores, instrucciones de duda→`warnings`, ejemplo mínimo | M | Revisado; sin ambigüedad sobre el formato de salida |
| TH0.4 | Runner: fixtures × modelos (`qwen2.5:7b-instruct`, `llama3.1:8b`, un tercero pequeño tipo 3–4B como control de suelo) × 3 repeticiones, temperatura 0, `format=` schema; guarda respuesta cruda + resultado de validación + latencia | M | Matriz completa ejecutada; respuestas archivadas en `runs/` |
| TH0.5 | **Etiquetado a mano**: `labels/<fixture>.yaml` con, por columna, rol esperado y conjunto de generadores aceptables (más de uno puede ser correcto) | L | Los 7 fixtures etiquetados; segundo repaso en día distinto |
| TH0.6 | Métricas: % JSON válido a la primera, exactitud de rol, exactitud de generador (pertenencia al conjunto aceptable), calibración (¿la exactitud crece con la confianza declarada?), estabilidad entre repeticiones | M | `RESULTS.md` con tablas por modelo y por fixture |
| TH0.7 | Informe y decisión: `RESULTS.md` + ADR-002 con la decisión Go/No-Go y sus consecuencias | S | ADR aprobado (aunque el aprobador seas tú) |

**Puerta de decisión (fin de semana 2).** Con los umbrales de la especificación (≥ 95 % validez sintáctica, ≥ 80 % exactitud en fixtures 1–5 con algún modelo ≤ 8B):

- **Go** → el LLM es el cerebro semántico por defecto (`llm.enabled: true`); H3 se ejecuta como está planificado.
- **No-Go** → el LLM pasa a asistente opcional (`llm.enabled: false` por defecto); en H2 se amplía el presupuesto de heurísticas (T2.4 sube de M a L, añadiendo más patrones y un modo interactivo de confirmación en `plan`); H3 se mantiene pero se recorta a un solo proveedor. **El resto del plan no cambia** — esa insensibilidad es deliberada.

Resultado intermedio (validez alta, exactitud 60–80 %): Go condicionado, subiendo `min_confidence` por defecto a 0.8 y reforzando el mensaje de "revisa el plan" en la CLI.

---

## 3. Hito 1 — Núcleo estructural (semanas 3–5)

Objetivo: `schema.sql` → `SchemaSpec` → fases de generación, con diagnósticos honestos. Al cierre de este hito la IR queda **congelada** para el MVP (cambios posteriores = ADR obligatoria), porque H2 y H3 construyen encima.

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T1.1 | `ir/schema.py`: modelos Pydantic completos de la especificación (§5) con serialización JSON estable (claves ordenadas) | M | Round-trip JSON sin pérdida; mypy verde |
| T1.2 | `parsing/types.py`: catálogo canónico de tipos + mapeo desde PostgreSQL (incluye `serial`→integer autoincrement, `numeric(p,s)`, `varchar(n)`, `timestamptz`, enums) | M | Tabla de mapeo cubierta por tests parametrizados |
| T1.3 | `parsing/ddl.py`: `CREATE TABLE`/`ALTER TABLE ADD CONSTRAINT`/`CREATE TYPE ... AS ENUM`/`COMMENT ON` → `TableSpec`. Conserva texto y AST de `CHECK` y `DEFAULT`. Ignora con **aviso registrado** (nunca en silencio) lo no soportado: triggers, funciones, `GENERATED` (v0: se marca la columna y se excluye) | L | Snapshot de IR (syrupy/golden JSON) correcto para los 7 fixtures |
| T1.4 | `constraints/check_interp.py` v0: comparaciones, `BETWEEN`, `IN`, `AND`/`OR` sobre una columna → cotas (`bounds_derived`); multi-columna → predicado evaluable si entra en el subconjunto; resto → `ast_supported=false` | M | Casos de los fixtures + casos adversos (subconsulta, función) clasificados bien |
| T1.5 | `ir/hashing.py`: hash canónico del esquema (SHA-256 de la serialización ordenada, excluyendo campos volátiles) | S | Mismo DDL con espacios/orden distinto ⇒ mismo hash |
| T1.6 | `graph/dependency.py`: grafo, SCC y condensación (networkx), fases fusionando tablas independientes; detección de puentes (`kind=bridge`) y de 1:1 (`UNIQUE` sobre FK) | M | Fases esperadas en fixtures 1–5; puente del taller detectado |
| T1.7 | `graph/strategies.py` v0: autorreferencia por niveles (anulable, y NOT NULL con raíz auto-referida + aviso), ciclo rompible por FK anulable (Insert+Update), `UnbreakableCycle` con diagnóstico y sugerencias | M | Las 3 variantes de `ciclos.sql` y las 2 de `rrhh_autoref.sql` producen exactamente la estrategia/diagnóstico esperado |
| T1.8 | CLI `synthdb analyze`: resumen Rich de la IR (tablas, relaciones, fases, avisos de soporte) + `--json` | M | Salida revisada sobre los 7 fixtures; avisos de `opaco.sql` correctos |
| T1.9 | Documentación del hito: `docs/adr/003-ir-congelada.md`, docstrings de `ir/` y `parsing/`, sección "Qué DDL se soporta" en `docs/limitations.md` (embrión) | S | Enlazada desde README |

**Criterio de salida del hito**: `synthdb analyze` correcto y honesto sobre los 7 fixtures; cobertura de tests de `parsing/` y `graph/` ≥ 85 %; IR congelada por ADR.

---

## 4. Hito 2 — Generación determinista sin LLM (semanas 5–8)

Objetivo: el motor completo funcionando con `--no-llm`. Es el hito más grande y el corazón determinista del proyecto; al terminarlo existe una herramienta útil por sí sola aunque el LLM no existiera.

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T2.1 | `generation/seeding.py`: semillas jerárquicas (`seed_tabla = blake2b(seed_global, tabla)`, flujo por fila derivado del índice) | S | Test: independencia del tamaño de lote (mismo resultado con lotes de 10 y de 5 000) |
| T2.2 | `generation/generators/base.py`: interfaz `Generator.generate(ctx) -> value`, registro por nombre, parámetros validados con Pydantic | M | Registrar/resolver generadores por `GeneratorSpec`; error claro si faltan parámetros |
| T2.3 | Generadores del catálogo: `faker` (locale, `unique`), `numeric_range` (uniform/normal/lognormal/zipf con `random` estándar), `datetime_range`, `choice` (pesos), `sequence`, `uuid`, `template`, `fallback` por tipo | L | Test por generador: rango, tipo, reproducibilidad, respeto de `unique` |
| T2.4 | `semantic/heuristics.py`: diccionario de patrones es/en (≥ 40 patrones) combinado con tipo y restricciones → `(rol, GeneratorSpec, confianza)` | M/L* | Exactitud medida contra las etiquetas del H0: ≥ 60 % en fixtures 1–5 solo con heurísticas (*L si el H0 fue No-Go) |
| T2.5 | `config/models.py` + `loader.py`: YAML completo del MVP (seed, locale, rows, columns, fk, rules, refs, hierarchy, output) validado con Pydantic; errores con ruta de campo exacta | M | YAML del ejemplo de la especificación (§11) carga; errores legibles ante campos inválidos |
| T2.6 | `semantic/merge.py` v0: prioridad usuario > IR > heurísticas > fallback; intersección de cotas de la IR con parámetros propuestos; rechazo con error si el usuario contradice la IR | M | Tests de contradicción: usuario vs CHECK, heurística vs enum, cotas intersecadas |
| T2.7 | `generation/keystore.py`: append-only por tabla, tuplas para PK compuestas, muestreo por índice | S | 10⁶ claves en memoria sin degradación apreciable |
| T2.8 | `generation/fk.py`: `uniform`, `zipf(s)`, `unique_subset`, `quota(min,max)`, `null_ratio`; PK compuesta = tupla entera | M | Distribución empírica verificada por test estadístico grueso (χ² tolerante); quota exacta |
| T2.9 | `rules/dsl.py` + `rules/eval.py`: parseo de expresiones (reutilizando el parser de expresiones de sqlglot) a AST propio; intérprete con lista blanca (`parent()`, `ref()`, `date()`, `date_add()`, `years_between()`, `noise()`, `round()`, comparaciones, aritmética, booleanas); doble uso: cota (desigualdades simples) y aserción | L | Reglas del ejemplo trabajado (§16 de la especificación) parsean y evalúan; expresión fuera de la lista blanca ⇒ rechazo con aviso |
| T2.10 | `generation/context.py` + orden de columnas intra-fila: grafo de columnas por `depends_on`, topo-sort, ciclo ⇒ error de plan | M | Fila con cadena `a→b→c` se genera en orden; ciclo detectado en compilación |
| T2.11 | `generation/engine.py`: ejecución por fases y lotes; integración de KeyStore, fk, reglas, RowContext | L | `inmobiliaria` genera 10⁴ filas coherentes en memoria |
| T2.12 | Estrategias especiales en el motor: autorreferencia por niveles (consume el plan de T1.7) y puentes (`sample_pairs_without_replacement` + quota) | M | `rrhh_autoref` y `taller` generan sin colisiones de unicidad |
| T2.13 | `validation/structural.py`: tipos, NOT NULL, UNIQUE (sets), FK contra KeyStore, CHECKs interpretados, aserciones DSL; por lote | M | Inyección de valores corruptos ⇒ detectados con mensaje por columna |
| T2.14 | Emisores `csv_json.py` y `sql_file.py` (INSERTs por lotes, orden de fases, UPDATEs de ciclos al final); `--dry-run` (plan + 10 filas/tabla) | M | `seed.sql` generado carga en PostgreSQL real sin errores para fixtures 1, 3, 5 |
| T2.15 | CLI `generate`, `export`, `plan --no-llm`; salida Rich del plan con source/confianza/avisos | M | Quickstart sin LLM completo de punta a punta |
| T2.16 | Test de reproducibilidad en CI: misma semilla ⇒ hash SHA-256 idéntico de los CSV | S | Test binario verde; falla si alguien introduce no-determinismo |
| T2.17 | Documentación: `docs/cli.md` (comandos existentes), `docs/configuration.md` (referencia YAML v0), `docs/dsl.md` (gramática + lista blanca + ejemplos), ADRs de fusor y semillas | M | Cada opción del YAML documentada con ejemplo |

**Criterio de salida**: `generate --no-llm` reproducible sobre `inmobiliaria`, `taller` y `rrhh_autoref`; `opaco.sql` produce datos 100 % válidos con `semantic_coverage` baja y avisos; ≥ 10⁴ filas/s de generación pura en el benchmark preliminar.

---

## 5. Hito 3 — Capa semántica LLM (semanas 8–10)

Objetivo: el plan del LLM entra en el fusor con todas las barreras de contención, y `synthdb plan` se convierte en la pieza de revisión humana. Reutiliza directamente prompt, contrato y aprendizajes del H0.

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T3.1 | `llm/provider.py`: `Protocol` + jerarquía de errores (`ProviderError`, `SchemaViolation`, `Timeout`) + reintento acotado (2) ante JSON inválido | S | Contrato de errores testado con proveedor falso |
| T3.2 | `llm/ollama.py` (endpoint nativo, `format=` JSON Schema) y `llm/openai_compat.py` (`response_format`; sirve para vLLM/llama.cpp server/OpenAI) | M | Test de humo manual contra Ollama local; unit tests con `httpx` mockeado |
| T3.3 | `llm/contract.py`: contrato completo de la especificación (§8) en Pydantic, `additionalProperties=false`, export del schema | S | Fixtures de respuestas válidas/inválidas clasificadas correctamente |
| T3.4 | `llm/chunking.py`: vecindarios (tabla objetivo + resumen de padres directos); presupuesto de tokens configurable; fusión de respuestas parciales | M | Esquema ecommerce troceado y re-ensamblado sin pérdida ni duplicados |
| T3.5 | `llm/prompts.py`: prompt v1 (iterado desde H0), versionado; la versión participa en la clave de caché | M | Cambio de prompt ⇒ invalidación de caché comprobada |
| T3.6 | `semantic/cache.py`: clave = `schema_hash + provider.fingerprint + prompt_version`; almacenamiento en `~/.cache/synthdb/`; CLI `cache show|clear` | M | Segunda ejecución sin llamada de red (verificado con mock/contador) |
| T3.7 | Integración en el fusor: `min_confidence`, degradación a fallback con aviso, recorte por IR, registro de `source` | L | Tests: LLM propone fuera de enum ⇒ recortado; confianza 0.5 ⇒ fallback + aviso; sobre `opaco.sql`, ninguna columna sin evidencia supera el umbral |
| T3.8 | `synthdb plan` completo: tabla Rich por tabla/columna con generador, source, confianza, reglas y avisos; `--json`; `--min-confidence` como puerta de CI | M | Plan del ejemplo trabajado (§16) reproducido |
| T3.9 | Batería de respuestas grabadas (válidas, truncadas, campos extra, generador inexistente, expresión DSL que no compila) para validador+fusor; tests reales marcados `@pytest.mark.llm` en el job manual de CI | M | CI estándar no requiere modelo; job `llm` verde en máquina con Ollama |
| T3.10 | Documentación: `docs/llm.md` (proveedores, modelos recomendados, privacidad, caché, qué hacer ante planes malos), actualización de `configuration.md` | S | Sección de privacidad explícita: qué se envía y qué no |

**Criterio de salida**: plan completo y auditado para los 7 fixtures con al menos un modelo local; caché operativa; todas las barreras de contención (validación, recorte, umbral, fallback) cubiertas por tests sin necesidad de modelo.

---

## 6. Hito 4 — Inserción, validación E2E y cierre (semanas 10–12)

Objetivo: cerrar el ciclo `populate → validate → report.json` con la BD como juez final.

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T4.1 | `emit/database.py`: SQLAlchemy Core, transacción por lote (5 000), savepoints con bisección para aislar filas culpables, cuarentena en `quarantine/` con motivo | L | Inyección artificial de filas malas ⇒ solo ellas en cuarentena, el resto insertado |
| T4.2 | Lote piloto: ~100 filas/tabla dentro de transacción revertida (o SQLite efímero construido desde la IR cuando no hay BD destino) | M | CHECK opaco que rechaza >20 % del piloto ⇒ aborta con mensaje accionable |
| T4.3 | `validation/repair.py`: regeneración selectiva de columnas ofensoras, máx. 3 intentos, luego cuarentena | M | Fila con una columna inválida se repara sin regenerar la fila entera |
| T4.4 | `validation/semantic.py` + `report.py`: anti-joins de huérfanos, duplicados UNIQUE, re-evaluación de reglas, métricas de la especificación (§13), `report.json` + resumen Rich con `seed/schema_hash/plan_fingerprint` | M | `report.json` validado contra su propio esquema Pydantic |
| T4.5 | CLI `populate` (con `--dry-run`) y `validate` | M | Flujo completo del quickstart operativo |
| T4.6 | Integración testcontainers: fixtures 1–5 con 0 violaciones y 0 huérfanos; `ciclos.sql` anulable e irrompible; `opaco.sql` | L | Suite de integración verde en CI |
| T4.7 | Benchmark reproducible (`ecommerce`, 10⁵ filas): filas/s de generación y de inserción, con presupuesto de regresión en CI | S | Presupuesto definido y verde |
| T4.8 | Endurecimiento UX: mensajes de error revisados uno a uno (parseo, ciclo, contradicción usuario-IR, proveedor caído, BD inaccesible) con acción sugerida | M | Revisión manual de cada ruta de error provocándola |

**Criterio de salida**: Definición de "MVP listo" (§0) puntos 3–6 verificados en CI.

---

## 7. Plan de documentación

La documentación se produce en dos ritmos: la **continua** (sale de cada tarea, obligatoria para cerrarla) y la **de release** (semana 11–12, pulido final). Para el MVP: Markdown plano en el repo, sin generador de sitio (mkdocs-material queda para v1.0 — ADR corto para registrarlo).

**Continua (desde la semana 0):**

- Docstrings en todo `src/` (estilo Google, verificadas por ruff).
- `CHANGELOG.md` (formato Keep a Changelog) actualizado en cada PR.
- ADRs en `docs/adr/NNN-titulo.md` con plantilla mínima: contexto → decisión → consecuencias (≤ 1 página). Previstas: 001 licencia, 002 Go/No-Go LLM, 003 IR congelada, 004 prioridades del fusor, 005 semillas, 006 sin mkdocs en MVP.
- `docs/limitations.md`: crece con cada aviso nuevo del parser o del motor. Es la sección 15 de la especificación convertida en documento vivo — la honestidad sobre límites es parte del producto.

**De release (semanas 11–12):**

| Documento | Contenido | Tarea |
|---|---|---|
| `README.md` | Promesa en 3 líneas, badges (CI, PyPI, licencia), **quickstart de 10 minutos** con el fixture inmobiliaria (instalar → `analyze` → `plan` → `populate` → `validate`), tabla "qué hace / qué no hace", enlaces al resto | TD.1 (M) |
| `docs/cli.md` | Referencia de comandos con ejemplos de salida reales (copiadas de ejecución, no inventadas) | TD.2 (S, ya existe borrador de H2) |
| `docs/configuration.md` | Referencia completa del YAML, opción a opción, con valores por defecto extraídos de los modelos Pydantic | TD.3 (M) |
| `docs/dsl.md` | Gramática del mini-DSL, lista blanca de funciones, ejemplos por tipo de regla, errores comunes | TD.4 (S) |
| `docs/llm.md` | Proveedores, modelos probados con sus métricas del H0, privacidad, caché, resolución de problemas | TD.5 (S) |
| `docs/architecture.md` | Versión condensada de la especificación (diagrama + responsabilidades + flujo), enlazando la especificación completa | TD.6 (M) |
| `docs/limitations.md` | Consolidado final: DDL soportado, CHECKs, ciclos, nombres opacos, qué no promete el proyecto | TD.7 (S) |
| `examples/inmobiliaria/` | `schema.sql`, `config.yaml`, `Makefile` o script con los comandos, salida esperada (`plan.txt`, muestra de CSV, `report.json`) regenerable con `make example` | TD.8 (M) |
| `CONTRIBUTING.md` + `CODE_OF_CONDUCT.md` | Cómo montar el entorno, correr tests (incluido el job `llm`), añadir un generador, criterios de PR | TD.9 (S) |

Prueba de fuego de la documentación (TD.10, S): pedir a una persona ajena (o hacerlo uno mismo en una máquina limpia) que ejecute el quickstart cronometrado. Si supera 10 minutos o tropieza, el problema es del README, no del usuario.

---

## 8. Release 0.1.0 (semana 12)

Checklist secuencial:

1. Rama `release/0.1.0`; versión fijada (SemVer; 0.x = API inestable, declararlo en README).
2. `CHANGELOG.md` consolidado para 0.1.0.
3. Empaquetado verificado: `uv build` → instalar la wheel en un venv limpio → quickstart completo desde la wheel (no desde el repo).
4. Publicación en **TestPyPI** → `pipx install --index-url ...` → quickstart de nuevo.
5. Publicación en PyPI (con *trusted publishing* de GitHub Actions, no tokens manuales).
6. Tag `v0.1.0` + GitHub Release con notas (qué hace, qué no, enlace al quickstart y a `limitations.md`).
7. Sembrar 5–8 issues `good first issue` reales (patrones de heurísticas nuevos, proveedores Faker, dialecto SQLite...) — es lo que convierte un repo publicado en un proyecto open source.
8. Anuncio donde tenga sentido (Show HN, r/dataengineering, foros de Python/Postgres) con el ejemplo de la inmobiliaria como demo; recoger feedback en issues etiquetadas `feedback-0.1`.

---

## 9. Gestión de riesgos operativos durante la ejecución

- **Ir por detrás del calendario en H2** (el hito más probable de desviarse): recortar en este orden, sin tocar la Definición de Listo: generador `template` (S de recuperar después), estrategia `quota` (dejando `uniform`/`zipf`), benchmark con presupuesto (dejar solo la medición). No recortar nunca: reproducibilidad, validación pre-emisión, `--no-llm`.
- **El H0 da resultados mediocres con 7–8B**: aplicar la rama No-Go/condicionada de la §2; no gastar más de 2 días extra iterando prompts dentro del H0 (la iteración fina de prompts pertenece a T3.5, con el fusor ya protegiendo).
- **Un fixture revela un caso estructural no previsto** (p. ej. una forma de DDL frecuente no soportada): registrar en `limitations.md` + issue etiquetada `post-mvp`; solo bloquea el hito si afecta a fixtures 1–5.
- **Scope creep**: cualquier idea nueva va a `docs/backlog.md` con dos líneas; se revisa entre hitos, nunca en mitad de uno. La especificación (§18) ya define qué quedó fuera y por qué.
- **Bus factor / continuidad**: los ADRs y este plan son la memoria del proyecto; si se retoma tras semanas de pausa, la re-entrada es leer el milestone activo y el último ADR.

---

## 10. Resumen ejecutable

Si solo se leyera una sección: semana 0, montar repo+CI+fixtures (T0.1–T0.7). Semanas 1–2, ejecutar el experimento LLM y **decidir** (TH0.1–TH0.7 + ADR-002). Semanas 3–5, parser→IR→grafo hasta que `analyze` sea correcto y honesto sobre los 7 fixtures. Semanas 5–8, el motor determinista completo hasta que `generate --no-llm` sea reproducible byte a byte. Semanas 8–10, el plan LLM con sus cinco barreras y su caché hasta que `plan` sea auditable. Semanas 10–12, `populate` transaccional con la BD como juez, documentación de release y publicación 0.1.0 con el checklist de la §8. En cada paso, la tarea no está cerrada sin test y sin su documentación — así el MVP llega no solo funcionando, sino documentado por construcción.
