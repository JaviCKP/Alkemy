"""Tests de `config/models.py` + `config/loader.py` (T2.5, especificacion.md §11).

El criterio de aceptación de la tarea: el YAML del ejemplo de §11 carga
completo; un campo desconocido produce un error con su ruta exacta; un YAML mal
formado produce un error con línea y columna. Todo lo demás cuelga de ahí.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthdb.config.loader import ConfigError, load_config, load_config_text
from synthdb.config.models import Config, FkQuota, FkZipf

_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
_EJEMPLO = _CONFIGS_DIR / "inmobiliaria_ejemplo.yaml"


# --- El ejemplo completo de §11 carga -----------------------------------------


def test_ejemplo_de_la_especificacion_carga_completo() -> None:
    config = load_config(_EJEMPLO)

    assert config.version == 1
    assert config.seed == 42
    assert config.locale == "es_ES"
    assert config.dialect == "postgres"

    # llm (§11): se parsea entero aunque no tenga efecto hasta el H3.
    assert config.llm.enabled is True
    assert config.llm.provider == "ollama"
    assert config.llm.model == "qwen2.5:7b-instruct"
    assert config.llm.min_confidence == 0.7
    assert config.llm.allow_data_sampling is False

    assert config.defaults.rows == 100
    assert config.defaults.null_ratio == 0.0

    # tables + override de columna del usuario.
    assert config.tables["clientes"].rows == 500
    viviendas = config.tables["viviendas"]
    assert viviendas.rows == 800
    superficie = viviendas.columns["superficie_m2"]
    assert superficie.generator == "numeric_range"
    assert superficie.params["min"] == 35
    assert superficie.params["distribution"]["family"] == "lognormal"

    assert config.refs["precio_m2_base"] == 2350
    assert config.hierarchy["empleados.manager_id"].branching == 6
    assert config.hierarchy["empleados.manager_id"].max_depth == 4

    assert config.output.batch_size == 5000
    assert config.output.on_error == "quarantine"
    assert config.output.max_repair_retries == 3


def test_fk_del_ejemplo_valida_su_forma() -> None:
    """`fk` valida la forma de cada estrategia (§7.4); el selector es de la sesión C."""
    config = load_config(_EJEMPLO)

    vivienda_fk = config.tables["compraventas"].fk["vivienda_id"]
    assert isinstance(vivienda_fk, FkQuota)
    assert (vivienda_fk.min, vivienda_fk.max) == (0, 2)

    comprador_fk = config.tables["compraventas"].fk["comprador_id"]
    assert isinstance(comprador_fk, FkZipf)
    assert comprador_fk.s == 1.3


def test_rules_se_guardan_sin_interpretar() -> None:
    """Las reglas del mini-DSL se conservan como cadenas literales (sesión D)."""
    config = load_config(_EJEMPLO)

    rules = config.tables["compraventas"].rules
    assert rules == [
        "fecha >= date(parent(vivienda_id).anio_construccion, 1, 1)",
        "precio = parent(vivienda_id).superficie_m2 * ref('precio_m2_base') * noise(0.2)",
    ]


# --- Valores por defecto ------------------------------------------------------


def test_config_minima_usa_defectos() -> None:
    config = load_config_text("version: 1\n")

    assert config.seed == 0
    assert config.locale == "es_ES"
    assert config.dialect == "postgres"
    assert config.llm.enabled is False
    assert config.llm.min_confidence == 0.7
    assert config.defaults.rows == 100
    assert config.output.batch_size == 5000
    assert config.tables == {}


def test_yaml_vacio_es_config_por_defecto() -> None:
    assert load_config_text("") == Config()


# --- Campo desconocido: error con ruta exacta ---------------------------------


def test_campo_desconocido_en_columna_reporta_ruta_exacta() -> None:
    text = """
    tables:
      viviendas:
        columns:
          superficie_m2:
            generador: numeric_range
    """
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    message = str(exc.value)
    assert "tables.viviendas.columns.superficie_m2.generador" in message


def test_campo_desconocido_en_raiz_reporta_ruta() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_text("semilla: 42\n")

    assert "semilla" in str(exc.value)


def test_estrategia_fk_desconocida_es_error() -> None:
    text = """
    tables:
      compraventas:
        fk:
          vivienda_id: {strategy: piramidal}
    """
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    assert "tables.compraventas.fk.vivienda_id" in str(exc.value)


def test_fk_quota_min_mayor_que_max_es_error() -> None:
    text = """
    tables:
      compraventas:
        fk:
          vivienda_id: {strategy: quota, min: 5, max: 2}
    """
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    assert "min" in str(exc.value)


def test_null_ratio_fuera_de_rango_es_error() -> None:
    text = """
    defaults:
      null_ratio: 1.5
    """
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    assert "defaults.null_ratio" in str(exc.value)


def test_on_error_invalido_reporta_ruta() -> None:
    text = """
    output:
      on_error: explotar
    """
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    assert "output.on_error" in str(exc.value)


# --- YAML mal formado: error con línea/columna --------------------------------


def test_yaml_malformado_reporta_linea_y_columna() -> None:
    # El corchete abierto sin cerrar rompe el flujo en la línea 2.
    text = "tables:\n  viviendas: {rows: 800\n"
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)

    message = str(exc.value)
    assert "mal formado" in message
    assert "línea" in message and "columna" in message


def test_raiz_no_mapa_es_error() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_text("- 1\n- 2\n")

    assert "mapa" in str(exc.value)


def test_archivo_inexistente_es_error() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_CONFIGS_DIR / "no_existe.yaml")

    assert "no se pudo leer" in str(exc.value)
