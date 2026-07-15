"""TH0.2 — Contrato v0: subconjunto del JSON Schema de la especificacion (SS8).

Solo lo necesario para medir lo que el Hito 0 pregunta (exactitud de rol y
de generador, calibracion de confianza, validez sintactica). Se omiten a
proposito `depends_on`, `rules`, `distribution`, `null_ratio` y `fk_hints`
del contrato completo: pertenecen al fusor de H3 y no aportan nada a la
pregunta falsable de este experimento. El contrato completo se escribe en
T3.3 sobre el mismo patron.

Uso:
    from contract import SemanticPlanResponse
    SemanticPlanResponse.model_validate(respuesta_json)
    SemanticPlanResponse.model_json_schema()  # para format= del proveedor
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GeneratorType = Literal[
    "faker",
    "choice",
    "numeric_range",
    "datetime_range",
    "template",
    "sequence",
    "uuid",
    "derived",
    "text_pool",
]


class GeneratorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: GeneratorType
    params: dict[str, object] = Field(default_factory=dict)


class ColumnPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column_name: str
    semantic_role: str = Field(
        description="Etiqueta corta: email, nombre_persona, precio, superficie..."
    )
    generator: GeneratorSpec
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class TablePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    entity: str = Field(description="Que representa la tabla, en 3-8 palabras")
    confidence: float = Field(ge=0, le=1)
    columns: list[ColumnPlan]
    warnings: list[str] = Field(default_factory=list)


class SemanticPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TablePlan]


if __name__ == "__main__":
    import json

    ejemplo = {
        "tables": [
            {
                "table_name": "clientes",
                "entity": "cliente o persona de contacto",
                "confidence": 0.95,
                "warnings": [],
                "columns": [
                    {
                        "column_name": "email",
                        "semantic_role": "email",
                        "generator": {
                            "type": "faker",
                            "params": {"provider": "email", "unique": True},
                        },
                        "confidence": 0.99,
                        "warnings": [],
                    },
                    {
                        "column_name": "fecha_alta",
                        "semantic_role": "fecha_alta_cliente",
                        "generator": {"type": "datetime_range", "params": {"start": "2015-01-01"}},
                        "confidence": 0.85,
                        "warnings": [],
                    },
                ],
            }
        ]
    }

    plan = SemanticPlanResponse.model_validate(ejemplo)
    print("Ejemplo valida OK:", plan.tables[0].table_name)
    print()
    print(json.dumps(SemanticPlanResponse.model_json_schema(), indent=2, ensure_ascii=False))
