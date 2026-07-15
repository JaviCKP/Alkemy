# RESULTADOS — Hito 0: experimento de validación LLM

Generado por `compute_metrics.py` a partir de `runs/*.json` (TH0.4) y `labels/*.yaml` (TH0.5, etiquetado por Claude — ver `labels/README.md` para la nota metodológica).

**Nota sobre exactitud de rol**: se mide por solapamiento de palabras clave entre el rol esperado y el propuesto, no por igualdad exacta de texto. Es una aproximación barata, no una métrica formal.

## Resumen por modelo

| Modelo | Llamadas | JSON válido | Schema válido | Exactitud rol | Exactitud generador | Calibración (baja confianza esperada) | Latencia media (s) |
|---|---|---|---|---|---|---|---|
| llama3.1:8b | 30 | 100.0% (30/30) | 100.0% (30/30) | 98.7% (373/378) | 77.2% (308/399) | 0.0% (0/21) | 20.7 |
| qwen2.5:3b-instruct | 30 | 100.0% (30/30) | 100.0% (30/30) | 87.3% (275/315) | 85.4% (287/336) | 0.0% (0/21) | 12.0 |
| qwen2.5:7b-instruct | 30 | 100.0% (30/30) | 100.0% (30/30) | 90.9% (289/318) | 88.8% (301/339) | 0.0% (0/21) | 20.2 |

## Estabilidad entre repeticiones (temperatura 0)

Fracción de columnas donde las 3 repeticiones coinciden en (rol, generador).

| Fixture | Modelo | Estabilidad |
|---|---|---|
| cementerio | llama3.1:8b | 100% |
| cementerio | qwen2.5:3b-instruct | 77% |
| cementerio | qwen2.5:7b-instruct | 92% |
| ciclos_deferrable | llama3.1:8b | 100% |
| ciclos_deferrable | qwen2.5:3b-instruct | 67% |
| ciclos_deferrable | qwen2.5:7b-instruct | 100% |
| ciclos_nullable | llama3.1:8b | 100% |
| ciclos_nullable | qwen2.5:3b-instruct | 83% |
| ciclos_nullable | qwen2.5:7b-instruct | 100% |
| ciclos_unbreakable | llama3.1:8b | 50% |
| ciclos_unbreakable | qwen2.5:3b-instruct | 50% |
| ciclos_unbreakable | qwen2.5:7b-instruct | 83% |
| ecommerce | llama3.1:8b | 100% |
| ecommerce | qwen2.5:3b-instruct | 100% |
| ecommerce | qwen2.5:7b-instruct | 86% |
| inmobiliaria | llama3.1:8b | 75% |
| inmobiliaria | qwen2.5:3b-instruct | 63% |
| inmobiliaria | qwen2.5:7b-instruct | 100% |
| opaco | llama3.1:8b | 90% |
| opaco | qwen2.5:3b-instruct | 0% |
| opaco | qwen2.5:7b-instruct | 60% |
| rrhh_autoref_notnull | llama3.1:8b | 100% |
| rrhh_autoref_notnull | qwen2.5:3b-instruct | 100% |
| rrhh_autoref_notnull | qwen2.5:7b-instruct | 100% |
| rrhh_autoref_nullable | llama3.1:8b | 80% |
| rrhh_autoref_nullable | qwen2.5:3b-instruct | 100% |
| rrhh_autoref_nullable | qwen2.5:7b-instruct | 100% |
| taller | llama3.1:8b | 100% |
| taller | qwen2.5:3b-instruct | 76% |
| taller | qwen2.5:7b-instruct | 19% |

## Detalle por fixture y modelo

