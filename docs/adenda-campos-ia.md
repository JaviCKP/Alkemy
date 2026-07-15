# Adenda A — Campos de contenido generado por IA

*Extensión de la especificación v0.1 y del plan de ejecución del MVP · 15 de julio de 2026*

## A.1 Veredicto: ¿cambia mucho el proyecto?

**Conceptualmente, no. Operativamente, sí en tres puntos concretos.**

No cambia porque el diseño ya contenía las tres piezas que este requisito necesita: (1) la clasificación campo a campo ya existe — es exactamente lo que hace el `SemanticPlan`, que asigna a cada columna un `semantic_role` y un `GeneratorSpec`; añadir la dimensión "determinista vs. IA" es añadir tipos al catálogo de generadores, no un subsistema nuevo; (2) el contexto de fila y relaciones ya existe — `RowContext` da acceso a los valores ya generados de la fila y a las filas padre (`ctx.parent("cliente_id")`), que es justo lo que necesita el prompt de una conversación; (3) la excepción "texto libre por LLM en volúmenes pequeños y como opción explícita" ya estaba declarada en el §1 de la especificación. Este requisito la convierte de excepción en característica de primera clase.

Sí cambia en:

1. **El perfil de coste pasa de O(1) a O(filas-con-IA) llamadas al modelo.** Hasta ahora el LLM se llamaba una vez por esquema (plan, cacheado). Ahora se llama además durante la generación, una vez por micro-lote de filas de las columnas marcadas como IA. Es el cambio más importante y hay que gobernarlo con presupuesto explícito (§A.6).
2. **La garantía de reproducibilidad se matiza** para esas columnas: "misma semilla ⇒ mismos bytes" se mantiene gracias a una caché de contenido, no a que el modelo sea determinista (§A.7).
3. **El plan del MVP crece ~2 semanas** con un hito nuevo tras la capa LLM (§A.9).

El principio central **no se toca**: el LLM sigue sin decidir estructura, sin elegir claves, sin generar SQL y sin tocar campos que Faker o una distribución resuelven mejor. Solo redacta texto dentro de huecos cuya validez estructural ya está garantizada por el motor determinista. La frontera se desplaza una casilla, no se disuelve.

## A.2 Clasificación en dos modos de generación

Cada columna del plan lleva ahora un `generation_mode`:

- **`deterministic`** (por defecto): IDs, FKs, nombres, emails, teléfonos, fechas, direcciones, importes, categóricos, y cualquier texto corto con estructura (referencias, matrículas). Generadores: los del catálogo actual (Faker, `numeric_range`, `choice`, `datetime_range`, `derived`, `text_pool`...).
- **`ai`**: texto libre que requiere redacción natural y contexto — conversaciones, resúmenes, observaciones, descripciones extensas, notas comerciales. Generadores nuevos: `llm_text` y `llm_group`.

**Cómo se decide el modo** (misma cadena de prioridad de siempre; nada nuevo que aprender):

1. Usuario (YAML): `mode: ai` o `mode: deterministic` por columna, inapelable.
2. IR: una columna con enum, CHECK de valores, o tipo no textual **nunca** puede ser `ai`.
3. LLM del plan: propone `llm_text`/`llm_group` como propone cualquier otro generador, con confianza.
4. Heurísticas: tipo `text` sin restricciones de valores + nombre que casa con `observacion|nota|descripcion|resumen|comentario|conversacion|mensaje|detalle` + (si se conoce) longitud declarada generosa ⇒ candidato `ai` con confianza media.
5. Fallback ante duda: `deterministic` con `text_pool`/plantilla. La IA es opt-in por diseño: un campo mal clasificado como determinista produce texto pobre pero válido; el error inverso quema tokens y tiempo.

Con `--no-llm`, toda columna `ai` degrada automáticamente a `text_pool`/plantilla con aviso en el plan y en el informe. La promesa local-first mínima (funcionar sin ningún modelo) se conserva intacta.

## A.3 Nuevos generadores

**`llm_text`** — una columna de texto por fila.

```yaml
observaciones:
  generator: llm_text
  params:
    instructions: "Nota interna breve del agente tras la visita, tono profesional, 1-3 frases."
    language: es
    max_chars: 400            # se interseca con varchar(n) de la IR si existe
    context: [parent(cliente_id).nombre, parent(vivienda_id).tipo,
              parent(vivienda_id).direccion, fecha, estado]
```

