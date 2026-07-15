# CLAUDE.md — Convenciones de trabajo de SynthDB

Contexto permanente para agentes de código que trabajen en este repositorio. Léelo antes de tocar nada.

## Qué es este proyecto

Generador local-first de datos sintéticos semánticamente coherentes para bases de datos relacionales. Recibe un `schema.sql`, lo convierte en una representación intermedia canónica (IR), planifica el orden de generación respetando dependencias, asigna un generador a cada columna (heurísticas + plan LLM + configuración del usuario) y genera/inserta datos válidos y reproducibles.

Documentos de referencia, en orden de autoridad:

1. `docs/adr/` — decisiones que enmiendan a los demás documentos. **Un ADR posterior gana a la especificación.**
2. `especificacion.md` (raíz) — arquitectura, IR, algoritmos, contrato LLM (§8), validación.
3. `plan-ejecucion-mvp.md` (raíz) + `docs/adenda-campos-ia.md` — tareas, criterios de aceptación, hitos.

## Principios innegociables

- **La IR es la única fuente de verdad estructural.** Nada aguas abajo relee SQL ni reinterpreta el esquema. Tras el cierre del H1 la IR está congelada: cambiarla exige un ADR nuevo.
- **Las restricciones de la base de datos mandan siempre.** Ninguna inferencia (LLM, heurística) ni configuración de usuario puede producir valores que violen enums, CHECK, NOT NULL, UNIQUE o tipos. Las contradicciones se resuelven recortando o rechazando con error explícito, nunca relajando la restricción.
- **El LLM propone, el código decide.** El modelo solo produce planes validados por Pydantic y vocabularios/textos dentro de huecos ya validados. Jamás genera SQL que se ejecute, jamás elige claves, jamás decide estructura.
- **Prioridad del fusor** (especificación §7.1): usuario > IR > LLM (con confianza *efectiva*, ver ADR-002) > heurísticas > fallback seguro. Toda decisión lleva `source` y `confidence` trazables.
- **Nada falla ni se ignora en silencio.** DDL no soportado, inferencias dudosas, filas en cuarentena: todo se registra como aviso visible en plan/informe.
- **Determinismo y reproducibilidad.** Semillas jerárquicas (`generation/seeding.py`); prohibido `random` global, `datetime.now()` en rutas de generación, iteración sobre sets/dicts sin orden definido en nada que afecte a la salida. Misma semilla + mismo plan ⇒ mismos bytes (hay un test en CI que lo verifica; no lo rompas).
- **Prohibido `eval`/`exec`** sobre cualquier texto de usuario o de modelo. Las expresiones del mini-DSL pasan por `rules/dsl.py` (parser) y `rules/eval.py` (intérprete con lista blanca). Si necesitas una función nueva en el DSL, añádela a la lista blanca con tests, no abras la puerta.
- **Privacidad**: al LLM solo viajan esquema y metadatos. Nunca valores de datos salvo tras `--allow-data-sampling` explícito.

## Stack (fijado; no introducir dependencias nuevas sin ADR)

Python ≥ 3.11 · sqlglot (parser SQL, AST) · Pydantic v2 (IR, planes, contrato LLM) · Faker · networkx · SQLAlchemy 2 Core (nunca ORM) · Typer + Rich (CLI) · ruamel.yaml · httpx (proveedores LLM) · pytest + syrupy + testcontainers.

## Comandos

```bash
uv sync                                  # entorno
uv run pytest                            # tests estándar (sin modelo, sin Docker)
uv run pytest -m integration             # requiere Docker (testcontainers)
uv run pytest -m llm                     # requiere Ollama local con qwen2.5:7b-instruct
uv run mypy src/                         # estricto; debe quedar a cero
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
```

## Cómo se trabaja aquí

- **Una tarea (issue) por sesión.** El ID del plan (T1.3, T2.9…) define el alcance; no implementes tareas vecinas "ya que estás".
- **El criterio de aceptación del plan es la definición de hecho.** Cierra demostrándolo con la salida de los tests.
- **Tests primero** en parsers, intérpretes y fusor. Los snapshots de IR por fixture (syrupy, `tests/unit/parsing/__snapshots__/`) son la red de seguridad: si un cambio altera un snapshot, justifícalo en el PR; nunca regeneres snapshots en bloque para "poner verde".
- **Los fixtures de `tests/schemas/` son la definición operativa de "correcto".** Si descubres un caso que ningún fixture cubre, añade el caso mínimo a un fixture o crea uno nuevo — primero el fixture, después el código.
- Las 90 respuestas grabadas del H0 (`experiments/00_llm_plan/runs/`) son fixtures de regresión del validador del contrato y del fusor. No las edites; añade nuevas si hace falta.
- Docstrings estilo Google en todo `src/`; actualiza `CHANGELOG.md` (Keep a Changelog) en cada PR; decisiones de diseño no triviales ⇒ ADR corto en `docs/adr/` (contexto → decisión → consecuencias, ≤ 1 página).
- Mensajes de error orientados a acción: qué pasó, en qué tabla/columna, y qué puede hacer el usuario (YAML, flag, fixture mínimo para reportar).
- No toques `experiments/`: es código de usar y tirar cuyos resultados ya están consumidos por ADRs.

## Cosas que NO hacer

- No añadir microservicios, colas, hilos/async en el motor de generación, ni abstracciones "por si acaso". La solución más simple que pueda evolucionar.
- No prometer en docs compatibilidad de dialectos o CHECKs más allá de lo declarado en `docs/limitations.md`; ante un caso nuevo no soportado, la respuesta correcta es aviso + entrada en limitations, no soporte a medias silencioso.
- No usar la confianza declarada por el LLM como señal de decisión directa (ADR-002): siempre a través de la confianza efectiva de `semantic/merge.py`.
- No desactivar constraints de la BD por defecto bajo ninguna circunstancia (`--allow-ddl` existe, está desaconsejado y no es el camino para arreglar un test).
