# ADR-002 — Resultado del Hito 0: Go con confianza recalibrada

- **Estado**: aceptada
- **Fecha**: 2026-07-15
- **Referencias**: `experiments/00_llm_plan/RESULTS.md`, especificación §7.1 y §18, plan de ejecución §2

## Contexto

El experimento del Hito 0 (7 fixtures × 3 modelos locales × 3 repeticiones, temperatura 0, salida restringida por JSON Schema vía Ollama) arroja:

1. **Validez sintáctica: 100 %** (90/90 llamadas JSON válido y conforme al contrato, cero reintentos). Umbral ≥ 95 % superado sin ambigüedad; la decodificación restringida cumple.
2. **Exactitud de generador en fixtures 1–5** (umbral ≥ 80 % con algún modelo ≤ 8B): qwen2.5:3b-instruct **90,1 %**, qwen2.5:7b-instruct **88,2 %** — ambos superan el umbral con margen. llama3.1:8b se queda justo debajo (79,7 %) pese a la mejor exactitud de rol (98,7 %): entiende las columnas pero elige peor dentro del catálogo cerrado.
3. **Calibración de confianza: 0 % en los tres modelos.** Sobre las columnas deliberadamente opacas de `opaco.sql`, todos declaran confianza 0,6–0,99; ninguno baja del umbral cuando está adivinando a ciegas. La duda sí se verbaliza razonablemente en `warnings` ("sin comment explicativo", "no hay contexto"), pero no en `confidence`.
4. **Estabilidad** alta en general, con caídas concentradas exactamente donde hay incertidumbre real (taller/7b: 19 %; opaco/3b: 0 %). Operativamente cubierta por la caché de planes.
5. **Caveats de método**: exactitud de rol medida por solapamiento de palabras clave (sesgo hacia subestimar, confirmado por spot-check); las etiquetas de TH0.5 las generó Claude, no una persona con doble repaso.

## Decisión

**Go: el LLM es el cerebro semántico por defecto (`llm.enabled: true`), con una enmienda al fusor de §7.1: la confianza declarada por el modelo deja de ser la señal de decisión y pasa a ser un insumo más de una *confianza efectiva* calculada determinísticamente.**

La confianza efectiva por columna se define así (implementación en `semantic/merge.py`, H3):

```
conf_efectiva = min(
    conf_declarada,
    techo_por_evidencia(columna)
) - penalizaciones
```

donde:

- `techo_por_evidencia`: si la columna no casa con **ningún** patrón de las heurísticas, no tiene comentario, no tiene enum/CHECK con valores, y su nombre puntúa como opaco (longitud ≤ 3, patrón `c\d+|t\d+|cod_.|val|aux|tmp`), el techo es **0,5** (⇒ siempre por debajo del umbral ⇒ fallback seguro + aviso, que es exactamente el comportamiento que la especificación quería para `opaco.sql` y que la confianza declarada nunca habría producido). Con evidencia parcial (comentario o patrón parcial), techo 0,8. Con evidencia fuerte, sin techo.
- `penalizaciones`: la presencia de `warnings` del modelo que expresen falta de contexto resta un valor fijo (0,2). Los datos del H0 muestran que los `warnings` son la señal honesta que la confianza no es; esta regla la convierte en operativa.
- La coincidencia independiente heurística↔LLM (mismo rol/generador por dos vías) **suma** evidencia y puede elevar la confianza efectiva por encima de la declarada (máx. +0,1).

`min_confidence` se mantiene en 0,7 pero se aplica sobre la confianza efectiva.

**Modelo por defecto recomendado y documentado: `qwen2.5:7b-instruct`** (mejor equilibrio exactitud/estabilidad en los fixtures ambiguos). `qwen2.5:3b-instruct` se documenta como opción rápida válida, con la advertencia de su inestabilidad sobre esquemas opacos (irrelevante con caché, relevante al regenerar el plan). `llama3.1:8b` no se recomienda como default en esta versión de prompt.

## Consecuencias

1. **H3 / T3.7 se amplía** (de M a L): implementar la confianza efectiva con sus tres componentes, testada contra las respuestas grabadas del H0 (las 90 respuestas de `runs/` pasan a ser fixtures de regresión del fusor). Criterio de aceptación adicional: sobre `opaco.sql`, ninguna columna sin evidencia supera el umbral, con cualquiera de los tres modelos.
2. **H3 / T3.5 (prompt v1)**: incorporar dos aprendizajes: (a) instruir explícitamente el uso de confianzas bajas ante falta de evidencia — se intentará, pero el diseño ya no depende de que funcione; (b) hipótesis a probar para llama3.1: describir mejor cada generador del catálogo en el prompt (su fallo es de elección, no de comprensión). Si (b) lo sube del 80 %, se reevalúa como default en un mini-experimento de 1 día; si no, se archiva.
3. **Backlog (no bloqueante)**: revisión humana de `labels/*.yaml` antes de usarlas como referencia definitiva de calidad (S); sustituir la métrica de solapamiento de palabras clave por juicio semántico si las labels se promocionan a suite de regresión permanente.
4. **Pendiente del H0 ampliado (adenda A)**: la tarea de valorar calidad de conversaciones+resúmenes con estos modelos no consta en RESULTS.md; se ejecuta como tarea S al inicio del H3B, antes de fijar el modelo por defecto de `llm_group`.
5. **Documentación**: `docs/llm.md` incluirá la tabla de modelos con estas métricas y una sección explícita "por qué no confiamos en la confianza declarada", enlazando este ADR. `limitations.md` registra la calibración nula como limitación conocida de los modelos evaluados.
6. **Sin cambios** en H1, H2, la IR, el contrato JSON Schema (la confianza declarada se sigue pidiendo: es un insumo útil aunque no fiable) ni en el calendario global.
