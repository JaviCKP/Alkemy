"""Modelos Pydantic del YAML de configuración del MVP (T2.5, especificacion.md §11).

La configuración del usuario es la fuente de máxima prioridad del fusor
(§7.1): manda sobre la IR salvo cuando la contradice, en cuyo caso el fusor
la rechaza (`semantic/merge.py`, T2.6). Aquí solo se define y valida su
*forma*; nada de esto genera datos ni interpreta reglas.

Alcance deliberado de esta sesión (H2 Sesión B):

- **`fk`** valida únicamente la forma de cada estrategia de selección de FK
  (`uniform`, `zipf`, `unique_subset`, `quota`; especificacion.md §7.4). El
  selector que las consume es de la sesión C (T2.8); aquí un `fk` mal escrito
  falla pronto, con ruta de campo exacta, en vez de reventar en el motor.
- **`rules`** son cadenas del mini-DSL que se guardan **sin interpretar**: el
  parser y el intérprete con lista blanca son de la sesión D (T2.9). Validar
  su gramática aquí adelantaría trabajo de esa sesión y acoplaría dos tareas.
- **`llm`** se parsea entero (todos los campos de §11) pero **no tiene efecto
  hasta el Hito 3**: la capa semántica del modelo (proveedores, contrato,
  fusión con confianza efectiva de ADR-002) se conecta en T3.x. `min_confidence`
  es la única excepción: el fusor del H2 ya lo lee como umbral de las heurísticas.

Todos los modelos llevan `extra="forbid"`: una clave desconocida es un error
de validación con su ruta exacta (p. ej. `tables.viviendas.columns.foo.generator`),
nunca un campo ignorado en silencio (CLAUDE.md).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigModel(BaseModel):
    """Base común de los modelos de configuración: no admite campos desconocidos."""

    model_config = ConfigDict(extra="forbid")


class LLMConfig(ConfigModel):
    """Bloque `llm` del YAML (especificacion.md §11).

    Sin efecto en el Hito 2 salvo `min_confidence`, que el fusor usa como umbral
    de las heurísticas. El resto de campos (proveedor, modelo, `base_url`,
    `allow_data_sampling`) los consume la capa semántica del Hito 3 (T3.x). El
    valor por defecto de `enabled` se deja en `False` para que el H2 sea
    local-first sin sorpresas; ADR-002 (decisión *Go*) fija que el H3 lo activará
    por defecto al conectar el modelo.
    """

    enabled: bool = Field(
        default=False,
        description="Activa la capa semántica del LLM. Sin efecto hasta el Hito 3 (ADR-002).",
    )
    provider: Literal["ollama", "openai_compat", "anthropic"] = "ollama"
    model: str = "qwen2.5:7b-instruct"
    base_url: str | None = Field(
        default=None, description="Endpoint del proveedor; None ⇒ el del proveedor."
    )
    min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Umbral por debajo del cual una propuesta cae al fallback seguro con "
            "aviso. Ya operativo en el H2 como umbral de las heurísticas del fusor; "
            "en el H3 se aplica sobre la confianza *efectiva* de ADR-002."
        ),
    )
    allow_data_sampling: bool = Field(
        default=False,
        description="Privacidad: sin esto, al modelo solo viajan esquema y metadatos, no valores.",
    )


class Defaults(ConfigModel):
    """Valores por defecto aplicados a toda tabla/columna que no los fije (§11)."""

    rows: int = Field(default=100, gt=0, description="Filas por tabla si la tabla no fija `rows`.")
    null_ratio: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Proporción de NULL por defecto en columnas anulables.",
    )


class ColumnConfig(ConfigModel):
    """Configuración de una columna dentro de `tables.<tabla>.columns.<col>` (§11).

    Todos los campos son opcionales: el usuario puede fijar solo el generador, solo
    el `null_ratio`, o cualquier combinación. `params` se pasa tal cual al modelo
    de parámetros del generador cuando el fusor lo resuelve (validación fina
    diferida a ese punto, con la tabla/columna en el mensaje).
    """

    generator: str | None = Field(
        default=None, description="Id del generador (faker, numeric_range, choice...)."
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Parámetros del generador.")
    null_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Proporción de NULL de esta columna; solo aplicable si la columna es anulable.",
    )
    unique: bool | None = Field(
        default=None,
        description="Fuerza valores únicos; la unicidad de la IR lo impone aunque sea None.",
    )


class FkUniform(ConfigModel):
    """Selección de FK uniforme: cualquier padre con igual probabilidad (§7.4)."""

    strategy: Literal["uniform"]
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class FkZipf(ConfigModel):
    """Selección de FK sesgada: pocos padres concentran muchos hijos (§7.4)."""

    strategy: Literal["zipf"]
    s: float = Field(default=1.2, gt=0.0, description="Exponente de la zipf; mayor ⇒ más sesgo.")
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class FkUniqueSubset(ConfigModel):
    """Selección de FK 1:1: padres sin reemplazo (§7.4)."""

    strategy: Literal["unique_subset"]
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class FkQuota(ConfigModel):
    """Selección de FK por cuotas: cada padre recibe entre `min` y `max` hijos (§7.4)."""

    strategy: Literal["quota"]
    min: int = Field(ge=0, description="Mínimo de hijos por padre.")
    max: int = Field(ge=0, description="Máximo de hijos por padre.")
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    def model_post_init(self, _context: object) -> None:
        """Valida que `min <= max` con un mensaje accionable."""
        if self.min > self.max:
            raise ValueError(
                f"fk quota: 'min' ({self.min}) no puede ser mayor que 'max' ({self.max})."
            )


FkStrategy = Annotated[
    FkUniform | FkZipf | FkUniqueSubset | FkQuota,
    Field(discriminator="strategy"),
]
"""Estrategia de selección de una FK, discriminada por `strategy` (§7.4).

