# Fixtures contractuales y baseline H3-R1

`contract_cases.json` es la matriz de entradas válidas e inválidas del contrato
`semantic-proposal/1`. El test exige que el 100 % se clasifique según `valid`.

`baseline_v1.json` graba las métricas históricas de rol y generador sobre una
población única: 85 columnas de inmobiliaria, cementerio, taller, ecommerce y
las dos variantes de RR. HH. Cada modelo H0 se compara sobre 255 observaciones
(85 columnas × 3 repeticiones). Una columna ausente o duplicada en una respuesta
cuenta como fallo; no altera el denominador.

Esta población corrige dos asimetrías anteriores:

- el test heurístico solo incluía `rrhh_autoref_nullable`;
- `RESULTS.md` recorría las columnas devueltas por cada respuesta, lo que daba
  denominadores distintos (252, 255 o 315) pese a describir la misma población.

No se añaden métricas: siguen siendo exactitud de rol y de generador. Las labels
históricas se leen sin modificarlas y continúan marcadas como no definitivas.
`labels_review_v1.yaml` deja preparado el segundo repaso humano independiente,
con hashes de las seis fuentes y estado `pending_human_second_review`. El test
recalcula cada SHA-256 desde su `source`, por lo que CI detecta si el manifiesto
queda obsoleto.
