"""Test trivial de humo: el paquete se instala y se puede importar (T0.4)."""

import synthdb
from synthdb.cli import app


def test_version_is_set() -> None:
    assert synthdb.__version__


def test_cli_app_is_typer_app() -> None:
    import typer

    assert isinstance(app, typer.Typer)
