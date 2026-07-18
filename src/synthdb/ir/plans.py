"""Plan estructural y fases de ejecución (especificacion.md §6).

`StructuralPlan` es la salida de `graph/dependency.py` (T1.6): documenta la
topología de dependencias del esquema (fases, ciclos, autorreferencias,
puentes) sin decidir todavía cómo generar los datos. `Phase` (unión de
`InsertPhase`/`InsertLeveledPhase`/`UpdatePhase`/`DeferredPhase`) es la
salida de `graph/strategies.py` (T1.7): la secuencia final y concreta de
pasos de inserción que ejecutará el motor de generación (H2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from synthdb.ir.schema import GeneratorSpec, IRModel


class FkRef(IRModel):
    """Identifica sin ambigüedad una FK concreta dentro de un `SchemaSpec`."""

    table: str = Field(description="Tabla propietaria de la FK (el «hijo»).")
    columns: list[str] = Field(description="Columnas locales de la FK, en orden de declaración.")
    ref_table: str = Field(description="Tabla referenciada (el «padre»).")
    null_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Subconjunto de `columns` que se inserta a NULL para romper un ciclo, "
            "cuando este `FkRef` aparece en `InsertPhase.null_fks`. Bajo "
            "`MATCH SIMPLE` puede ser solo parte de la FK (p. ej. `entidad_id` de "
            "`(inmobiliaria_id, entidad_id)`, con `inmobiliaria_id` a su valor "
            "real); bajo `MATCH FULL` son todas. Vacío fuera de ese contexto "
            "(p. ej. en `UnbreakableCycle.edges`, que solo identifica la FK). "
            "Ver ADR-004."
        ),
    )


class StructuralPlan(IRModel):
    """Topología de dependencias del esquema, ya resuelta en fases.

    No decide todavía cómo romper ciclos ni autorreferencias (eso es
    `graph/strategies.py::resolve_cycles`, T1.7); documenta la estructura
    del grafo para que ese paso posterior no tenga que recalcularla.
    """

    tables_by_phase: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Fases de dependencia, de padres a hijos: cada fase es la lista "
            "de tablas que la componen, en orden alfabético. Tablas "
            "independientes entre sí (y componentes fuertemente conexos de "
            "una sola tabla) se fusionan en la misma fase cuando su posición "
            "en el grafo lo permite; un ciclo real (2+ tablas) ocupa una "
            "única fase con todos sus miembros."
        ),
    )
    sccs: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Componentes fuertemente conexos de 2 o más tablas (ciclos "
            "reales entre tablas distintas), cada uno con sus tablas en "
            "orden alfabético; la lista externa, ordenada por fase y luego "
            "alfabéticamente. No incluye autorreferencias (ver `self_refs`)."
        ),
    )
    self_refs: list[str] = Field(
        default_factory=list,
        description="Tablas con alguna FK que se referencia a sí misma, en orden alfabético.",
    )
    bridges: list[str] = Field(
        default_factory=list,
        description="Tablas con `TableSpec.kind == 'bridge'`, en orden alfabético.",
    )
    warnings: list[str] = Field(default_factory=list)


class InsertPhase(IRModel):
    """Inserción de una o más tablas sin dependencias pendientes entre sí.

    Cuando `null_fks` no está vacío, esta fase rompe un ciclo: las columnas
    de `FkRef.null_columns` se insertan a `NULL` y una `UpdatePhase` posterior
    les asigna su valor real (especificacion.md §6.2, opción 1). Bajo
    `MATCH SIMPLE` esas columnas pueden ser solo una parte de la FK compuesta
    —el resto se inserta con su valor real— y bajo `MATCH FULL` son todas
    (ADR-004).
    """

    kind: Literal["insert"] = "insert"
    tables: list[str] = Field(description="Tablas a insertar en esta fase, ya en orden válido.")
    null_fks: list[FkRef] = Field(
        default_factory=list,
        description=(
            "FK cuyas `null_columns` se insertan a NULL en esta fase para romper "
            "un ciclo; cada `FkRef` registra QUÉ columnas se anulan, no solo de "
            "qué FK se trata (ADR-004)."
        ),
    )


class InsertLeveledPhase(IRModel):
    """Autorreferencia generada por niveles (especificacion.md §6.3).

    L0 son las raíces (`self_fk_columns` a NULL), L1 apunta a L0, etc. El
    reparto real de filas por nivel es responsabilidad del motor de
    generación (H2): esta fase solo documenta la estrategia estructural.
    """

    kind: Literal["insert_leveled"] = "insert_leveled"
    table: str
    self_fk_columns: list[str] = Field(description="Columnas de la FK de autorreferencia.")
    roots_point_to_self: bool = Field(
        default=False,
        description=(
            "`True` cuando la FK de autorreferencia es NOT NULL y no "
            "diferible: sin tocar el DDL, la única salida válida es que las "
            "filas raíz se referencien a sí mismas. Siempre acompañado de un "
            "aviso en `StructuralPlan.warnings`."
        ),
    )


class UpdatePhase(IRModel):
    """UPDATE posterior que asigna los valores reales de las columnas insertadas a NULL."""

    kind: Literal["update"] = "update"
    table: str
    columns: list[str] = Field(
        description=(
            "Columnas que se insertaron a NULL para romper el ciclo y que ahora "
            "reciben su valor real; coinciden con las `null_columns` de la "
            "`InsertPhase` correspondiente (ADR-004)."
        )
    )


class DeferredPhase(IRModel):
    """Inserción de un conjunto de tablas en una única transacción con constraints diferidas."""

    kind: Literal["deferred"] = "deferred"
    tables: list[str]


Phase = InsertPhase | InsertLeveledPhase | UpdatePhase | DeferredPhase
"""Paso concreto de un `PopulationPlan` (especificacion.md §6.2-6.3)."""


PlanSource = Literal["user", "ir", "llm", "heuristic", "fallback"]
"""Origen de la decisión del fusor para una columna (especificacion.md §7.1).

