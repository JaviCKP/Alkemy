"""Hash canónico del esquema (T1.5, especificacion.md §5 y §13).

`schema_hash()` calcula el SHA-256 de la forma canónica de la IR (nunca del
texto SQL original): dos DDL que produzcan la misma `SchemaSpec` estructural
producen siempre el mismo hash, con independencia de espacios, mayúsculas de
palabras clave o comentarios de sintaxis, que ni siquiera llegan a la IR.

Decisiones de canonicalización (ya tomadas; no reabrir sin un ADR nuevo,
CLAUDE.md):

- Las **tablas** se ordenan por la clave compuesta `(schema, name)` (`schema`
  ausente se normaliza a cadena vacía): dos esquemas con las mismas tablas en
  distinto orden producen el mismo hash. Solo `name` no basta porque dos
  tablas homónimas en namespaces distintos (`ventas.users`, `rrhh.users`,
  ambas válidas en PostgreSQL) desempatarían por orden de entrada.
- Las **columnas** dentro de cada tabla NO se reordenan: su orden es parte de
  la identidad del esquema (afecta a los `INSERT` posicionales), así que
  reordenarlas cambia el hash.
- Los campos **derivados** que no proceden del DDL, sino que synthdb infiere
  a partir de él, quedan excluidos del hash (ver `_EXCLUDED_FIELDS`): el hash
  identifica lo que el usuario escribió, no lo que synthdb dedujo de ello.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from synthdb.ir.schema import SchemaSpec

_EXCLUDED_FIELDS: dict[str, Any] = {
    # SchemaSpec: el propio hash (sería circular) y los avisos, que son
    # ruido de ejecución, no estructura del esquema.
    "hash": True,
    "warnings": True,
    "tables": {
        "__all__": {
            # TableSpec.kind: rol estructural inferido por graph/dependency.py.
            "kind": True,
            "foreign_keys": {
                # RelationshipSpec.cardinality_hint: derivado por el planificador.
                "__all__": {"cardinality_hint": True}
            },
            "checks": {
                # CheckSpec.ast_supported/bounds_derived: derivados por
                # constraints/check_interp.py, no forman parte del DDL.
                "__all__": {"ast_supported": True, "bounds_derived": True}
            },
            "columns": {
                "__all__": {
                    # Checks de columna: mismos campos derivados que arriba.
                    "checks": {"__all__": {"ast_supported": True, "bounds_derived": True}}
                }
            },
        }
    },
}
"""Campos de la IR excluidos del hash por ser derivados, no procedentes del DDL.

Mecanismo explícito y centralizado (CLAUDE.md): añadir un campo derivado
nuevo a la IR en el futuro solo requiere una entrada nueva aquí, nunca un
caso suelto disperso por el código. La forma anidada sigue el formato de
`include`/`exclude` avanzado de Pydantic v2, donde `"__all__"` aplica la
exclusión a todos los elementos de una lista, evitando así toda ambigüedad
con campos homónimos en otros niveles (p. ej. `TableSpec.kind` frente a
`TypeSpec.kind`, que no se excluye).
"""


def schema_hash(spec: SchemaSpec) -> str:
    """Calcula el hash canónico SHA-256 de un `SchemaSpec`.

    El hash se calcula sobre la IR ya parseada, nunca sobre el texto SQL: dos
    DDL que difieran solo en espacios, mayúsculas de palabras clave o
    comentarios producen la misma `SchemaSpec` y, por tanto, el mismo hash.

    Es determinista entre llamadas, procesos y valores de `PYTHONHASHSEED`:
    la canonicalización nunca depende de `hash()` de Python ni del orden de
    iteración de un `set`/`dict`, solo de los valores de los campos.

    Args:
        spec: Esquema ya parseado y validado cuyo hash se quiere calcular.

    Returns:
        El hash como cadena hexadecimal de 64 caracteres en minúsculas.
    """
    data = spec.model_dump(mode="json", by_alias=True, exclude=_EXCLUDED_FIELDS)
    data["tables"] = sorted(
        data["tables"], key=lambda table: (table.get("schema") or "", table["name"])
    )

    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
