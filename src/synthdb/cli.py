"""Punto de entrada de la CLI (Typer). Los subcomandos se añaden hito a hito."""

import typer

app = typer.Typer(name="synthdb", no_args_is_help=True)


if __name__ == "__main__":
    app()