**`llm_group`** — varias columnas coherentes entre sí, generadas en **una sola llamada** por fila. Es la respuesta directa al caso conversación + resumen: la coherencia no se verifica a posteriori entre dos textos independientes, se obtiene por construcción al pedir ambos en la misma respuesta estructurada.

```yaml
tables:
  interacciones:
    content_groups:
      conversacion_y_resumen:
        columns: [transcripcion, resumen]
        instructions: >
          Conversación telefónica realista entre el cliente y el agente sobre la vivienda,
          coherente con el estado de la operación. El resumen debe tener 1-2 frases y ser
          fiel a la conversación.
        context: [parent(cliente_id).nombre, parent(agente_id).nombre,
                  parent(vivienda_id).*, fecha, estado_operacion]
```

Cuando conversación y resumen viven en **tablas distintas** (p. ej. `resumenes.conversacion_id → conversaciones.id`), no hace falta mecanismo nuevo: el orden topológico garantiza que la conversación existe antes, y el generador del resumen la recibe vía `parent(conversacion_id).transcripcion` en su contexto. Grupo intra-tabla ⇒ una llamada; relación entre tablas ⇒ contexto del padre. Dos casos, cero excepciones al modelo de ejecución.

## A.4 Cambios en el contrato del plan (§8 de la especificación)

Mínimos y aditivos: el enum de `generator.type` incorpora `llm_text` y `llm_group`; se añade un campo opcional `content_group` a nivel de columna (nombre del grupo) y un bloque `content_groups` a nivel de tabla (columnas, instrucciones sugeridas, contexto sugerido). El modelo del plan puede *proponer* instrucciones y contexto; el usuario las corrige en YAML con la prioridad habitual. Sigue vigente que el modelo no emite SQL ni código, y las expresiones de `context` usan el mismo mini-DSL ya definido (`parent()`, columnas de la fila), parseadas por el intérprete seguro.

## A.5 Cambios en el motor de generación

1. **Orden intra-fila**: las columnas `ai` declaran dependencia implícita de todas las columnas de su `context` ⇒ el grafo de columnas existente las ordena al final de la fila, con las FKs ya resueltas y los padres accesibles. Sin código nuevo de ordenación.
2. **Fase de contenido por micro-lotes**: dentro de cada lote de 5 000 filas, las filas con columnas `ai` se agrupan en micro-lotes (por defecto 8–16 filas por llamada) y se envían al proveedor con salida estructurada: un array JSON donde cada elemento contiene los campos del grupo para una fila. Concurrencia configurable (`ai.concurrency`, por defecto 2–4 peticiones en vuelo contra Ollama; más contra vLLM).
3. **Validación específica post-generación** (barata, por texto): longitud ≤ `varchar(n)` y ≤ `max_chars` (si se excede: reintento con instrucción de acortar, máx. 2; luego truncado limpio por frase + aviso); no vacío; idioma esperado (heurística ligera); y para grupos, chequeos estructurales del par (el resumen es más corto que la conversación). La emisión SQL ya era parametrizada, así que el texto libre no introduce riesgo de inyección en el emisor.
4. **Fallo del proveedor a mitad de generación**: el micro-lote fallido reintenta (acotado); si el proveedor cae, las filas quedan con la columna `ai` en cuarentena *parcial* — el resto de la fila es válido — y el comando puede reanudarse (`--resume`) completando solo el contenido pendiente gracias a la caché de contenido (§A.7).

## A.6 Coste y rendimiento: gobernarlos, no negarlos

Orden de magnitud honesto sin inventar cifras: un modelo local de 7–8B genera pocos cientos de tokens por segundo en hardware de consumo; una conversación + resumen son fácilmente 300–800 tokens de salida. Miles de filas con IA en un portátil se miden en horas, no en segundos. Por eso:

- `ai.max_rows_per_table` (techo duro; por defecto 1 000): superado el techo, el resto de filas usa el fallback `text_pool` y el informe lo declara. Alternativa explícita: `ai.coverage: 0.2` genera IA en una fracción de filas y fallback en el resto — útil cuando solo se necesita que "haya" contenido realista muestreable.
- El `--dry-run` y el lote piloto muestran ejemplos reales de contenido y una **estimación de llamadas totales** antes de comprometerse.
- Para volumen real, la recomendación operativa es el escalón vLLM/llama.cpp server ya previsto en el diseño de proveedores: mismo código, más throughput.
- Las tablas con columnas `ai` deberían ser, por defecto de configuración de ejemplo, las de menor cardinalidad del esquema (interacciones muestrales, no logs masivos).