Solo se valida la *forma*: el selector que la consume (KeyStore) es de la
sesión C (T2.8). Una `strategy` desconocida o un campo de más falla aquí con
ruta exacta, no en el motor.
"""


class TableConfig(ConfigModel):
    """Configuración de una tabla dentro de `tables.<tabla>` (§11)."""

    rows: int | None = Field(
        default=None, gt=0, description="Filas de la tabla; None ⇒ `defaults.rows`."
    )
    columns: dict[str, ColumnConfig] = Field(default_factory=dict)
    fk: dict[str, FkStrategy] = Field(
        default_factory=dict,
        description="Estrategia por columna FK (solo forma; selector en sesión C).",
    )
    rules: list[str] = Field(
        default_factory=list,
        description=(
            "Reglas del mini-DSL, guardadas SIN interpretar. El parser/intérprete "
            "con lista blanca es de la sesión D (T2.9); aquí son cadenas opacas."
        ),
    )


class HierarchyConfig(ConfigModel):
    """Parámetros de una autorreferencia (`tabla.columna` → forma del árbol, §11)."""

    branching: int = Field(gt=0, description="Hijos por nodo al repartir filas en niveles.")
    max_depth: int = Field(gt=0, description="Profundidad máxima del árbol de la autorreferencia.")


class OutputConfig(ConfigModel):
    """Bloque `output` del YAML: cómo emitir y qué hacer ante errores (§11)."""

    batch_size: int = Field(
        default=5000, gt=0, description="Tamaño de lote de generación/inserción."
    )
    on_error: Literal["quarantine", "abort"] = Field(
        default="quarantine", description="Filas inválidas: aislar en cuarentena o abortar."
    )
    max_repair_retries: int = Field(
        default=3, ge=0, description="Reintentos de reparación de una fila antes de la cuarentena."
    )


class Config(ConfigModel):
    """Raíz del YAML de configuración del MVP (especificacion.md §11).

    `dialect` aparece en el ejemplo de §11 y por tanto forma parte del contrato
    (con `extra="forbid"`, omitirlo del modelo haría que el ejemplo no cargara).
    """

    version: int = Field(default=1, description="Versión del formato de configuración.")
    seed: int = Field(default=0, description="Semilla global de la generación (determinismo).")
    locale: str = Field(default="es_ES", description="Locale de Faker por defecto.")
    dialect: str = Field(default="postgres", description="Dialecto SQL del esquema.")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    tables: dict[str, TableConfig] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(
        default_factory=dict, description="Constantes con nombre usables en reglas (sesión D)."
    )
    hierarchy: dict[str, HierarchyConfig] = Field(
        default_factory=dict, description="Autorreferencias por `tabla.columna` (§6.3)."
    )
    output: OutputConfig = Field(default_factory=OutputConfig)
