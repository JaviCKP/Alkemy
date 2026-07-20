"""Tests del fusor `semantic/merge.py` (T2.6, especificacion.md §7.1).

Cada test ejercita un eslabón de la cadena de prioridad (usuario > IR >
heurística > fallback) y, sobre todo, sus **contradicciones**: lo que el fusor
tiene que rechazar o recortar es tan importante como lo que acepta. Todo pasa
por el `parse_ddl` + `interpret_checks` reales, no por una IR fabricada a mano.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthdb.config.loader import ConfigError, load_config
from synthdb.config.models import (
    ColumnConfig,
    Config,
    Defaults,
    FkQuota,
    LLMConfig,
    TableConfig,
)
from synthdb.constraints.check_interp import interpret_checks
from synthdb.ir.plans import ColumnPlan, TablePlans
from synthdb.ir.schema import canonical_json
from synthdb.parsing.ddl import parse_ddl
from synthdb.semantic.merge import PlanError, build_plan

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"


def _plan(ddl: str, config: Config | None = None) -> TablePlans:
    spec = interpret_checks(parse_ddl(ddl))
    return build_plan(spec, config or Config())


def _plan_schema(name: str, config: Config | None = None) -> TablePlans:
    return _plan((_SCHEMAS_DIR / name).read_text("utf-8"), config)


def _column(plan: TablePlans, table: str, column: str) -> ColumnPlan:
    table_plan = next(t for t in plan.tables if t.table == table)
    return next(c for c in table_plan.columns if c.column == column)


# --- 1. Usuario: manda, pero se valida contra la IR ---------------------------


def test_usuario_gana_dentro_de_las_cotas() -> None:
    plan = _plan(
        "CREATE TABLE viviendas (superficie_m2 NUMERIC(7,2) NOT NULL CHECK (superficie_m2 > 0));",
        Config(
            tables={
                "viviendas": TableConfig(
                    columns={
                        "superficie_m2": ColumnConfig(
                            generator="numeric_range", params={"min": 35, "max": 450}
                        )
                    }
                )
            }
        ),
    )
    cp = _column(plan, "viviendas", "superficie_m2")
    assert cp.source == "user"
    assert cp.generator is not None
    assert cp.generator.type == "numeric_range"
    assert cp.generator.params["min"] == 35  # dentro del CHECK (>0): no se toca


def test_usuario_con_cotas_fuera_del_check_es_plan_error() -> None:
    with pytest.raises(PlanError) as exc:
        _plan(
            "CREATE TABLE t (anio INT NOT NULL CHECK (anio BETWEEN 1900 AND 2026));",
            Config(
                tables={
                    "t": TableConfig(
                        columns={
                            "anio": ColumnConfig(
                                generator="numeric_range", params={"min": 1800, "max": 3000}
                            )
                        }
                    )
                }
            ),
        )
    message = str(exc.value)
    assert "t.anio" in message
    assert "1800" in message  # la cota del usuario
    assert "1900" in message  # y la del CHECK, ambas partes del conflicto


def test_usuario_con_choice_fuera_del_enum_es_plan_error() -> None:
    with pytest.raises(PlanError) as exc:
        _plan(
            "CREATE TABLE t (tipo TEXT NOT NULL CHECK (tipo IN ('piso', 'chalet')));",
            Config(
                tables={
                    "t": TableConfig(
                        columns={
                            "tipo": ColumnConfig(
                                generator="choice", params={"values": ["piso", "mansion"]}
                            )
                        }
                    )
                }
            ),
        )
    message = str(exc.value)
    assert "t.tipo" in message
    assert "mansion" in message  # el valor inválido
    assert "piso" in message and "chalet" in message  # el dominio permitido


# --- 2. IR: enum, cotas, autoincremento, unicidad -----------------------------


def test_enum_de_la_ir_gana_a_la_heuristica() -> None:
    """Una columna 'email' con CHECK IN se resuelve como choice del enum, no como faker."""
    plan = _plan(
        "CREATE TABLE clientes (email TEXT NOT NULL CHECK (email IN ('a@x.com', 'b@x.com')));"
    )
    cp = _column(plan, "clientes", "email")
    assert cp.source == "ir"
    assert cp.generator is not None
    assert cp.generator.type == "choice"
    assert cp.generator.params["values"] == ["a@x.com", "b@x.com"]


def test_cotas_del_check_recortan_la_heuristica() -> None:
    """La heurística propone un rango amplio; el CHECK lo interseca (§7.1)."""
    plan = _plan(
        "CREATE TABLE viviendas "
        "(anio_construccion INT NOT NULL CHECK (anio_construccion BETWEEN 1900 AND 2026));"
    )
    cp = _column(plan, "viviendas", "anio_construccion")
    assert cp.source == "heuristic"
    assert cp.generator is not None
    assert cp.generator.params["min"] == 1900
    assert cp.generator.params["max"] == 2026  # recortado desde el 2100 de la heurística


def test_autoincremento_se_excluye_de_la_generacion() -> None:
    plan = _plan("CREATE TABLE t (id SERIAL PRIMARY KEY);")
    cp = _column(plan, "t", "id")
    assert cp.source == "ir"
    assert cp.generator is None
    assert any("excluye de la generación" in w for w in cp.warnings)


def test_unique_de_la_ir_fuerza_unique_en_el_generador() -> None:
    plan = _plan("CREATE TABLE clientes (email TEXT NOT NULL UNIQUE);")
    cp = _column(plan, "clientes", "email")
    assert cp.generator is not None
    assert cp.generator.unique is True


def test_unique_de_la_ir_gana_a_unique_false_del_usuario() -> None:
    plan = _plan(
        "CREATE TABLE clientes (email TEXT NOT NULL UNIQUE);",
        Config(tables={"clientes": TableConfig(columns={"email": ColumnConfig(unique=False)})}),
    )
    cp = _column(plan, "clientes", "email")
    assert cp.generator is not None
    assert cp.generator.unique is True
    assert any("UNIQUE" in w for w in cp.warnings)


def test_excluded_values_del_check_avisa_si_no_se_puede_evitar() -> None:
    plan = _plan("CREATE TABLE t (cantidad INT NOT NULL CHECK (cantidad <> 0));")
    cp = _column(plan, "t", "cantidad")
    assert cp.source == "heuristic"
    assert any("excluye" in w and "0" in w for w in cp.warnings)


# --- null_ratio ---------------------------------------------------------------


def test_null_ratio_sobre_columna_not_null_es_plan_error() -> None:
    with pytest.raises(PlanError) as exc:
        _plan(
            "CREATE TABLE t (nombre TEXT NOT NULL);",
            Config(tables={"t": TableConfig(columns={"nombre": ColumnConfig(null_ratio=0.5)})}),
        )
    message = str(exc.value)
    assert "t.nombre" in message
    assert "NOT NULL" in message


def test_null_ratio_sobre_columna_anulable_se_aplica() -> None:
    plan = _plan(
        "CREATE TABLE clientes (telefono VARCHAR(20));",
        Config(
            tables={"clientes": TableConfig(columns={"telefono": ColumnConfig(null_ratio=0.3)})}
        ),
    )
    cp = _column(plan, "clientes", "telefono")
    assert cp.generator is not None
    assert cp.generator.null_ratio == 0.3


def test_null_ratio_por_defecto_no_rompe_columna_not_null() -> None:
    """defaults.null_ratio > 0 NO fuerza NULL en columnas NOT NULL (solo aplica a anulables)."""
    plan = _plan(
        "CREATE TABLE t (nombre TEXT NOT NULL);",
        Config(defaults=Defaults(null_ratio=0.2)),
    )
    cp = _column(plan, "t", "nombre")
    assert cp.generator is not None
    assert cp.generator.null_ratio == 0.0


# --- 4. Fallback seguro con aviso ---------------------------------------------


def test_sin_ninguna_fuente_cae_al_fallback_con_aviso() -> None:
    plan = _plan("CREATE TABLE t (c1 SERIAL PRIMARY KEY, c2 VARCHAR(50) NOT NULL);")
    cp = _column(plan, "t", "c2")
    assert cp.source == "fallback"
    assert cp.generator is not None
    assert cp.generator.type == "fallback"
    assert cp.warnings, "el fallback debe dejar un aviso por columna"


def test_heuristica_bajo_umbral_cae_al_fallback() -> None:
    """Con min_confidence alto, ni una heurística correcta entra: fallback + aviso."""
    plan = _plan(
        "CREATE TABLE clientes (email TEXT NOT NULL);",
        Config(llm=LLMConfig(min_confidence=0.99)),
    )
    cp = _column(plan, "clientes", "email")
    assert cp.source == "fallback"


# --- FK: generador 'fk' con estrategia de selección ---------------------------


def test_columna_fk_sin_yaml_usa_uniform_de_la_ir() -> None:
    plan = _plan(
        "CREATE TABLE clientes (id SERIAL PRIMARY KEY);\n"
        "CREATE TABLE viviendas (id SERIAL PRIMARY KEY,\n"
        "  propietario_id INT NOT NULL REFERENCES clientes(id));"
    )
    cp = _column(plan, "viviendas", "propietario_id")
    assert cp.source == "ir"  # el defecto lo dicta la estructura, no el usuario
    assert cp.confidence == 1.0
    assert cp.role == "fk"
    assert cp.generator is not None
    assert cp.generator.type == "fk"
    assert cp.generator.params == {"strategy": "uniform"}
    assert cp.warnings == []  # sin aviso provisional: el plan queda completo


def test_columna_fk_con_estrategia_del_yaml_es_user() -> None:
    plan = _plan(
        "CREATE TABLE compraventas (id SERIAL PRIMARY KEY);\n"
        "CREATE TABLE pagos (id SERIAL PRIMARY KEY,\n"
        "  compraventa_id INT NOT NULL REFERENCES compraventas(id));",
        Config(
            tables={
                "pagos": TableConfig(
                    fk={"compraventa_id": FkQuota(strategy="quota", min=1, max=12)}
                )
            }
        ),
    )
    cp = _column(plan, "pagos", "compraventa_id")
    assert cp.source == "user"
    assert cp.generator is not None
    assert cp.generator.type == "fk"
    assert cp.generator.params == {"strategy": "quota", "min": 1, "max": 12}
    assert cp.warnings == []


def test_estrategia_en_cualquier_columna_aplica_a_toda_fk_compuesta() -> None:
    plan = _plan(
        "CREATE TABLE p (tenant INT, id INT, PRIMARY KEY (tenant, id));\n"
        "CREATE TABLE h (tenant INT, parent_id INT, "
        "FOREIGN KEY (tenant, parent_id) REFERENCES p(tenant, id));",
        Config(
            tables={"h": TableConfig(fk={"parent_id": FkQuota(strategy="quota", min=0, max=3)})}
        ),
    )
    assert _column(plan, "h", "tenant").generator == _column(plan, "h", "parent_id").generator


def test_estrategias_distintas_en_la_misma_fk_compuesta_son_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        _plan(
            "CREATE TABLE p (tenant INT, id INT, PRIMARY KEY (tenant, id));\n"
            "CREATE TABLE h (tenant INT, parent_id INT, "
            "FOREIGN KEY (tenant, parent_id) REFERENCES p(tenant, id));",
            Config(
                tables={
                    "h": TableConfig(
                        fk={
                            "tenant": FkQuota(strategy="quota", min=0, max=3),
                            "parent_id": FkQuota(strategy="quota", min=1, max=3),
                        }
                    )
                }
            ),
        )
    message = str(exc.value)
    assert "tenant" in message and "parent_id" in message


# --- opaco.sql: cero inventos -------------------------------------------------


def test_opaco_no_inventa_semantica() -> None:
    """Sobre nombres opacos, nada es 'heuristic': lo asigna la BD, es una FK o es fallback."""
    plan = _plan_schema("opaco.sql")

    generatable = 0
    for table_plan in plan.tables:
        for cp in table_plan.columns:
            assert cp.source in {"ir", "fallback"}, f"{cp.column} inventó source={cp.source}"
            if cp.source == "ir":
                # o la asigna la BD (SERIAL ⇒ generador None) o es una FK estructural
                # (generador 'fk'); en ningún caso semántica inventada sobre un nombre opaco.
                assert cp.generator is None or cp.generator.type == "fk"
            else:
                generatable += 1
                assert cp.generator is not None and cp.generator.type == "fallback"
    assert generatable > 0  # y de verdad hay columnas generables, no todas excluidas


def test_opaco_excluye_las_pk_seriales() -> None:
    plan = _plan_schema("opaco.sql")
    assert _column(plan, "t1", "c1").source == "ir"
    assert _column(plan, "t2", "c1").source == "ir"


# --- inmobiliaria + YAML de §11: plan golden y determinismo -------------------


def test_inmobiliaria_plan_golden(snapshot: object) -> None:
    config = load_config(_CONFIGS_DIR / "inmobiliaria_ejemplo.yaml")
    plan = _plan_schema("inmobiliaria.sql", config)
    assert plan.model_dump(mode="json") == snapshot


def test_plan_es_determinista_byte_a_byte() -> None:
    config = load_config(_CONFIGS_DIR / "inmobiliaria_ejemplo.yaml")
    a = _plan_schema("inmobiliaria.sql", config)
    b = _plan_schema("inmobiliaria.sql", config)
    assert canonical_json(a) == canonical_json(b)
