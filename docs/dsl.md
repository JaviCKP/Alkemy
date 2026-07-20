# Mini-DSL de reglas

Referencia de la gramática de las **reglas** (`rules`) del YAML de configuración
(especificacion.md §7.2, tareas T2.9/T2.10). Una regla expresa una relación entre
columnas de una misma fila —una derivación, una cota o una aserción— y el motor la
usa para generar datos coherentes y para validarlos.

Las reglas se declaran por tabla:

```yaml
tables:
  compraventas:
    rules:
      - "fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)"
      - "precio = parent(vivienda_id).superficie_m2 * ref('precio_m2_base') * noise(0.2)"
refs:
  precio_m2_base: 2350
```

## Seguridad: por qué no es SQL ni Python

Una regla **no se ejecuta como SQL** (nunca llega a la base de datos) ni **como
Python** (nada de `eval`, `exec`, `compile` ni acceso dinámico a atributos). El
texto se parsea con el parser de expresiones de `sqlglot` y acto seguido se
**traduce** a un AST propio y minúsculo; el intérprete (`rules/eval.py`) solo sabe
recorrer ese AST con un puñado de operaciones elegidas a mano. La lista de
construcciones admitidas es **cerrada**: todo lo que no aparezca en esta página se
rechaza en la compilación del plan con un `RuleParseError` que señala el fragmento
culpable. No hay ninguna puerta trasera por la que una regla ejecute código
arbitrario, lea el sistema de ficheros o toque la red.

## Gramática

### Valores

| Construcción | Ejemplos | Notas |
|---|---|---|
| Número | `42`, `-7`, `3.14`, `-0.2` | enteros y decimales |
| Cadena | `'piso'`, `'precio_m2_base'` | comillas simples |
| Booleano | `true`, `false` | |
| Nulo | `null` | |
| Columna de la fila | `superficie`, `fecha` | el valor ya generado de esa columna |
| Columna del padre | `parent(vivienda_id).anio_construccion` | la fila padre elegida por la FK `vivienda_id` |
| Constante con nombre | `ref('precio_m2_base')` | del bloque `refs` del YAML |

- `parent(<fk>)` toma el **nombre desnudo** de una columna FK y siempre va seguido
  de `.<columna>`; `parent(<fk>)` a secas no es una expresión válida.
- `ref('<nombre>')` toma una **cadena** con el nombre de la constante.

### Nombres de columna y de FK: identificadores seguros

Los nombres de columna, el nombre de la FK de `parent(<fk>)` y el nombre de la
columna del padre en `parent(fk).<columna>` deben ser **identificadores seguros**:
casar con `[A-Za-z_][A-Za-z0-9_]*` y **no** usar el patrón *dunder* (doble guion
bajo inicial y final, `__x__`). Es una restricción de **seguridad** (defensa en
profundidad): corta en el parser cualquier nombre que un resolutor de columnas o de
padre basado en atributos pudiera convertir en un acceso a un interno de Python.

Consecuencia (limitación): una columna de PostgreSQL que solo exista **entre
comillas** —con espacios, guiones o dígito inicial (`"mi columna"`, `"2col"`)— o con
un nombre dunder (`"__dict__"`) **no es referenciable desde una regla del DSL**. Si
necesitas usarla en una regla, renómbrala en el esquema a un identificador seguro. Un
guion bajo doble solo al inicio (`__x`) o solo al final (`x__`) sí es válido: la
restricción es únicamente el patrón dunder completo.

### Operadores

| Tipo | Operadores |
|---|---|
| Comparación | `=`  `<>`  `<`  `<=`  `>`  `>=` |
| Aritmética | `+`  `-`  `*`  `/` (división real) |
| Booleanos | `and`  `or`  `not` |
| Paréntesis | `( ... )` para agrupar |

`=` es **igualdad** (comparación), no asignación; en una derivación `col = expr` se
lee además como "genera `col` a partir de `expr`" (ver más abajo).

### Funciones (lista blanca)

Estas seis funciones son las **únicas** permitidas (`rules/eval.py::FUNCTIONS`):

| Función | Firma | Devuelve | Descripción |
|---|---|---|---|
| `date` | `date(anio, mes, dia)` | fecha | construye una fecha |
| `date_add` | `date_add(fecha, dias)` | fecha | desplaza una fecha `dias` días |
| `years_between` | `years_between(a, b)` | entero | años completos de `b` a `a` (edad) |
| `noise` | `noise(sigma)` | número | multiplicador `1 + N(0, sigma)` |
| `round` | `round(x)` / `round(x, ndigits)` | número | redondeo (por defecto a entero) |
| `len` | `len(texto)` | entero | longitud de una cadena |