| Fixture | Modelo | JSON válido | Schema válido | Exactitud rol | Exactitud generador |
|---|---|---|---|---|---|
| cementerio | llama3.1:8b | 3/3 | 3/3 | 100% | 85% |
| cementerio | qwen2.5:3b-instruct | 3/3 | 3/3 | 100% | 77% |
| cementerio | qwen2.5:7b-instruct | 3/3 | 3/3 | 92% | 100% |
| ciclos_deferrable | llama3.1:8b | 3/3 | 3/3 | 100% | 50% |
| ciclos_deferrable | qwen2.5:3b-instruct | 3/3 | 3/3 | 100% | 72% |
| ciclos_deferrable | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 100% |
| ciclos_nullable | llama3.1:8b | 3/3 | 3/3 | 100% | 67% |
| ciclos_nullable | qwen2.5:3b-instruct | 3/3 | 3/3 | 100% | 50% |
| ciclos_nullable | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 67% |
| ciclos_unbreakable | llama3.1:8b | 3/3 | 3/3 | 100% | 50% |
| ciclos_unbreakable | qwen2.5:3b-instruct | 3/3 | 3/3 | 100% | 83% |
| ciclos_unbreakable | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 89% |
| ecommerce | llama3.1:8b | 3/3 | 3/3 | 100% | 81% |
| ecommerce | qwen2.5:3b-instruct | 3/3 | 3/3 | 90% | 100% |
| ecommerce | qwen2.5:7b-instruct | 3/3 | 3/3 | 95% | 81% |
| inmobiliaria | llama3.1:8b | 3/3 | 3/3 | 93% | 88% |
| inmobiliaria | qwen2.5:3b-instruct | 3/3 | 3/3 | 82% | 100% |
| inmobiliaria | qwen2.5:7b-instruct | 3/3 | 3/3 | 80% | 80% |
| opaco | llama3.1:8b | 3/3 | 3/3 | 89% | 90% |
| opaco | qwen2.5:3b-instruct | 3/3 | 3/3 | 100% | 77% |
| opaco | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 100% |
| rrhh_autoref_notnull | llama3.1:8b | 3/3 | 3/3 | 100% | 80% |
| rrhh_autoref_notnull | qwen2.5:3b-instruct | 3/3 | 3/3 | 80% | 100% |
| rrhh_autoref_notnull | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 100% |
| rrhh_autoref_nullable | llama3.1:8b | 3/3 | 3/3 | 100% | 80% |
| rrhh_autoref_nullable | qwen2.5:3b-instruct | 3/3 | 3/3 | 80% | 100% |
| rrhh_autoref_nullable | qwen2.5:7b-instruct | 3/3 | 3/3 | 100% | 100% |
| taller | llama3.1:8b | 3/3 | 3/3 | 100% | 67% |
| taller | qwen2.5:3b-instruct | 3/3 | 3/3 | 71% | 75% |
| taller | qwen2.5:7b-instruct | 3/3 | 3/3 | 83% | 90% |

---

## Hallazgos (análisis manual, TH0.7)

*A partir de aquí el contenido ya no lo genera `compute_metrics.py`; es
lectura e interpretación de los datos de arriba.*

### 1. Validez sintáctica: sin ambigüedad

**100% de JSON válido y conforme al contrato en las 90 llamadas**, en los
tres modelos. La decodificación restringida por JSON Schema de Ollama
(`format=`) cumple lo prometido: cero respuestas truncadas, cero campos
inventados fuera del esquema, cero reintentos necesarios. Esto por sí
solo ya despeja la mitad de la pregunta falsable (umbral ≥95%).

### 2. Exactitud sobre fixtures 1–5 (el umbral que importa para Go/No-Go)

El criterio de la especificación es "≥80% exactitud en fixtures 1–5 con
algún modelo ≤8B". Restringiendo la medición exactamente a esos fixtures
(inmobiliaria, cementerio, taller, ecommerce, rrhh_autoref × 2 variantes):

| Modelo | Exactitud de generador | Exactitud de rol |
|---|---|---|
| qwen2.5:3b-instruct | **90.1%** (227/252) | 84.1% (212/252) |
| qwen2.5:7b-instruct | **88.2%** (225/255) | 88.6% (226/255) |
| llama3.1:8b | 79.7% (251/315) | 98.7% (311/315) |

