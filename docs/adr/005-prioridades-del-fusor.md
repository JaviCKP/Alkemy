# ADR-005 — Prioridades del fusor: usuario > IR > LLM > heurística > fallback

- **Estado**: aceptada
- **Fecha**: 2026-07-21
- **Referencias**: especificación §7.1, `src/synthdb/semantic/merge.py` (T2.6),
  `src/synthdb/ir/plans.py` (`ColumnPlan.source`), CLAUDE.md (principio «las
  restricciones de la base de datos mandan siempre» y «el LLM propone, el código
  decide»), [ADR-002](002-resultado-hito-0.md) (confianza efectiva del LLM)

## Contexto

Cada columna del esquema recibe exactamente un generador. La decisión combina
varias fuentes que pueden discrepar: lo que el usuario fija en el YAML, lo que la
IR impone (enum, `CHECK`, `NOT NULL`, autoincrement, FK), lo que el LLM propondrá
(Hito 3) y lo que infieren las heurísticas de nombre/tipo. Sin un orden explícito
y trazable, dos fuentes en conflicto producirían resultados dependientes del
orden de evaluación, y —peor— una inferencia podría generar valores que la base
de datos rechaza.

El código ya implementa este orden (`semantic/merge.py::build_plan`, T2.6) y cada
`ColumnPlan` lleva `source` y `confidence`, pero la **regla de prioridad** no
estaba registrada en ningún ADR: vivía solo en la especificación §7.1 y en el
código. Al cerrar el Hito 2 conviene fijarla como decisión, porque el Hito 3
inserta el LLM en mitad de la cadena y debe respetarla.

## Decisión

El fusor resuelve cada columna aplicando las fuentes en este **orden de prioridad
decreciente**, y anota la ganadora en `ColumnPlan.source`:

1. **`user`** — la configuración del YAML (`columns.<c>.generator`, `fk`, `rules`).
   Manda sobre todo lo demás, **salvo** que contradiga una restricción de la IR
   (un generador que produciría valores fuera de un enum/`CHECK`, un `null_ratio`
   sobre una columna `NOT NULL`): esa contradicción se rechaza con `PlanError`,
   nunca se satisface relajando la restricción.
2. **`ir`** — la base de datos. Es una prioridad **inquebrantable**: enum/`CHECK`
   recortan las cotas de cualquier fuente; las columnas autoincrement y
   `GENERATED` se excluyen de la generación; las FK se resuelven contra el
   `KeyStore`. Ninguna inferencia puede violarla (CLAUDE.md).
3. **`llm`** — el plan del modelo, integrado en el Hito 3 con **confianza
   efectiva** ([ADR-002](002-resultado-hito-0.md)) y sujeto al recorte de la IR.
   Reservado en el H2 (`PlanSource` lo incluye; ningún camino lo produce todavía).
4. **`heuristic`** — patrones de nombre/tipo/restricción, aceptados solo si su
   confianza supera `llm.min_confidence`.
5. **`fallback`** — generador seguro por tipo, con confianza `0.0` y un **aviso
   visible**: no hay señal semántica, solo validez estructural.

La IR actúa además como **filtro sobre todas las fuentes**: las cotas de un
`CHECK` se intersecan con las del generador ganador venga de donde venga.

## Consecuencias

- **Trazabilidad total**: `source` y `confidence` explican *por qué* se eligió
  cada generador; `synthdb plan` los muestra por columna.
- **La base de datos nunca se viola por una inferencia**: la prioridad 2 es dura
  y las contradicciones del usuario se rechazan, no se acomodan.
- **El Hito 3 encaja sin reescribir el fusor**: el LLM entra como fuente entre
  `ir` y `heuristic`, aplicando `min_confidence` sobre la confianza *efectiva*.
- Cambiar este orden exige un ADR nuevo: es un contrato del que dependen los
  tests del fusor y la salida de `plan`.
