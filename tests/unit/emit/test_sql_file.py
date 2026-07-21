"""Tests del emisor `seed.sql` de PostgreSQL (T2.14).

Dos frentes: casos de literal/identificador construidos a mano (comillas,
apóstrofes, backslashes, Unicode, arrays vacíos, NULL, schema cualificado,
palabras reservadas) y el orden de fases end-to-end sobre los fixtures reales
(`ciclos_nullable` y `crm_real_minimo`), donde importa que el `INSERT` con
`NULL` preceda al `UPDATE` que cierra el ciclo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlglot

from synthdb.config.models import Config, FkUniform, TableConfig
from synthdb.emit import generate_files
from synthdb.emit.sql_file import ExportIntegrityError, _ident, _needs_quote, render_sql
from synthdb.generation import engine
from synthdb.generation.engine import Dataset, generate_dataset
from synthdb.ir.plans import InsertPhase, UpdatePhase
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec
from synthdb.parsing.ddl import parse_ddl

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _schema(name: str) -> SchemaSpec:
    return parse_ddl((_SCHEMAS / name).read_text("utf-8"))


# --- Identificadores: entrecomillar solo cuando el plegado lo exige ----------


def test_plain_lowercase_identifier_is_not_quoted() -> None:
    assert _needs_quote("clientes") is False
    assert _ident("clientes") == "clientes"


def test_identifiers_that_fold_or_are_reserved_are_quoted() -> None:
    # Mayúsculas (plegarían a minúsculas), espacios, acentos y palabras
    # reservadas: todas exigen comillas dobles.
    for name in ("MiTabla", "weird name", "año", "select", "user", "order"):
        assert _needs_quote(name) is True
    assert _ident("MiTabla") == '"MiTabla"'
    assert _ident("select") == '"select"'


# --- Literales: comillas, backslashes, Unicode, arrays, NULL, schema ---------


def _literal_case_schema() -> SchemaSpec:
    """Tabla `public."MiTabla"` con SERIAL, texto libre, array y palabra reservada."""
    return SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="MiTabla",
                schema_="public",
                columns=[
                    ColumnSpec(
                        name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
                    ),
                    ColumnSpec(name="texto", type=TypeSpec(kind="text"), nullable=True),
                    ColumnSpec(
                        name="tags", type=TypeSpec(kind="text", is_array=True), nullable=True
                    ),
                    ColumnSpec(name="select", type=TypeSpec(kind="integer"), nullable=True),
                ],
                primary_key=["id"],
            )
        ],
    )


def _render_literal_case(rows: list[dict[str, object]]) -> str:
    spec = _literal_case_schema()
    dataset = Dataset(tables={"MiTabla": rows}, phases=[InsertPhase(tables=["MiTabla"])])
    return render_sql(spec, dataset, Config())


def test_qualified_name_quoted_identifiers_and_autoincrement_omitted() -> None:
    sql = _render_literal_case([{"id": 1, "texto": "x", "tags": ["a"], "select": 5}])
    # Nombre cualificado con esquema y el nombre de tabla entrecomillado.
    assert 'INSERT INTO public."MiTabla"' in sql
    # `select` (reservada) va entrecomillada; `texto`/`tags` no; y la columna
    # autoincremental `id` NO aparece: la lista es exactamente esas tres.
    assert '(texto, tags, "select")' in sql


def test_string_literals_escape_quotes_backslashes_and_keep_unicode() -> None:
    value = "O'Brien " + chr(92) + " café ñ 日本"  # apóstrofe, backslash y Unicode
    sql = _render_literal_case([{"id": 1, "texto": value, "tags": ["a'b"], "select": None}])
    # La comilla simple se dobla; el backslash queda literal (standard strings);
    # el Unicode se conserva.
    assert "'O''Brien " + chr(92) + " café ñ 日本'" in sql
    # El array se emite como el literal de texto nativo de PostgreSQL (no
    # ARRAY[...]: revisión PR #42, hallazgo 4), y también escapa su comilla
    # (a nivel del literal SQL que envuelve todo el array, no del formato de
    # array en sí: una comilla simple no es un carácter reservado ahí).
    assert "'{a''b}'" in sql
    # Un valor None se emite como NULL.
    assert "NULL" in sql


def test_empty_array_is_the_untyped_empty_array_literal() -> None:
    # Revisión PR #42, hallazgo 4: SIN cast a un tipo hardcodeado (antes
    # `CAST(ARRAY[] AS TEXT[])`, incorrecto para una columna de array de
    # enum). `'{}'` es el literal de texto vacío, sin tipar: PostgreSQL lo
    # resuelve contra el tipo real de la columna destino, igual que con
    # cualquier valor escalar de esta misma función.
    sql = _render_literal_case([{"id": 1, "texto": None, "tags": [], "select": 1}])
    assert "'{}'" in sql
    assert "CAST" not in sql
    assert "ARRAY[]" not in sql


def test_header_declares_utf8_and_standard_strings() -> None:
    sql = _render_literal_case([{"id": 1, "texto": "x", "tags": ["a"], "select": 1}])
    assert "SET client_encoding = 'UTF8';" in sql
    assert "SET standard_conforming_strings = on;" in sql


# --- Orden de fases end-to-end ----------------------------------------------


def test_nullable_cycle_inserts_null_then_updates() -> None:
    spec = _schema("ciclos_nullable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=11, tables={"pedidos": TableConfig(rows=6), "facturas": TableConfig(rows=6)}),
    )
    sql = render_sql(spec, dataset, Config(seed=11))
    insert_pos = sql.index("INSERT INTO pedidos")
    update_pos = sql.index("UPDATE pedidos SET")
    # El INSERT de pedidos (con factura_id NULL) precede al UPDATE que lo enlaza.
    assert insert_pos < update_pos
    pedidos_insert = sql[insert_pos:update_pos]
    assert "NULL" in pedidos_insert  # factura_id se inserta a NULL
    assert "factura_id" in sql[update_pos:]  # y se fija después con UPDATE


def test_phases_are_wrapped_in_begin_commit() -> None:
    spec = _schema("ciclos_nullable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=11, tables={"pedidos": TableConfig(rows=4), "facturas": TableConfig(rows=4)}),
    )
    sql = render_sql(spec, dataset, Config(seed=11))
    assert sql.count("BEGIN;") == sql.count("COMMIT;")
    assert sql.count("BEGIN;") >= 2  # fase de INSERT y fase de UPDATE


def test_deferred_cycle_sets_constraints_deferred() -> None:
    spec = _schema("ciclos_deferrable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=13, tables={"pedidos": TableConfig(rows=10), "facturas": TableConfig(rows=10)}),
    )
    sql = render_sql(spec, dataset, Config(seed=13))
    assert "SET CONSTRAINTS ALL DEFERRED;" in sql


def test_multi_row_insert_respects_batch_size() -> None:
    spec = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY, v INT NOT NULL);")
    dataset = generate_dataset(
        spec,
        Config(seed=1, tables={"t": TableConfig(rows=5)}, output={"batch_size": 2}),
    )
    sql = render_sql(spec, dataset, Config(seed=1, output={"batch_size": 2}))
    # 5 filas en lotes de 2 ⇒ 3 sentencias INSERT (2 + 2 + 1).
    assert sql.count("INSERT INTO t") == 3


@pytest.mark.parametrize("fixture", ["crm_real_minimo", "ciclos_nullable"])
def test_generated_seed_sql_parses_as_postgres(fixture: str) -> None:
    # Señal fuerte sin base de datos: cada sentencia del seed.sql (SET, BEGIN,
    # INSERT multi-fila, arrays vacíos como '{}', UPDATE, COMMIT) debe parsear
    # como PostgreSQL válido. No sustituye al test @integration, pero atrapa
    # cualquier error de sintaxis antes de llegar a la BD.
    spec = _schema(f"{fixture}.sql")
    counts = (
        {"inmobiliarias": 5, "clientes": 30, "matches": 30}
        if fixture == "crm_real_minimo"
        else {"pedidos": 8, "facturas": 8}
    )
    config = Config(seed=5, tables={name: TableConfig(rows=n) for name, n in counts.items()})
    sql = render_sql(spec, generate_dataset(spec, config), config)
    statements = sqlglot.parse(sql, dialect="postgres")
    assert statements and all(statement is not None for statement in statements)


# --- Seguridad (revisión PR #42): inyección vía comentarios --------------


def test_sanitize_comment_text_replaces_every_recognised_line_break() -> None:
    from synthdb.emit.sql_file import _sanitize_comment_text

    single_char_breaks = [
        "\r",
        "\n",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    ]
    for char in single_char_breaks:
        assert _sanitize_comment_text(f"a{char}b") == "a b"
    assert _sanitize_comment_text("a\r\nb") == "a  b"  # \r y \n cada uno cuenta
    assert _sanitize_comment_text("sin saltos") == "sin saltos"


def test_table_name_with_newline_cannot_break_out_of_a_comment() -> None:
    # Reproduccion del hallazgo: un nombre de tabla con un salto de linea
    # podria, sin sanear, cerrar el comentario `-- INSERT: ...` e introducir
    # una linea SQL ejecutable fuera de cualquier identificador/literal.
    malicious = "x\nDROP TABLE victims; --"
    spec = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name=malicious,
                columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(tables={malicious: [{"id": 1}]}, phases=[InsertPhase(tables=[malicious])])
    sql = render_sql(spec, dataset, Config())

    # El comentario en si queda en UNA sola linea (el salto se sanea a
    # espacio); el payload sigue visible pero contenido, no eliminado.
    comment_lines = [line for line in sql.splitlines() if line.startswith("-- INSERT:")]
    assert len(comment_lines) == 1
    assert "DROP TABLE victims" in comment_lines[0]

    # Prueba de extremo a extremo con el parser real: el documento entero
    # produce EXACTAMENTE las sentencias esperadas (2x SET, BEGIN, INSERT,
    # COMMIT) y ninguna es un DROP. El nombre de tabla malicioso reaparece
    # como identificador citado dentro del propio INSERT (con su salto de
    # linea crudo: un identificador entre comillas dobles puede contenerlo,
    # es sintaxis SQL valida) pero eso no crea ninguna sentencia nueva.
    statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    kinds = [type(s).__name__ for s in statements]
    assert kinds == ["Set", "Set", "Transaction", "Insert", "Commit"]
    assert not any(isinstance(s, sqlglot.exp.Drop) for s in statements)


def test_column_name_with_newline_in_update_comment_is_sanitized() -> None:
    # El mismo vector existe en el comentario de una UpdatePhase, cuyo
    # `comment` interpola nombres de columna en vez de tabla.
    malicious_column = "c\nDROP TABLE victims; --"
    spec = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="t",
                columns=[
                    ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False),
                    ColumnSpec(name=malicious_column, type=TypeSpec(kind="integer"), nullable=True),
                ],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(
        tables={"t": [{"id": 1, malicious_column: 5}]},
        phases=[InsertPhase(tables=["t"]), UpdatePhase(table="t", columns=[malicious_column])],
    )
    sql = render_sql(spec, dataset, Config())

    comment_lines = [line for line in sql.splitlines() if line.startswith("-- UPDATE")]
    assert len(comment_lines) == 1
    assert "DROP TABLE victims" in comment_lines[0]

    statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    assert not any(isinstance(s, sqlglot.exp.Drop) for s in statements)


# --- Seguridad (revisión PR #42): cuarentena + SERIAL desalinea la secuencia ---


def test_contiguous_autoincrement_ids_do_not_raise() -> None:
    spec = _schema("ciclos_nullable.sql")
    dataset = generate_dataset(
        spec,
        Config(seed=11, tables={"pedidos": TableConfig(rows=6), "facturas": TableConfig(rows=6)}),
    )
    render_sql(spec, dataset, Config(seed=11))  # no debe lanzar


def test_gapped_autoincrement_ids_raise_export_integrity_error() -> None:
    # Reproducción mínima y directa (sin motor): una tabla con columna
    # autoincremental cuyas filas aceptadas dejan un hueco intermedio.
    spec = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="parent",
                columns=[
                    ColumnSpec(
                        name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
                    ),
                ],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(
        tables={"parent": [{"id": 1}, {"id": 3}, {"id": 4}]},  # falta el id 2
        phases=[InsertPhase(tables=["parent"])],
    )
    with pytest.raises(ExportIntegrityError, match=r"tabla parent, columna id"):
        render_sql(spec, dataset, Config())


def test_empty_table_with_autoincrement_column_does_not_raise() -> None:
    spec = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="t",
                columns=[
                    ColumnSpec(
                        name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False
                    ),
                ],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(tables={"t": []}, phases=[InsertPhase(tables=["t"])])
    render_sql(spec, dataset, Config())  # no debe lanzar: no hay filas que comprobar


def test_quarantined_intermediate_serial_row_with_surviving_fk_blocks_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Reproducción fiel end-to-end (revisión PR #42, hallazgo 3): parent
    # genera ids [1, 2, 3]; se cuarentena la fila 2 (intermedia, NOT NULL
    # violado) y sobreviven [1, 3] -un hueco-. Varias filas de child
    # sobreviven referenciando parent_id=3 (FK íntegra EN MEMORIA: la fila 3
    # sigue existiendo). Si se exportara sin más, un PostgreSQL recién
    # cargado asignaría ids [1, 2] a esas dos filas de parent (se omite la
    # columna autoincremental del INSERT) -la que ERA id=3 pasaría a ser
    # id=2- y las FK de child con parent_id=3 quedarían colgando: cero filas
    # de parent con id=3 tras la carga. render_sql debe rechazarlo ANTES de
    # escribir nada, en vez de producir ese seed.sql roto.
    spec = parse_ddl(
        "CREATE TABLE parent (id SERIAL PRIMARY KEY, value INT NOT NULL);"
        "CREATE TABLE child (id SERIAL PRIMARY KEY, parent_id INT NOT NULL "
        "REFERENCES parent(id));"
    )

    def corrupt(batch: list[dict[str, Any]]) -> None:
        for row in batch:
            if "value" in row and row.get("id") == 2:
                row["value"] = None

    monkeypatch.setattr(engine, "complete_batch", corrupt)
    config = Config(
        seed=7,
        tables={
            "parent": TableConfig(rows=3),
            "child": TableConfig(rows=6, fk={"parent_id": FkUniform(strategy="uniform")}),
        },
    )
    dataset = generate_dataset(spec, config)

    # Precondiciones del escenario: exactamente la fila 2 de parent en
    # cuarentena, ningún child en cuarentena, y al menos un child que SÍ
    # referencia el id superviviente posterior al hueco (parent_id=3).
    assert [row["id"] for row, _, _ in dataset.quarantine.get("parent", [])] == [2]
    assert dataset.quarantine.get("child") is None
    assert 3 in {row["parent_id"] for row in dataset.tables["child"]}

    with pytest.raises(ExportIntegrityError, match=r"tabla parent, columna id"):
        render_sql(spec, dataset, config)

    # CSV/JSON no tienen este problema: cada fila lleva su id, así que un
    # cliente que los cargue con sus propias herramientas conserva 1/3, no
    # depende de una secuencia SERIAL para reasignarlos.
    csv_paths = generate_files(spec, dataset, tmp_path, "csv")
    assert {p.name for p in csv_paths} == {"parent.csv", "child.csv"}


# --- Seguridad (revisión PR #42): arrays de enum -----------------------------


_TRICKY_ENUM_LABEL = "O'Brien, " + chr(92) + "special" + chr(92)  # comilla + 2 backslashes


def _enum_array_schema() -> SchemaSpec:
    """Tabla con `mood` (columna de array de un enum), tal como la IR la
    representa: `type.kind == "enum"`, `type.is_array == True` y las
    ETIQUETAS en `enum_values` (nunca el nombre `CREATE TYPE`, que la IR no
    conserva)."""
    return SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="t",
                columns=[
                    ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False),
                    ColumnSpec(
                        name="mood",
                        type=TypeSpec(kind="enum", is_array=True),
                        nullable=True,
                        enum_values=["happy", "sad", _TRICKY_ENUM_LABEL, "café ñ 日本"],
                    ),
                ],
                primary_key=["id"],
            )
        ],
    )


def test_empty_enum_array_is_untyped_not_cast_to_text() -> None:
    # El hallazgo original: CAST(ARRAY[] AS TEXT[]) sobre una columna de array
    # de enum es un tipo INCORRECTO (PostgreSQL no castea implícitamente
    # text[] a un enum[] de usuario). El literal de texto '{}' sin tipar deja
    # que PostgreSQL lo resuelva contra el tipo REAL de la columna.
    spec = _enum_array_schema()
    dataset = Dataset(tables={"t": [{"id": 1, "mood": []}]}, phases=[InsertPhase(tables=["t"])])
    sql = render_sql(spec, dataset, Config())
    assert "'{}'" in sql
    assert "TEXT[]" not in sql
    assert "CAST" not in sql


def test_non_empty_enum_array_escapes_special_characters_in_labels() -> None:
    spec = _enum_array_schema()
    dataset = Dataset(
        tables={"t": [{"id": 1, "mood": ["happy", _TRICKY_ENUM_LABEL, "café ñ 日本"]}]},
        phases=[InsertPhase(tables=["t"])],
    )
    sql = render_sql(spec, dataset, Config())
    # El array entero es UN literal de texto de PostgreSQL (revisión PR #42,
    # hallazgo 4: ni ARRAY[...] ni CAST). La etiqueta con coma/espacio/
    # backslashes va entrecomillada dentro del formato de array (backslash
    # doblado por ESE formato) y la comilla simple de "O'Brien" se dobla por
    # el literal SQL que envuelve todo el array; la etiqueta Unicode va
    # entrecomillada por sus espacios, sin necesitar más escapado.
    expected = "'{happy,\"O''Brien, " + chr(92) * 2 + "special" + chr(92) * 2 + '","café ñ 日本"}\''
    assert expected in sql
    statements = sqlglot.parse(sql, dialect="postgres")
    assert statements and all(statement is not None for statement in statements)
