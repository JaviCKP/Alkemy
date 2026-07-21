# Configuración (`config.yaml`)

Referencia completa del YAML de configuración del MVP. Los valores por defecto
están tomados directamente de los modelos Pydantic (`src/synthdb/config/models.py`);
si un valor no aparece en tu YAML, se aplica el que se indica aquí. Todos los
bloques rechazan **claves desconocidas** con un error que señala la ruta exacta
del campo (`extra="forbid"`).

La configuración del usuario es la **fuente de máxima prioridad** del fusor
(§7.1, [ADR-005](adr/005-prioridades-del-fusor.md)): manda sobre la IR salvo
cuando la contradice, en cuyo caso el fusor la rechaza con un `PlanError`
(nunca genera datos que violen el esquema).

## Ejemplo completo

```yaml
version: 1
seed: 42
locale: es_ES
dialect: postgres

llm:
  enabled: false
  provider: ollama
  model: qwen2.5:7b-instruct
  base_url: http://localhost:11434
  min_confidence: 0.7
  allow_data_sampling: false

defaults:
  rows: 100
  null_ratio: 0.0

tables:
  clientes:
    rows: 500
  viviendas:
    rows: 800
    columns:
      superficie_m2:
        generator: numeric_range
        params: {min: 35, max: 450}
      direccion:
        generator: faker
        params: {provider: street_address}
  compraventas:
    rows: 300
    fk:
      vivienda_id: {strategy: quota, min: 0, max: 2}
      comprador_id: {strategy: zipf, s: 1.3}
    rules:
      - "fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)"

refs:
  precio_m2_base: 2350

hierarchy:
  empleados.manager_id: {branching: 6, max_depth: 4}

output:
  batch_size: 5000
  on_error: quarantine
  max_repair_retries: 3
```

## Raíz

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `version` | int | `1` | Versión del formato de configuración. |
| `seed` | int | `0` | Semilla global de la generación. Misma semilla ⇒ mismos bytes ([ADR-006](adr/006-semillas-jerarquicas.md)). |
| `locale` | str | `es_ES` | Locale de Faker por defecto. |
| `dialect` | str | `postgres` | Dialecto SQL del esquema (el MVP solo promete PostgreSQL). |
| `llm` | mapa | ver abajo | Capa semántica del modelo (sin efecto hasta el Hito 3). |
| `defaults` | mapa | ver abajo | Valores por defecto de filas y `null_ratio`. |
| `tables` | mapa | `{}` | Configuración por tabla. |
| `refs` | mapa | `{}` | Constantes con nombre usables en reglas (`ref('nombre')`). |
| `hierarchy` | mapa | `{}` | Forma del árbol de las autorreferencias. |
| `output` | mapa | ver abajo | Lotes y política de errores. |

## `llm`

Se parsea entero, pero **no tiene efecto en el Hito 2** salvo `min_confidence`,
que el fusor ya usa como umbral de las heurísticas. El resto lo consume la capa
semántica del Hito 3 ([ADR-002](adr/002-resultado-hito-0.md)).

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Activa la capa LLM. Sin efecto hasta el Hito 3. |
| `provider` | `ollama` \| `openai_compat` \| `anthropic` | `ollama` | Proveedor del modelo. |
| `model` | str | `qwen2.5:7b-instruct` | Identificador del modelo. |
| `base_url` | str \| null | `null` | Endpoint del proveedor; `null` ⇒ el suyo por defecto. |
| `min_confidence` | float `[0,1]` | `0.7` | Umbral por debajo del cual una propuesta cae al `fallback` seguro con aviso. |
| `allow_data_sampling` | bool | `false` | Privacidad: sin esto, al modelo solo viajan esquema y metadatos, nunca valores. |

## `defaults`

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `rows` | int `> 0` | `100` | Filas por tabla si la tabla no fija `rows`. |
| `null_ratio` | float `[0,1]` | `0.0` | Proporción de `NULL` por defecto en columnas anulables. |

## `tables.<tabla>`

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `rows` | int `> 0` \| null | `null` ⇒ `defaults.rows` | Nº de filas de la tabla. |
| `columns` | mapa | `{}` | Configuración por columna (ver abajo). |
| `fk` | mapa | `{}` | Estrategia de selección por columna FK (ver abajo). |
| `rules` | lista de str | `[]` | Reglas del mini-DSL (ver [dsl.md](dsl.md)). |

### `tables.<tabla>.columns.<columna>`

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `generator` | str \| null | `null` | Id del generador (`faker`, `numeric_range`, `choice`, `datetime_range`, `template`, `sequence`, `uuid`…). Fuente `user`, confianza 1.0. |
| `params` | mapa | `{}` | Parámetros del generador; se validan al resolverlo. |
| `null_ratio` | float `[0,1]` \| null | `null` | Proporción de `NULL` de esta columna (solo si es anulable). |
| `unique` | bool \| null | `null` | Fuerza valores únicos; la unicidad de la IR se impone aunque sea `null`. |

### `tables.<tabla>.fk.<columna>`

Estrategia de selección de la clave foránea (§7.4). La clave es cualquier columna
de la FK; en una FK compuesta basta nombrar una y aplica a la relación entera
(dos columnas de la misma FK con estrategias distintas ⇒ `ConfigError`).

| `strategy` | Campos | Descripción |
|------------|--------|-------------|
| `uniform` | `null_ratio?` | Cualquier padre con igual probabilidad. |
| `zipf` | `s` (float `> 0`, def. `1.2`), `null_ratio?` | Sesgada: pocos padres concentran muchos hijos. |
| `unique_subset` | `null_ratio?` | 1:1, padres sin reemplazo. |
| `quota` | `min` (int `≥ 0`), `max` (int `≥ 0`), `null_ratio?` | Cada padre recibe entre `min` y `max` hijos (`min ≤ max`). |

`null_ratio` (float `[0,1]`, opcional) aplica solo si la FK es anulable.

## `refs`

Mapa de constantes con nombre (`{precio_m2_base: 2350}`) referenciables en las
reglas con `ref('nombre')`. Sin valores por defecto.

## `hierarchy.<tabla>.<columna>`

Forma del árbol de una autorreferencia (`empleados.manager_id`). Ambos campos son
obligatorios cuando se declara una entrada.

| Clave | Tipo | Descripción |
|-------|------|-------------|
| `branching` | int `> 0` | Hijos por nodo al repartir las filas en niveles. |
| `max_depth` | int `> 0` | Profundidad máxima del árbol. |

Sin una entrada para una autorreferencia, el motor usa `branching = 5` y una
profundidad suficiente para todas las filas.

## `output`

| Clave | Tipo | Defecto | Descripción |
|-------|------|---------|-------------|
| `batch_size` | int `> 0` | `5000` | Tamaño de lote de generación y de los `INSERT` multi-fila del `seed.sql`. |
| `on_error` | `quarantine` \| `abort` | `quarantine` | Filas inválidas: apartarlas en cuarentena (y continuar) o abortar con código 5. |
| `max_repair_retries` | int `≥ 0` | `3` | Reintentos de reparación antes de la cuarentena. Reservado para el Hito 4; sin efecto en el H2. |
