# Validación de `schema.sql` de inmobiliaria

Fecha: 2026-07-17  
Repositorio: `C:\Users\javi\Documents\PueblaBBDD`  
Rama observada: `main` (`618f36a`)  
Archivo probado: `C:\Users\javi\Documents\workflows\inmobiliaria-ia\schema.sql`

## Resultado

El `schema.sql` original **no completa** `synthdb analyze` con el estado actual del repositorio.
La CLI termina con código `1` por un error de parseo en la línea 202, columna 24:

```sql
on delete set null (assigned_user_id),
```

El mismo patrón aparece 21 veces en el archivo. SQLGlot 30.12.0 acepta `ON DELETE SET NULL`, pero rechaza la lista de columnas posterior a `SET NULL`. Por eso no se genera JSON ni se alcanza el análisis del grafo con el archivo original.

Comando reproducible:

```powershell
.\.venv\Scripts\python.exe -m synthdb.cli analyze `
  "C:\Users\javi\Documents\workflows\inmobiliaria-ia\schema.sql" --json
```

Salida relevante:

```text
exit code: 1
Error de sintaxis SQL en línea 202, columna 24: Expecting ).
```

`uv run` produce el mismo resultado cuando se le asigna una caché temporal escribible. La primera ejecución de `uv run` no llegó al proyecto porque la caché por defecto estaba bloqueada por permisos en `C:\Users\javi\AppData\Local\uv\cache`.

## Ejecución hasta el siguiente bloqueo

Para continuar la comprobación se creó una copia temporal, sin modificar el original, sustituyendo únicamente las 21 cláusulas `ON DELETE SET NULL (columna)` por `ON DELETE SET NULL`. Esa transformación **no es una corrección aplicable automáticamente**, porque puede cambiar la semántica de las FK compuestas multi-inmobiliaria.

Con esa copia, el pipeline reconoce:

- 20 tablas: `inmobiliarias`, `usuarios`, `clientes`, `busquedas`, `inmuebles`, `propietarios_inmueble`, `historial_precios`, `matches`, `intereses`, `operaciones`, `participantes_operacion`, `ofertas`, `tramites`, `documentos`, `conversaciones`, `tareas`, `citas`, `llamadas`, `aprobaciones` y `faq`.
- 121 avisos acumulados de parser e interpretación de `CHECK`.
- 0 avisos adicionales del grafo antes de resolver ciclos.

La CLI alcanza entonces un segundo bloqueo y termina con código `2` por un ciclo irrompible entre:

```text
aprobaciones, citas, intereses, matches, operaciones, tareas, tramites
```

El diagnóstico indica que las FK implicadas se consideran todas `NOT NULL` y ninguna es `DEFERRABLE`. El mensaje ofrece como opciones hacer alguna FK anulable, usar `DEFERRABLE INITIALLY DEFERRED` o desactivar constraints durante la carga; esta última opción está desaconsejada por las reglas del proyecto.

## Avisos que aparecerían después del parseo

La copia temporal también confirma limitaciones independientes del error de sintaxis:

- `TEXT[]` se degrada a `text` sin restricciones en `clientes.roles`, `busquedas.tipos`, `busquedas.zonas` y `faq.etiquetas`.
- Se registran avisos para `CREATE EXTENSION`, funciones PL/pgSQL, triggers, índices, vistas, `ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY` y políticas RLS, porque son sentencias fuera del subconjunto DDL estructural soportado por el Hito 1.
- Probar SQLGlot con `ErrorLevel.IGNORE` no es una solución: solo conserva 9 de las 20 tablas y produce nodos parciales/comandos.

El cálculo actual de nulabilidad de una FK compuesta usa `all(...)` sobre sus columnas locales en [parsing/ddl.py](../src/synthdb/parsing/ddl.py#L524). En este esquema, `inmobiliaria_id` suele ser `NOT NULL` mientras que el identificador de entidad sí admite `NULL`; además, `ON DELETE SET NULL (entidad_id)` expresa que solo debe anularse esa segunda columna. El IR actual no representa esa selección de columnas, por lo que no se puede afirmar que el ciclo se resolvería correctamente sin ampliar parser e IR.

## Salud del repositorio

La suite estándar sí está verde cuando pytest usa un directorio temporal escribible dentro del repo:

```text
294 passed
10 snapshots passed
```

La primera ejecución directa obtuvo 288 tests pasados y 6 errores de permisos antes de ejecutar esos tests; todos desaparecieron al cambiar únicamente `--basetemp`. No se modificó código fuente, `schema.sql`, `pyproject.toml` ni `uv.lock`.

## Conclusión y siguiente trabajo recomendado

El repositorio está sano según su suite, pero el parser actual no puede consumir este esquema tal cual. Para soportarlo correctamente habría que:

1. Añadir al parser/IR la lista de columnas afectadas por `SET NULL`/`SET DEFAULT` en una FK.
2. Hacer que la planificación de ciclos use esa nulabilidad dirigida, no solo la nulabilidad de todas las columnas de la FK compuesta.
3. Decidir explícitamente qué sentencias operativas (RLS, políticas, funciones, índices y vistas) deben seguir siendo avisos o recibir soporte.
4. Registrar el cambio de IR mediante un ADR, porque la IR está congelada.

Esta validación no ejecutó el DDL contra un servidor PostgreSQL real; valida el flujo estructural de SynthDB y SQLGlot.