El orden de la unión refleja la prioridad decreciente: la configuración del
usuario manda sobre la IR, la IR sobre el LLM (con confianza *efectiva*,
ADR-002), el LLM sobre las heurísticas, y el fallback seguro cierra la lista.
`"llm"` se reserva aquí pero no lo produce todavía ningún camino: la capa
semántica del modelo entra en el fusor en el Hito 3 (T3.7); en el H2 el plan
solo puede llevar `"user"`, `"ir"`, `"heuristic"` o `"fallback"`.
"""


class ColumnPlan(IRModel):
    """Decisión del fusor para una columna: qué generador, de dónde y con qué confianza.

    Salida de `semantic/merge.py::build_plan` (T2.6). Cada columna del esquema
    produce exactamente un `ColumnPlan`, con la trazabilidad que exige la
    especificación (§7.1): `source` y `confidence` dicen *por qué* se eligió ese
    generador, y `warnings` recoge todo lo que el usuario debería revisar sin que
    nada quede en silencio (CLAUDE.md).
    """

    column: str
    generator: GeneratorSpec | None = Field(
        default=None,
        description=(
            "Generador resuelto para la columna, o `None` cuando la base de datos "
            "asigna el valor y la columna se excluye de los INSERT: columnas "
            "`autoincrement` (SERIAL) y `GENERATED ALWAYS AS`. En ese caso "
            "`source='ir'` (la exclusión la dicta la IR, no una inferencia)."
        ),
    )
    source: PlanSource
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confianza de la fuente ganadora. `1.0` para usuario e IR (decisiones "
            "duras), la confianza del patrón para heurística, `0.0` para fallback "
            "(no hay señal semántica, solo validez estructural)."
        ),
    )
    role: str | None = Field(
        default=None,
        description="Rol semántico inferido (heurística) para trazabilidad; `None` si no lo hay.",
    )
    warnings: list[str] = Field(default_factory=list)


class TablePlan(IRModel):
    """Plan de generación de una tabla: un `ColumnPlan` por columna, en orden de la IR."""

    table: str
    columns: list[ColumnPlan]
    warnings: list[str] = Field(default_factory=list)


class TablePlans(IRModel):
    """Planes de todas las tablas del esquema, en el orden de la IR.

    Salida completa de `build_plan`. No decide todavía cantidades de filas ni
    estrategias de FK (eso vive en el `PopulationPlan` y en el selector de la
    sesión C): es la asignación columna→generador ya fusionada y auditada, lista
    para que el motor la consuma. Determinista: mismo `spec` + misma `Config` ⇒
    mismos bytes vía `canonical_json` (CLAUDE.md).
    """

    tables: list[TablePlan]
    warnings: list[str] = Field(default_factory=list)