## A.7 Reproducibilidad: caché de contenido

Nueva pieza pequeña pero necesaria: `ContentStore` (SQLite en `~/.cache/synthdb/content/` o junto al proyecto con `--content-cache ./`), clave = `(plan_fingerprint, tabla, grupo, semilla_de_fila)`, valor = los textos generados. Reglas:

- Re-ejecutar con la misma semilla y el mismo plan **reutiliza** el contenido ⇒ se mantiene "mismos bytes" sin depender del determinismo del modelo.
- Se envía `temperature=0` y `seed` al backend cuando lo soporta, pero la garantía contractual del proyecto se apoya en la caché, no en el backend — y así se documenta en `docs/llm.md`.
- `synthdb cache clear --content` regenera desde cero a voluntad.

## A.8 Evaluación de calidad del contenido

Al informe se añaden: `ai_rows_generated`, `ai_fallback_rows`, tasa de reintentos por longitud, y una muestra de K textos en `report.json` para revisión humana. La verificación automática de fidelidad conversación↔resumen (¿el resumen menciona lo pactado?) se hace con chequeos superficiales en MVP (longitud relativa, solapamiento léxico mínimo, aparición de las entidades del contexto que se pidieron explícitamente); el LLM-as-judge sobre una muestra queda como opción post-MVP, como ya preveía la especificación.

## A.9 Delta sobre el plan de ejecución del MVP

Se inserta un hito nuevo entre H3 y H4 (el motor y los proveedores ya existen en ese punto), y el calendario del MVP pasa de 12 a **~14 semanas**:

**Hito 3B — Generación de contenido IA (semanas 10–12):**

| ID | Tarea | Tam. | Criterio de aceptación |
|---|---|---|---|
| T3B.1 | Extensión de taxonomía y contrato: `generation_mode`, `llm_text`, `llm_group`, `content_groups` en IR de planes, contrato Pydantic y heurísticas de clasificación | M | Plan clasifica correctamente las columnas de un fixture nuevo |
| T3B.2 | Fixture nuevo `inmobiliaria_crm.sql`: añade `interacciones(transcripcion, resumen, observaciones)` con FKs a cliente/agente/vivienda — se convierte en el fixture 8 y en el ejemplo estrella de la documentación | S | Carga en PostgreSQL; etiquetado a mano del modo esperado por columna |
| T3B.3 | Generador `llm_text`: plantilla de prompt con contexto de `RowContext`, salida estructurada, validación de longitud/idioma, reintentos acotados | M | Textos válidos y coherentes con la fila en el fixture 8 |
| T3B.4 | Generador `llm_group` con micro-lotes y concurrencia | L | Conversación y resumen coherentes entre sí en una llamada; 16 filas/llamada funcionando |
| T3B.5 | `ContentStore` + `--resume` + `cache clear --content` | M | Re-ejecución con misma semilla ⇒ hash de salida idéntico sin llamadas de red |
| T3B.6 | Presupuestos: `max_rows_per_table`, `coverage`, estimación de llamadas en `--dry-run`, degradación a `text_pool` con `--no-llm` | M | Techo respetado y declarado en el informe |
| T3B.7 | Tests: respuestas grabadas para validador/reintentos/truncado; test real marcado `llm` con el fixture 8; documentación (`docs/ai-content.md` + actualización de `configuration.md` y `limitations.md`) | M | CI estándar sin modelo; job `llm` verde |

Ajustes menores en otros hitos: en H0 se añade una tarea S (medir con los mismos modelos la calidad subjetiva de 10 conversaciones+resúmenes con contexto dado — decide si el default de `llm_group` exige un modelo mayor); en H4 la validación posterior incorpora las métricas de A.8; el quickstart del README pasa a usar `inmobiliaria_crm` para lucir la característica.

**Qué NO entra en el MVP de esta característica** (v1.0+): verificación semántica profunda conversación↔resumen (LLM-as-judge), coherencia conversacional *entre* filas (hilos multi-interacción con memoria), estilos por persona/agente persistentes entre filas, y streaming de contenido a disco para tablas IA masivas.

## A.10 Conclusión

El requisito encaja en la arquitectura existente como una extensión del catálogo de generadores más una caché de contenido: ~2 semanas de plan, dos generadores, un fixture y un capítulo de documentación. Lo único que exige disciplina nueva no es técnico sino de producto: tratar el contenido IA como un recurso presupuestado (techos, cobertura, estimación previa) para que la característica más vistosa del proyecto no se convierta en su cuello de botella por defecto.