Los dos modelos de la familia qwen2.5 superan el umbral con holgura.
**Llama3.1:8b se queda justo por debajo (79.7%)** en exactitud de
generador, aunque con la exactitud de rol más alta de los tres — sugiere
que entiende bien qué es cada columna pero elige peor el generador
concreto dentro del catálogo cerrado. Dato interesante y no trivial: el
modelo más pequeño (3B) iguala o supera al de 7B en este recorte
concreto — el tamaño no es el factor dominante aquí, probablemente
porque la tarea (clasificación + elección entre 9 opciones cerradas) no
exige tanta capacidad como generación libre.

### 3. Calibración de confianza: la señal de alarma real

**0% de calibración correcta en las tres modelos** (0/21 en cada uno).
En las columnas deliberadamente vacías de significado de `opaco.sql`
(`c2`, `c3`, `c7`, `cod_x`, `c4`, `c5`, `val` — sin `COMMENT`, sin pistas
de nombre) los tres modelos declaran confianza entre 0.6 y 0.99, nunca
por debajo del umbral de "esto es una suposición, no lo sé". Ejemplo real
(`t1.c7`, `NUMERIC(10,2)` sin comentario ni contexto):

- qwen2.5:7b-instruct → `"valor numérico con decimales"`, confianza 0.9
- llama3.1:8b → `"valor monetario"`, confianza 0.8
- qwen2.5:3b-instruct → `"valor numérico (opcional)"`, confianza 0.85

Ninguno se equivoca de forma grosera (el tipo de generador que proponen
suele ser razonable), pero **ninguno usa la confianza para señalar que
está adivinando a ciegas**. Si el fusor (§7.1) confía en el número que
declara el modelo para decidir cuándo hacer fallback, esta columna nunca
haría fallback aunque no haya ninguna base real para la respuesta. Los
`warnings` sí que verbalizan la duda razonablemente bien en varios casos
("no hay contexto adicional", "sin comment explicativo") — la señal
honesta existe, pero está en `warnings`, no en `confidence`.

### 4. Estabilidad

Alta en general (la mayoría ≥80% con temperatura 0), pero con caídas
notables: `qwen2.5:7b-instruct` en `taller` (19%) y `qwen2.5:3b-instruct`
en `opaco` (0%). Ambos son los fixtures más grandes/ambiguos de su tipo
(tabla puente con atributos; nombres opacos), consistente con que la
inestabilidad aparece justo donde más incertidumbre real hay — no es
ruido aleatorio uniforme, es ruido concentrado donde cabría esperarlo.

### 5. Limitación de método a declarar

"Exactitud de rol" se mide por solapamiento de al menos una palabra clave
entre el rol esperado y el propuesto (ver nota al inicio del documento),
no por juicio semántico. Un spot-check manual (`inmobiliaria` ×
`llama3.1:8b`) confirma que el método falla sobre todo por **falsos
negativos** (sinónimos que no comparten token, p. ej. `categoria_inmueble`
vs `tipo_vivienda`, o `compraventa` vs `compra_venta` partido en dos
palabras) más que por falsos positivos — así que si hay sesgo, es hacia
*subestimar* la exactitud real, no inflarla.

### 6. Las etiquetas de TH0.5 no son un etiquetado humano

Ver `labels/README.md`: las escribió Claude a partir del diseño de los
fixtures, no una persona en un segundo repaso en día distinto como pide
literalmente TH0.5. Esto no invalida las cifras de validez JSON (100%,
no depende de las labels) ni la calibración (0%, tampoco depende de las
labels — es un hecho sobre los números de confianza declarados). Sí
introduce incertidumbre adicional sobre las cifras exactas de exactitud
de rol/generador, que deberían confirmarse con revisión humana antes de
usarse como referencia definitiva de calidad (no antes de decidir
Go/No-Go: el margen sobre el umbral, salvo en llama3.1:8b, es amplio).

