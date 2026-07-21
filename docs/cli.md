# CLI de SynthDB

La CLI (`synthdb`, Typer + Rich) es la única interfaz del MVP. Al cierre del
Hito 2 expone cuatro comandos: `analyze` (Hito 1) y `plan`, `generate` y
`export` (Hito 2). Todos leen un `schema.sql` de PostgreSQL; `plan`/`generate`/
`export` aceptan además una configuración YAML (ver [configuration.md](configuration.md)).

Las salidas de este documento están **copiadas de ejecuciones reales** sobre
`tests/schemas/inmobiliaria.sql` con la configuración de ejemplo, a un ancho de
terminal de 100 columnas.

## Convenciones comunes

- **UTF-8 siempre**, también en Windows: la CLI reconfigura stdout/stderr y todos
  los archivos se escriben en UTF-8 (nunca `cp1252`).
- **Determinismo**: misma semilla + mismo esquema + misma config ⇒ mismos bytes.
  Las salidas `--json` ordenan sus claves; los CSV usan terminador `\n` fijo.
- **Sin tracebacks**: cualquier error se presenta como un mensaje accionable en
  stderr y un código de salida; nunca una traza de Python.

### Códigos de salida

| Código | Significado |
|--------|-------------|
| `0` | Correcto (con o sin avisos). |
| `1` | Error de sintaxis SQL (`ParseError`). |
| `2` | Ciclo irrompible entre tablas (`UnbreakableCycle`). |
| `3` | Archivo de esquema inexistente o ilegible. |
| `4` | Error de plan o de configuración (`PlanError` / `ConfigError`), con el mensaje completo. |
| `5` | Generación abortada por `output.on_error: abort` ante una fila inválida. |

Si al terminar `generate`/`export` la **cuarentena** no está vacía, se informa
siempre (tabla, nº de filas y primer motivo) sin cambiar el código de salida
(con `on_error: quarantine`, las filas inválidas se apartan y la ejecución es un
éxito).

## `analyze`

```bash
synthdb analyze RUTA.sql [--dialect postgres] [--json]
```

Inspección estructural de solo lectura: parsea el DDL a la IR, interpreta los
`CHECK`, planifica las fases y muestra columnas, claves, restricciones, fases y
avisos. No genera datos. Ver el [README](../README.md) y
[limitations.md](limitations.md) para el detalle del DDL soportado.

## `plan`

```bash
synthdb plan RUTA.sql [-c config.yaml] [--json] [--no-llm]
```

Muestra el **plan de generación por columna** —generador, fuente (`user`/`ir`/
`heuristic`/`fallback`), confianza, rol, reglas que la fijan y avisos— y las
**fases** de generación, sin generar ni escribir nada. `--no-llm` está declarado
por estabilidad de la interfaz, pero es un **no-op** en el Hito 2: aún no hay
capa LLM (llega en el Hito 3, [ADR-002](adr/002-resultado-hito-0.md)); el H2
usa solo heurísticas. `-c` es opcional: sin config se usan los valores por
defecto.

```text
─────────────────────────────────── Plan de generación · 4 tablas ───────────────────────────────────

───────────────────────────────────────── Tabla clientes ──────────────────────────────────────────
Columna    │ Generador      │ Fuente    │ Confianza │ Rol            │ Reglas │ Avisos
───────────┼────────────────┼───────────┼───────────┼────────────────┼────────┼────────────────────
id         │ — (BD)         │ ir        │ 1.00      │ identificador  │ —      │ autoincremental (…)
nombre     │ faker          │ heuristic │ 0.70      │ nombre_persona │ —      │ —
email      │ faker          │ heuristic │ 0.95      │ email          │ —      │ —
fecha_alta │ datetime_range │ heuristic │ 0.80      │ fecha_alta     │ —      │ —

─────────────────────────────────────── Tabla compraventas ────────────────────────────────────────
Columna      │ Generador      │ Fuente    │ Confianza │ Rol           │ Reglas │ Avisos
─────────────┼────────────────┼───────────┼───────────┼───────────────┼────────┼───────────────────
id           │ — (BD)         │ ir        │ 1.00      │ identificador │ —      │ autoincremental (…)
vivienda_id  │ fk[quota]      │ user      │ 1.00      │ fk            │ —      │ —
comprador_id │ fk[zipf]       │ user      │ 1.00      │ fk            │ —      │ —
fecha        │ datetime_range │ heuristic │ 0.70      │ fecha         │ —      │ —
precio       │ numeric_range  │ heuristic │ 0.75      │ precio        │ —      │ —

───────────────────────────────────── Fases de generación (4) ─────────────────────────────────────
  1. Insert         clientes
  2. Insert         viviendas
  3. Insert         compraventas
  4. Insert         pagos

───────────────────────────────────────────── Resumen ─────────────────────────────────────────────
  Columnas por fuente — fallback: 1, heuristic: 8, ir: 6, user: 5.
```

Una columna sin señal semántica cae al generador `fallback` con un aviso; en el
ejemplo, `pagos.num_plazo` (un entero de nombre opaco) es el único `fallback`.
Fija un generador en el YAML para darle dominio.

### `plan --json`

Salida determinista (claves ordenadas) apta para CI. Estructura: `tables` (un
`TablePlan` por tabla, con `columns` → generador/fuente/confianza/rol/avisos),
`phases`, `rules` (las reglas del YAML por tabla) y `warnings`.