`noise(sigma)` usa el **RNG determinista de la fila**: la misma fila con la misma
semilla produce el mismo ruido (no hay `random` global ni `datetime.now()` en
ninguna ruta de generación). Se usa multiplicativo: `precio * noise(0.2)` perturba
`precio` en ±20 % (1σ).

## Los tres tipos de regla

`clasify_rule(rule)` etiqueta cada regla según su uso **adicional** en el motor.
Con independencia del tipo, **toda regla se re-evalúa siempre como aserción** tras
generar la fila (doble uso de §7.2).

### `bound` — cota del generador

Una desigualdad con **una columna local despejada** a un lado y una expresión que
**no la referencia** al otro. El motor usa la expresión como cota (inferior o
superior) del generador de esa columna, de modo que el valor nace ya válido.

```text
fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)   # cota inferior de fecha
fecha_defuncion > fecha_nacimiento                            # fecha_defuncion > otra columna
superficie_m2 <= 450                                          # cota superior
```

`col >= expr` y `col > expr` dan cota **inferior** (inclusiva / exclusiva);
`col <= expr` y `col < expr`, cota **superior**. La forma volteada (`0 < precio`)
se normaliza a `precio > 0`.

### `derivation` — valor calculado

`col = expresión`, donde `col` es una columna local desnuda y la expresión no la
referencia. El generador `derived` de esa columna calcula su valor evaluando la
expresión.

```text
precio = parent(vivienda_id).superficie_m2 * ref('precio_m2_base') * noise(0.2)
total = subtotal
```

### `assertion` — comprobación posterior

Cualquier otra expresión evaluable a booleano: se comprueba tras generar la fila
completa y no impone orden entre columnas.

```text
a + b = c + d
activo and superficie > 0
precio <> 0
```

## Orden de generación de columnas

Las reglas `bound` y `derivation` introducen dependencias **implícitas**: la
columna que acotan o derivan debe generarse **después** de las columnas locales que
su expresión lee. `build_column_order` (en `generation/context.py`) construye ese
grafo y hace un topo-sort determinista (desempate alfabético). Un ciclo entre
columnas (`a = b + 1`, `b = a + 1`) es un `PlanError` que nombra el ciclo: no existe
un orden válido y hay que reescribir una de las reglas (por ejemplo, como aserción,
que no impone orden). Las referencias al padre (`parent(...)`) y las constantes
(`ref(...)`) no son columnas de la fila, así que no cuentan como dependencias de
orden.

## Errores

- **`RuleParseError`** (compilación del plan): la regla usa una construcción fuera
  de esta gramática. El mensaje incluye el fragmento culpable. Ejemplos: una función
  no permitida, un subíndice, un comentario, un `;`, una subconsulta, un agregado de
  grupo.
- **`RuleEvalError`** (ejecución): la regla es gramaticalmente válida pero falla al
  evaluarse sobre una fila concreta — columna que aún no existe, `ref` no definida en
  `refs`, fila padre ausente (FK NULL o no resuelta), división entre cero, o tipos
  incompatibles. El mensaje incluye la regla y un extracto de la fila.

## Qué NO está soportado (y por qué)

Deliberadamente fuera del mini-DSL del MVP:

- **Agregados sobre el grupo de hijos** (`sum(importe) ≈ parent.precio ± 0.01`): son
  de la v1.0 (`sum_over_group`, especificacion.md §2/§16). Se rechazan con mención
  expresa a la v1.0.
- **Concatenación** (`||`, `concat`), **`LIKE`**, **subíndices** (`a[0]`),
  **atributos arbitrarios** (`a.b.c`), **columnas cualificadas** (`t.c`), **`CAST`**,
  **`CASE`**, **`IN`**, **`BETWEEN`**, **`IS NULL`**, **`^`**, **`%`**, subconsultas,
  comentarios (`--`, `/* */`) y `;`.
- **Cualquier función** que no sea una de las seis de la lista blanca.

Si necesitas expresar algo que aquí no cabe, abre una incidencia con el caso: la
lista blanca se amplía añadiendo una función a `rules/eval.py::FUNCTIONS` **con sus
tests**, nunca abriendo una vía genérica.
