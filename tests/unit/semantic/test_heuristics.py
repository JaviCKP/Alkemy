"""Tests de `semantic/heuristics.py` (T2.4, especificacion.md §7.1).

Tres frentes:

- una **tabla de casos** (≥ 40) nombre+tipo ⇒ (rol, generador) esperados, que es
  la definición operativa de qué reconoce cada patrón;
- el **orden** del diccionario como contrato (un patrón específico gana a uno
  genérico), porque de ahí depende que `codigo_postal` no acabe en `codigo`;
- una **métrica** contra las labels del H0 (fixtures 1–5) con el umbral del plan
  (≥ 60 %), marcada `metric` para separarla de la suite unitaria.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from synthdb.constraints.check_interp import interpret_checks
from synthdb.ir.schema import ColumnSpec, TableSpec, TypeSpec
from synthdb.parsing.ddl import parse_ddl
from synthdb.semantic.heuristics import infer_column, patterns

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
_LABELS_DIR = Path(__file__).resolve().parents[3] / "experiments" / "00_llm_plan" / "labels"


def _column(name: str, kind: str, **type_kwargs: Any) -> ColumnSpec:
    return ColumnSpec(name=name, type=TypeSpec(kind=kind, **type_kwargs), nullable=True)


def _in(table_name: str, column: ColumnSpec) -> Any:
    return infer_column(TableSpec(name=table_name, columns=[column]), column)


# --- Tabla de casos: nombre + tipo ⇒ (rol, generador) --------------------------
# (tabla, columna, kind, type_kwargs, rol_esperado, generador_esperado)
_CASES: list[tuple[str, str, str, dict[str, Any], str, str]] = [
    ("clientes", "email", "text", {}, "email", "faker"),
    ("clientes", "correo_electronico", "varchar", {"length": 120}, "email", "faker"),
    ("empresas", "iban", "varchar", {"length": 34}, "iban", "faker"),
    ("personas", "dni", "char", {"length": 9}, "documento_identidad", "faker"),
    ("personas", "nif", "varchar", {"length": 9}, "documento_identidad", "faker"),
    ("direcciones", "codigo_postal", "varchar", {"length": 5}, "codigo_postal", "faker"),
    ("direcciones", "cp", "varchar", {"length": 5}, "codigo_postal", "faker"),
    ("cuentas", "username", "varchar", {"length": 30}, "usuario", "faker"),
    ("cuentas", "login", "varchar", {"length": 30}, "usuario", "faker"),
    ("servidores", "ip_address", "varchar", {"length": 45}, "ip", "faker"),
    ("recursos", "url", "text", {}, "url", "faker"),
    ("recursos", "web", "varchar", {"length": 200}, "url", "faker"),
    ("perfiles", "avatar", "text", {}, "imagen", "faker"),
    ("contactos", "telefono_movil", "varchar", {"length": 20}, "telefono_movil", "faker"),
    ("contactos", "telefono", "varchar", {"length": 20}, "telefono", "faker"),
    ("contactos", "phone", "varchar", {"length": 20}, "telefono", "faker"),
    ("personas", "apellidos", "text", {}, "apellidos", "faker"),
    ("personas", "surname", "text", {}, "apellidos", "faker"),
    ("personas", "nombre_completo", "text", {}, "nombre_persona", "faker"),
    ("clientes", "empresa", "text", {}, "empresa", "faker"),
    ("empleados", "puesto", "text", {}, "puesto", "faker"),
    ("empleados", "cargo", "text", {}, "puesto", "faker"),
    ("clientes", "nombre", "text", {}, "nombre_persona", "faker"),
    ("productos", "nombre", "text", {}, "nombre_producto", "faker"),
    ("viviendas", "direccion", "text", {}, "direccion", "faker"),
    ("direcciones", "ciudad", "varchar", {"length": 80}, "ciudad", "faker"),
    ("direcciones", "provincia", "varchar", {"length": 80}, "provincia", "faker"),
    ("direcciones", "pais", "varchar", {"length": 2}, "pais", "faker"),
    ("direcciones", "nacionalidad", "text", {}, "pais", "faker"),
    ("cuentas", "moneda", "char", {"length": 3}, "moneda", "faker"),
    ("productos", "color", "varchar", {"length": 30}, "color", "faker"),
    ("vehiculos", "matricula", "varchar", {"length": 10}, "matricula", "faker"),
    ("articulos", "slug", "varchar", {"length": 120}, "slug", "faker"),
    ("libros", "titulo", "text", {}, "titulo", "faker"),
    ("reparaciones", "descripcion", "text", {}, "descripcion", "template"),
    ("reparaciones", "observaciones", "text", {}, "descripcion", "template"),
    ("sepulturas", "codigo", "varchar", {"length": 20}, "codigo", "template"),
    ("pedidos", "referencia", "varchar", {"length": 20}, "codigo", "template"),
    ("usuarios", "password", "varchar", {"length": 255}, "password", "template"),
    ("usuarios", "password_hash", "varchar", {"length": 255}, "password", "template"),
    ("personas", "edad", "integer", {}, "edad", "numeric_range"),
    ("viviendas", "anio_construccion", "integer", {}, "anio", "numeric_range"),
    (
        "descuentos",
        "porcentaje",
        "numeric",
        {"precision": 5, "scale": 2},
        "porcentaje",
        "numeric_range",
    ),
    ("ubicaciones", "latitud", "numeric", {"precision": 9, "scale": 6}, "latitud", "numeric_range"),
    (
        "ubicaciones",
        "longitud",
        "numeric",
        {"precision": 9, "scale": 6},
        "longitud",
        "numeric_range",
    ),
    ("productos", "precio", "numeric", {"precision": 10, "scale": 2}, "precio", "numeric_range"),
    ("pagos", "importe", "numeric", {"precision": 12, "scale": 2}, "importe", "numeric_range"),
    ("nominas", "salario", "numeric", {"precision": 10, "scale": 2}, "salario", "numeric_range"),
    ("lineas", "cantidad", "integer", {}, "cantidad", "numeric_range"),
    ("productos", "stock", "integer", {}, "cantidad", "numeric_range"),
    ("sepulturas", "capacidad", "integer", {}, "capacidad", "numeric_range"),
    (
        "viviendas",
        "superficie_m2",
        "numeric",
        {"precision": 7, "scale": 2},
        "superficie",
        "numeric_range",
    ),
    ("personas", "fecha_nacimiento", "date", {}, "fecha_nacimiento", "datetime_range"),
    ("productos", "fecha_caducidad", "date", {}, "fecha_caducidad", "datetime_range"),
    ("clientes", "fecha_alta", "date", {}, "fecha_alta", "datetime_range"),
    ("compraventas", "fecha", "date", {}, "fecha", "datetime_range"),
    ("pedidos", "updated_at", "timestamp", {}, "fecha", "datetime_range"),
    ("productos", "activo", "boolean", {}, "booleano", "choice"),
    ("clientes", "es_vip", "boolean", {}, "booleano", "choice"),
    ("eventos", "uuid", "uuid", {}, "uuid", "uuid"),
    ("clientes", "id", "integer", {}, "identificador", "sequence"),
    ("viviendas", "propietario_id", "integer", {}, "fk", "sequence"),
    ("eventos", "id", "uuid", {}, "identificador", "uuid"),
]


@pytest.mark.parametrize(
    "table_name,col_name,kind,type_kwargs,expected_role,expected_generator",
    _CASES,
    ids=[f"{c[0]}.{c[1]}:{c[2]}" for c in _CASES],
)
def test_case_table(
    table_name: str,
    col_name: str,
    kind: str,
    type_kwargs: dict[str, Any],
    expected_role: str,
    expected_generator: str,
) -> None:
    result = _in(table_name, _column(col_name, kind, **type_kwargs))

    assert result is not None, f"{col_name}:{kind} no casó con ningún patrón"
    assert result.role == expected_role
    assert result.generator.type == expected_generator
    assert 0.6 <= result.confidence <= 0.95


def test_al_menos_40_patrones() -> None:
    names = patterns()
    assert len(names) >= 40
    assert len(names) == len(set(names)), "hay nombres de patrón duplicados"


def test_confianzas_no_son_todas_iguales() -> None:
    """Las confianzas deben estar calibradas por patrón, no ser un 0.9 uniforme."""
    confidences = {
        result.confidence
        for name, col, kind, kw, _role, _gen in _CASES
        if (result := _in(name, _column(col, kind, **kw))) is not None
    }
    assert len(confidences) >= 4


# --- El orden del diccionario es contrato -------------------------------------


def _order(name: str) -> int:
    return patterns().index(name)


def test_patrones_especificos_ganan_a_los_genericos() -> None:
    assert _order("codigo_postal") < _order("codigo")
    assert _order("fecha_nacimiento") < _order("fecha")
    assert _order("fecha_caducidad") < _order("fecha")
    assert _order("fecha_alta") < _order("fecha")
    assert _order("usuario") < _order("nombre")
    assert _order("email") < _order("nombre")
    assert _order("nombre_completo") < _order("nombre")
    assert _order("telefono_movil") < _order("telefono")


def test_identificador_es_el_ultimo_recurso() -> None:
    """`id`/`*_id` va al final: solo gana cuando ningún patrón semántico casa."""
    assert patterns()[-1] == "identificador"


@pytest.mark.parametrize(
    "col_name,kind,expected_role",
    [
        ("codigo_postal", "varchar", "codigo_postal"),  # no "codigo"
        ("fecha_nacimiento", "date", "fecha_nacimiento"),  # no "fecha"
        ("nombre_usuario", "varchar", "usuario"),  # no "nombre"
        ("email_secundario", "varchar", "email"),  # no "nombre"/"codigo"
    ],
)
def test_orden_resuelve_solapamientos(col_name: str, kind: str, expected_role: str) -> None:
    result = _in(
        "t", _column(col_name, kind, length=50) if kind != "date" else _column(col_name, kind)
    )
    assert result is not None
    assert result.role == expected_role


# --- Sin match, tipos incompatibles y marcadores inertes ----------------------


@pytest.mark.parametrize("name", ["c1", "c2", "c7", "cod_x", "val", "xyz", "campo_raro"])
def test_nombres_opacos_no_casan(name: str) -> None:
    """Nombres sin contenido (como los de `opaco.sql`) devuelven None, no inventos."""
    assert _in("t1", _column(name, "varchar", length=50)) is None
    assert _in("t1", _column(name, "integer")) is None


def test_tipo_incompatible_no_casa() -> None:
    """Un patrón exige nombre Y tipo: `email` en una columna entera no casa."""
    assert _in("clientes", _column("email", "integer")) is None
    assert _in("personas", _column("edad", "text")) is None


@pytest.mark.parametrize(
    "name", ["codigo", "referencia", "siguiente_referencia", "serial", "folio"]
)
def test_codigo_integer_usa_sequence(name: str) -> None:
    result = _in("inmobiliarias", _column(name, "integer"))

    assert result is not None
    assert result.role == "codigo"
    assert result.generator.type == "sequence"


@pytest.mark.parametrize("kind", ["text", "varchar"])
@pytest.mark.parametrize(
    "name", ["codigo", "referencia", "siguiente_referencia", "serial", "folio"]
)
def test_codigo_textual_sigue_usando_template(name: str, kind: str) -> None:
    type_kwargs = {"length": 50} if kind == "varchar" else {}
    result = _in("inmobiliarias", _column(name, kind, **type_kwargs))

    assert result is not None
    assert result.role == "codigo"
    assert result.generator.type == "template"


def test_password_nunca_es_faker() -> None:
    """`password`/`hash` producen un marcador inerte, jamás un valor de Faker (privacidad)."""
    for name in ("password", "passwd", "user_hash", "api_key"):
        result = _in("usuarios", _column(name, "varchar", length=255))
        assert result is not None
        assert result.generator.type == "template"
        assert result.generator.type != "faker"


def test_pais_de_dos_caracteres_es_iso_alfa2() -> None:
    """`pais varchar(2)` ⇒ código ISO alfa-2; `pais text` ⇒ nombre de país (§7.1)."""
    iso = _in("d", _column("pais", "varchar", length=2))
    assert iso is not None and iso.generator.params["provider"] == "country_code"
    full = _in("d", _column("pais", "text"))
    assert full is not None and full.generator.params["provider"] == "country"


# --- Métrica contra las labels del H0 (fixtures 1-5) --------------------------

_STOPWORDS = {"de", "del", "la", "el", "en", "y", "o", "un", "una", "para", "que"}
_FIXTURES_1_5 = ["inmobiliaria", "cementerio", "taller", "ecommerce", "rrhh_autoref_nullable"]


def _keywords(text: str) -> set[str]:
    words = re.split(r"[^a-záéíóúñ0-9]+", text.lower())
    return {w for w in words if w and w not in _STOPWORDS}


def _load_labels(fixture: str) -> dict[str, Any]:
    data: dict[str, Any] = YAML(typ="safe").load(
        (_LABELS_DIR / f"{fixture}.yaml").read_text("utf-8")
    )
    return data


@pytest.mark.metric
def test_exactitud_heuristicas_contra_labels_h0() -> None:
    """Réplica de la métrica del H0 (compute_metrics.py) solo con heurísticas.

    Exactitud de generador = pertenencia al conjunto `acceptable_generators`;
    exactitud de rol = solapamiento de palabras clave con el `role` de la label,
    excluyendo columnas etiquetadas `desconocido` (igual que el H0). El criterio
    de aceptación de T2.4 es ≥ 60 % sobre los fixtures 1–5.
    """
    role_hits = role_total = gen_hits = gen_total = 0
    for fixture in _FIXTURES_1_5:
        labels = _load_labels(fixture)
        spec = interpret_checks(parse_ddl((_SCHEMAS_DIR / f"{fixture}.sql").read_text("utf-8")))
        for table in spec.tables:
            table_labels = labels.get("tables", {}).get(table.name, {}).get("columns", {})
            for column in table.columns:
                label = table_labels.get(column.name)
                if label is None:
                    continue
                result = infer_column(table, column)
                predicted_gen = result.generator.type if result is not None else None
                predicted_role = result.role if result is not None else ""

                gen_total += 1
                gen_hits += int(predicted_gen in label.get("acceptable_generators", []))

                if label.get("role") != "desconocido":
                    role_total += 1
                    role_hits += int(bool(_keywords(label["role"]) & _keywords(predicted_role)))

    role_accuracy = role_hits / role_total
    gen_accuracy = gen_hits / gen_total
    assert role_accuracy >= 0.60, (
        f"exactitud de rol {role_accuracy:.0%} < 60% ({role_hits}/{role_total})"
    )
    assert gen_accuracy >= 0.60, f"exactitud de generador {gen_accuracy:.0%} < 60%"