```json
{
  "phases": [
    {
      "kind": "insert",
      "null_fks": [],
      "tables": [
        "clientes"
      ]
    }
  ],
  "rules": {},
  "tables": [ ... ],
  "warnings": []
}
```

## `generate`

```bash
synthdb generate RUTA.sql -c config.yaml -o DIR [--format csv|json] [--dry-run]
```

Genera los datos y escribe **un archivo por tabla** en `DIR` (creado si no
existe). `--format csv` (por defecto) o `--format json`.

```text
Escritos 4 archivo(s) CSV en out:
  out\clientes.csv
  out\viviendas.csv
  out\compraventas.csv
  out\pagos.csv
```

El CSV conserva el orden de columnas del esquema, representa `NULL` como campo
vacío y serializa los arrays como JSON dentro de la celda:

```csv
id,nombre,email,fecha_alta
1,Onofre Morata,fguerra@example.org,2018-05-18
2,Jerónimo Borrego-Morell,ggirona@example.net,2018-07-10
3,Catalina Corominas Ros,qsoler@example.org,2022-06-23
```

El formato JSON escribe una lista de objetos por tabla, con `null` para `NULL` y
listas nativas para los arrays.

### `--dry-run`

`generate` y `export` aceptan `--dry-run`: ejecutan el pipeline completo,
imprimen el plan y **hasta 10 filas de muestra por tabla**, y **no escriben
nada** (ni siquiera crean el directorio de salida).

```text
─────────────────────────────── Muestra (hasta 10 filas por tabla) ────────────────────────────────

clientes  (500 filas)
id │ nombre                      │ email                     │ fecha_alta
───┼─────────────────────────────┼───────────────────────────┼───────────
1  │ Onofre Morata               │ fguerra@example.org       │ 2018-05-18
2  │ Jerónimo Borrego-Morell     │ ggirona@example.net       │ 2018-07-10
3  │ Catalina Corominas Ros      │ qsoler@example.org        │ 2022-06-23
```

## `export`

```bash
synthdb export RUTA.sql -c config.yaml --format sql -o seed.sql [--dry-run]
```

Genera los datos y escribe un `seed.sql` de PostgreSQL cargable con
`psql -v ON_ERROR_STOP=1 -f seed.sql` (el esquema debe existir ya; el archivo no
contiene DDL). En el MVP solo se soporta `--format sql`.

```text
Escrito seed.sql (76006 bytes).
```

### Anatomía del `seed.sql`

- Cabecera con `SET client_encoding = 'UTF8'` y `SET standard_conforming_strings = on`.
- Cada **fase** del plan va envuelta en `BEGIN`/`COMMIT`, en orden.
- `INSERT` multi-fila por lotes (`output.batch_size`), con las columnas en el
  orden del esquema. Las columnas autoincrementales (`SERIAL`) se **omiten**: las
  asigna la base de datos.
- Todos los literales se renderizan con el generador de expresiones de sqlglot
  (comillas y backslashes escapados por la librería, arrays como `ARRAY[...]`,
  arrays vacíos como `CAST(ARRAY[] AS ...[])`). No hay escapado artesanal.
- Los identificadores se entrecomillan **solo cuando el plegado de PostgreSQL lo
  exige** (mayúsculas, caracteres especiales o palabra reservada).

```sql
-- Generado por SynthDB (synthdb export --format sql).
-- Dialecto: postgres. Semilla: 42.
-- Carga: psql -v ON_ERROR_STOP=1 -f seed.sql (el esquema debe existir ya).
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

-- INSERT: clientes
BEGIN;
INSERT INTO clientes (nombre, email, fecha_alta) VALUES
  ('Onofre Morata', 'fguerra@example.org', '2018-05-18'),
  ('Jerónimo Borrego-Morell', 'ggirona@example.net', '2018-07-10');
COMMIT;
```

Un **ciclo anulable** (p. ej. `ciclos_nullable.sql`) se emite insertando la FK
del ciclo a `NULL` y cerrándola después con un `UPDATE` en su propia fase, de
modo que el archivo carga aunque la FK no sea diferible:

```sql
-- INSERT: pedidos, facturas
BEGIN;
INSERT INTO pedidos (fecha, factura_id) VALUES
  ('2016-06-21', NULL),
  ('2021-04-17', NULL);
INSERT INTO facturas (numero, pedido_id) VALUES
  ('facturas_numero_5464', 2),
  ('facturas_numero_1864', 1);
COMMIT;

-- UPDATE diferido: pedidos (factura_id)
BEGIN;
UPDATE pedidos SET factura_id = 2 WHERE id = 1;
UPDATE pedidos SET factura_id = 3 WHERE id = 2;
COMMIT;
```

Un **ciclo diferible** se emite en una sola transacción con
`SET CONSTRAINTS ALL DEFERRED`.

## Cuarentena

Con `output.on_error: quarantine` (por defecto), las filas que no cumplen una
restricción se apartan y la generación continúa; al final se informa:

```text
Cuarentena: 3 fila(s) apartada(s) por no cumplir una restricción.
  pedidos: 3 fila(s). Primer motivo: columna fecha no admite NULL
```

Con `output.on_error: abort`, la primera fila inválida detiene la ejecución con
código de salida `5`.
