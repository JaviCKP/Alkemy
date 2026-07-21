"""Tests de seguridad de nombres de archivo CSV/JSON (revisión PR #42, hallazgos 2 y R3-1).

`out_dir / f"{table.name}.csv"` permitía (a) escribir fuera de `--out` con un
nombre como `"../escaped"` y (b) que dos tablas PostgreSQL que solo difieren en
mayúsculas (`foo` y `"FOO"`) se pisaran en Windows/macOS, donde el sistema de
archivos es insensible a mayúsculas. Estos tests cubren la codificación segura
e **inyectiva bajo comparación insensible a mayúsculas** (`_safe_table_filename`),
la contención dentro de `out_dir` (`_resolve_safe_path`) y la validación de
colisiones case-insensitive antes de escribir el primer archivo
(`validate_table_filenames`, invocada desde `generate_files`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthdb.emit import EmitPathError, generate_files
from synthdb.emit.csv_json import (
    CsvSink,
    _resolve_safe_path,
    _safe_table_filename,
    validate_table_filenames,
)
from synthdb.generation.engine import Dataset
from synthdb.ir.plans import InsertPhase
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec


def _table(name: str, schema: str | None = None) -> TableSpec:
    return TableSpec(
        name=name,
        schema=schema,
        columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
        primary_key=["id"],
    )


def _fname(name: str, schema: str | None = None, ext: str = "csv") -> str:
    return _safe_table_filename(_table(name, schema), ext)


# --- Caso normal y codificación ---------------------------------------------


def test_normal_lowercase_table_name_is_unchanged() -> None:
    # Comportamiento del caso común preservado a propósito.
    assert _fname("clientes") == "clientes.csv"
    assert _fname("clientes", ext="json") == "clientes.json"
    assert _fname("pedidos_uk") == "pedidos_uk.csv"


@pytest.mark.parametrize("ext", ["csv", "json"])
def test_maximum_postgres_identifier_names_write_physical_files(tmp_path: Path, ext: str) -> None:
    # Cada identificador ocupa 62 bytes UTF-8: es válido para PostgreSQL y
    # fuerza el límite de longitud sin depender de nombres ASCII cortos.
    schema = "ñ" * 31
    table_name = "é" * 31
    assert len(schema.encode("utf-8")) == 62
    assert len(table_name.encode("utf-8")) == 62

    out_dir = tmp_path / "out"
    spec = SchemaSpec(dialect="postgres", tables=[_table(table_name, schema)])
    dataset = Dataset(
        tables={table_name: [{"id": 31}]},
        phases=[InsertPhase(tables=[table_name])],
    )

    paths = generate_files(spec, dataset, out_dir, ext)

    assert len(paths) == 1
    path = paths[0]
    assert path.exists()
    assert len(path.name.encode("utf-8")) <= 255
    assert len(list(out_dir.glob(f"*.{ext}"))) == 1
    if ext == "csv":
        assert "31" in path.read_text(encoding="utf-8")
    else:
        assert json.loads(path.read_text(encoding="utf-8")) == [{"id": 31}]


@pytest.mark.parametrize(
    "malicious_name",
    ["../escaped", "..\\escaped", "../../etc/passwd", "a/b", "a" + chr(92) + "b", "..", "."],
)
def test_path_traversal_names_are_encoded_away(malicious_name: str) -> None:
    filename = _fname(malicious_name)
    assert "/" not in filename
    assert "\\" not in filename
    assert ".." not in filename.removesuffix(".csv")


def test_control_characters_unicode_and_percent_are_base32_encoded() -> None:
    filename = _fname("a\x00b\tc%d")
    assert "\x00" not in filename
    assert "\t" not in filename
    assert filename.startswith("~")
    assert "%" not in filename


def test_encoding_output_is_entirely_lowercase() -> None:
    # La salida en minúsculas es lo que garantiza la inyectividad bajo
    # comparación insensible a mayúsculas: casefold es la identidad.
    for name in ["Foo", "FOO", "CamelCase", "MiXeD_123", "ÑOÑO"]:
        filename = _fname(name)
        assert filename == filename.casefold()


# --- Inyectividad bajo comparación insensible a mayúsculas (R3-1) ------------


def test_case_only_variants_produce_casefold_distinct_filenames() -> None:
    # El bug original: en Windows, foo.csv y FOO.csv son el mismo archivo.
    variants = ["foo", "Foo", "FOO", "fOo"]
    casefolded = {_fname(v).casefold() for v in variants}
    assert len(casefolded) == len(variants)
    assert _fname("foo") == "foo.csv"  # el normal minúsculas no cambia


def test_schema_case_only_variants_are_casefold_distinct() -> None:
    # schemas a.foo / A.foo deben producir archivos distintos también en NTFS.
    assert _fname("foo", schema="a").casefold() != _fname("foo", schema="A").casefold()
    # y esquemas distintos con la misma tabla, también.
    assert _fname("foo", schema="a").casefold() != _fname("foo", schema="b").casefold()


def test_many_distinct_names_never_casefold_collide() -> None:
    names = [
        "clientes",
        "../clientes",
        "CLIENTES",
        "Clientes",
        "cliéntes",
        "cli%65ntes",
        "cli.entes",
    ]
    casefolded = {_fname(n).casefold() for n in names}
    assert len(casefolded) == len(names)


def test_schema_qualified_does_not_collide_with_same_named_table() -> None:
    keys = {
        _fname("foo").casefold(),
        _fname("foo", schema="a").casefold(),
        _fname("foo", schema="b").casefold(),
    }
    assert len(keys) == 3


# --- Nombres reservados de Windows: sin prefijo que pueda colisionar --------


@pytest.mark.parametrize("reserved", ["con", "prn", "aux", "nul", "com1", "lpt9"])
def test_reserved_device_name_is_not_a_prefixed_real_name(reserved: str) -> None:
    # Un nombre reservado en minúsculas NO se resuelve como `_con` (que
    # colisionaría con una tabla real `_con`), sino codificando el componente
    # completo con el marcador reservado para formas no literales.
    reserved_file = _fname(reserved)
    stem = reserved_file.removesuffix(".csv")
    # El primer componente (antes del primer punto) ya no es un dispositivo.
    assert stem.split(".")[0].casefold() not in {"con", "prn", "aux", "nul", "com1", "lpt9"}
    assert stem != "_" + reserved  # explícitamente: NO es el prefijo `_`
    # Y no colisiona (case-insensitive) con la tabla real `_<reservado>`.
    real = _fname("_" + reserved)
    assert reserved_file.casefold() != real.casefold()


def test_uppercase_quoted_reserved_name_is_distinct_from_lowercase_and_underscore() -> None:
    # `"CON"` (citada, IR name "CON") vs `CON`→`con` vs `_CON`/`_con`.
    keys = {
        _fname("CON").casefold(),
        _fname("con").casefold(),
        _fname("_con").casefold(),
        _fname("_CON").casefold(),
    }
    assert len(keys) == 4


def test_reserved_schema_first_component_is_escaped() -> None:
    # schema `con` cualificando `foo`: en Windows `con.foo.csv` trataría `con`
    # como dispositivo. El primer componente se codifica completo.
    stem = _fname("foo", schema="con").removesuffix(".csv")
    assert stem.split(".")[0].casefold() not in {"con", "prn", "aux", "nul"}


# --- Contención dentro de out_dir -------------------------------------------


def test_resolved_path_stays_within_out_dir(tmp_path: Path) -> None:
    path = _resolve_safe_path(tmp_path, _table("../escaped"), "csv")
    assert path.resolve().parent == tmp_path.resolve()


def test_csv_sink_writes_inside_out_dir_for_traversal_name(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    sink = CsvSink(out_dir)
    sink.write_table(_table("../escaped"), [{"id": 1}])
    written = sink.paths[0]
    assert written.resolve().parent == out_dir.resolve()
    assert written.exists()
    assert not (tmp_path / "escaped.csv").exists()
    assert not (tmp_path.parent / "escaped.csv").exists()


# --- Validación de colisiones ANTES de escribir -----------------------------


def test_validate_raises_emit_path_error_on_genuine_collision(tmp_path: Path) -> None:
    with pytest.raises(EmitPathError, match="mismo archivo de salida"):
        validate_table_filenames([_table("dup"), _table("dup")], tmp_path, "csv")


def test_validate_accepts_case_variants_as_distinct(tmp_path: Path) -> None:
    # foo y FOO NO colisionan con la codificación nueva (antes sí, en NTFS).
    validate_table_filenames([_table("foo"), _table("FOO")], tmp_path, "csv")  # no raise


def test_generate_files_rejects_collision_before_writing_anything(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    spec = SchemaSpec(dialect="postgres", tables=[_table("a"), _table("b"), _table("a")])
    dataset = Dataset(
        tables={"a": [{"id": 1}], "b": [{"id": 2}]}, phases=[InsertPhase(tables=["a", "b"])]
    )
    with pytest.raises(EmitPathError, match="mismo archivo de salida"):
        generate_files(spec, dataset, out_dir, "csv")
    # Ningún archivo parcial.
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


# --- Dos archivos reales con contenido correcto (bug NTFS/APFS) --------------


@pytest.mark.parametrize("ext", ["csv", "json"])
def test_case_variant_tables_write_two_real_files_with_correct_content(
    tmp_path: Path, ext: str
) -> None:
    # El corazón del hallazgo: en un FS insensible a mayúsculas (Windows/macOS)
    # las tablas `foo` y `FOO` DEBEN producir dos archivos físicos distintos,
    # cada uno con SU contenido. Con la codificación anterior, en Windows
    # quedaba un solo `foo.csv` con las filas de la 2ª tabla.
    out_dir = tmp_path / "out"
    spec = SchemaSpec(dialect="postgres", tables=[_table("foo"), _table("FOO")])
    dataset = Dataset(
        tables={"foo": [{"id": 11}], "FOO": [{"id": 22}]},
        phases=[InsertPhase(tables=["foo", "FOO"])],
    )
    paths = generate_files(spec, dataset, out_dir, ext)

    on_disk = sorted(out_dir.glob(f"*.{ext}"))
    assert len(on_disk) == 2, on_disk  # dos archivos físicos, ninguno pisado
    assert len({p.name.casefold() for p in on_disk}) == 2

    contents = {p.name: p.read_text(encoding="utf-8") for p in on_disk}
    # `foo` (minúsculas) conserva su nombre literal.
    assert "foo." + ext in contents
    # Cada archivo lleva SU id, no el de la otra tabla.
    lower_file = next(p for p in paths if p.name == "foo." + ext)
    upper_file = next(p for p in paths if p.name != "foo." + ext)
    assert "11" in lower_file.read_text(encoding="utf-8")
    assert "22" in upper_file.read_text(encoding="utf-8")
    assert lower_file.name.casefold() != upper_file.name.casefold()
