# Etiquetado a mano (TH0.5)

Ground truth independiente para medir la exactitud de `semantic_role` y
`generator.type` que proponen los modelos (TH0.6), y para el segundo
repaso exigido por el criterio de aceptación de TH0.5.

**Metodología y su límite honesto**: estas etiquetas las escribió Claude
(no un humano) a partir del diseño original de cada fixture — el mismo
código que construyó el runner y el pipeline del experimento — leyendo
únicamente `tests/schemas/*.sql`, **sin mirar** las respuestas de los
modelos en `runs/`, para no anclarse a lo que dijeron. Aun así, esto no
es lo que el plan pedía literalmente ("etiquetado a mano" por una
persona): es una aproximación más barata y con un riesgo de sesgo
correlacionado entre LLMs que un etiquetado humano no tendría. Debe
citarse así en `RESULTS.md` y en el ADR-002, no presentarse como
etiquetado humano. El "segundo repaso en día distinto" que pide TH0.5
sigue pendiente de que un humano lo revise antes de tomar la decisión
Go/No-Go como definitiva.

## Formato

```yaml
tables:
  <tabla>:
    columns:
      <columna>:
        role: <etiqueta corta esperada, o "desconocido" si no hay pistas>
        acceptable_generators: [<uno o más tipos del catálogo cerrado>]
        low_confidence_expected: true|false   # solo cuando aplica
        notes: "..."                           # opcional
```

`acceptable_generators` es un conjunto, no un único valor correcto: varias
familias de generador pueden ser razonables para la misma columna (p. ej.
`sequence` o `uuid` para un identificador). Las columnas FK/PK llevan
`[derived, sequence, uuid]` como conjunto laxo porque el contrato v0 no
tiene un tipo de generador dedicado a "es una FK, el motor la resuelve
estructuralmente" — es una limitación real del contrato v0 descubierta
durante el experimento, no un descuido del etiquetado; se registra en
`RESULTS.md`.

Para `opaco.sql` no existe una "respuesta correcta" secreta detrás de
`c1`, `c2`, `cod_x`... son nombres deliberadamente vacíos de contenido.
Lo correcto ahí no es acertar una etiqueta oculta sino declarar
`low_confidence_expected: true` y no inventar semántica específica.
