# ADR-002: Decisión Go/No-Go del cerebro semántico LLM

**Estado**: Aceptada
**Fecha**: 2026-07-15

## Contexto

El Hito 0 (plan de ejecución, §2) exigía responder, antes de construir
nada alrededor, la pregunta falsable de la especificación (§18): *¿puede
un modelo local ≤8B, forzado por JSON Schema, clasificar columnas y
proponer generadores con ≥80% de exactitud sobre esquemas realistas?*

Umbrales fijados de antemano en la especificación: ≥95% de validez
sintáctica, ≥80% de exactitud (rol/generador) en los fixtures 1–5 con
algún modelo ≤8B.

Se ejecutaron 90 llamadas (10 fixtures × 3 modelos × 3 repeticiones,
temperatura 0) contra `qwen2.5:7b-instruct`, `llama3.1:8b` y
`qwen2.5:3b-instruct` (control de suelo), con salida restringida al
contrato v0 (subconjunto Pydantic del JSON Schema de §8). Resultados
completos en [`RESULTS.md`](../../experiments/00_llm_plan/RESULTS.md).

**Limitación de método declarada**: el etiquetado "a mano" (TH0.5) lo
produjo Claude a partir del diseño de los fixtures, no una persona en un
segundo repaso como pide el plan literalmente — ver
`experiments/00_llm_plan/labels/README.md`. No afecta a la validez
sintáctica (100%, no depende de labels) ni a la calibración (0%, es un
hecho sobre `confidence` declarado). Sí introduce incertidumbre sobre las
cifras exactas de exactitud de rol/generador, aunque el margen sobre el
umbral es amplio salvo en `llama3.1:8b`.

## Decisión

**Go.** El LLM local pasa a ser el cerebro semántico por defecto
(`llm.enabled: true`), tal como estaba planificado. H3 se ejecuta según
lo previsto en el plan de ejecución.

Evidencia:

- Validez sintáctica: **100%** en los tres modelos (umbral ≥95%).
- Exactitud de generador en fixtures 1–5: **qwen2.5:3b-instruct 90.1%**,
  **qwen2.5:7b-instruct 88.2%**, llama3.1:8b 79.7% (umbral ≥80%; dos de
  los tres modelos lo superan con holgura).
- Modelo recomendado por defecto: **`qwen2.5:7b-instruct`** (mejor
  equilibrio exactitud/rol/estabilidad de los dos que superan el umbral;
  `qwen2.5:3b-instruct` queda como alternativa válida de menor coste,
  documentada en `docs/llm.md` cuando se escriba en T3.10).
- `llama3.1:8b` no se recomienda como modelo por defecto: por debajo del
  umbral de exactitud de generador pese a tener la exactitud de rol más
  alta — clasifica bien qué es cada columna pero elige peor el generador
  concreto dentro del catálogo cerrado.

## Consecuencias

1. **H3 se construye como está planificado**: proveedores Ollama +
   OpenAI-compatible, contrato completo, troceo, fusor con las cinco
   barreras, caché — sin recortar a un solo proveedor ni ampliar el
   presupuesto de heurísticas de T2.4 (la rama No-Go/condicionada de §2
   no aplica).

2. **La calibración de confianza NO es fiable tal cual viene del
   modelo** — hallazgo nuevo, no anticipado como umbral pero sí como
   riesgo en la especificación (§1, §7.6). En los 7 casos de
   `opaco.sql` sin ningún indicio semántico, los tres modelos declaran
   confianza entre 0.6 y 0.99 (0% de calibración correcta, ver
   `RESULTS.md` §3). Consecuencia obligatoria para T3.7 (integración en
   el fusor): **`min_confidence` no puede depender solo del `confidence`
   autodeclarado por el modelo**. Hace falta una señal estructural
   adicional — por ejemplo, recortar la confianza efectiva cuando la
   columna no tiene `comment` y su nombre no matchea ningún patrón de
   heurística (T2.4) — antes de comparar contra el umbral. Se añade
   como nota de diseño obligatoria a T3.7, no opcional.

3. **`qwen2.5:7b-instruct` como modelo por defecto** en `config.yaml` y
   en la documentación (T3.10, `docs/llm.md`); `llama3.1:8b` se
   documenta como alternativa no recomendada por defecto, no se retira
   del adaptador `OpenAICompatProvider`/Ollama (el catálogo de modelos
   soportados no se reduce, solo la recomendación por defecto).

4. **`docs/limitations.md`** (embrión, T1.9) hereda desde ya la nota de
   calibración del punto 2 — es exactamente el tipo de límite honesto
   que esa sección existe para documentar.

5. **El etiquetado de TH0.5 debe repasarlo una persona** antes de
   usarse como referencia de calidad más allá de esta decisión Go/No-Go
   (el margen sobre el umbral absorbe la incertidumbre del método, pero
   las cifras exactas de exactitud de rol no deberían citarse como
   definitivas sin ese repaso).
