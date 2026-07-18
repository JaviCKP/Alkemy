"""Carga y validación del YAML de configuración (T2.5, especificacion.md §11).

Dos clases de error, ambas traducidas a `ConfigError` con un mensaje orientado
a acción (CLAUDE.md), nunca una traza cruda de ruamel o de Pydantic:

- **Sintaxis YAML** (indentación, corchete sin cerrar...): se reporta con
  línea y columna 1-basadas, que es lo que un editor muestra.
- **Validación del esquema** (campo desconocido, tipo incorrecto, valor fuera
  de rango): se reporta con la **ruta de campo exacta**
  (`tables.viviendas.columns.foo.generator`) por cada error, de modo que el
  usuario sepa qué línea del YAML corregir.

Se usa `ruamel.yaml` en modo seguro (no ejecuta tags arbitrarios). El parser
no infiere tipos más allá de los escalares estándar de YAML; el tipado fuerte
lo aplica Pydantic sobre el `dict` resultante.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError

from synthdb.config.models import Config


class ConfigError(ValueError):
    """Error de carga o validación de la configuración, con mensaje accionable."""


def _format_validation_error(exc: ValidationError, *, source: str) -> str:
    """Traduce un `ValidationError` de Pydantic a un mensaje con rutas de campo.

    Cada error se imprime como `ruta.del.campo: mensaje`, donde la ruta es la
    `loc` de Pydantic unida por puntos (los índices de lista se muestran entre
    corchetes). Un campo desconocido (`extra="forbid"`) aparece con la ruta de
    su contenedor y el nombre del campo sobrante.
    """
    lines = [f"configuración inválida en {source}:"]
    for error in exc.errors():
        location = _format_location(error["loc"])
        message = error["msg"]
        lines.append(f"  - {location}: {message}")
    lines.append(
        "Revisa esos campos en el YAML; consulta especificacion.md §11 para el "
        "formato completo de cada sección."
    )
    return "\n".join(lines)


def _format_location(loc: tuple[Any, ...]) -> str:
    """Une una `loc` de Pydantic en una ruta legible (`a.b[0].c`)."""
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts) or "(raíz)"


def load_config_text(text: str, *, source: str = "<config>") -> Config:
    """Valida una cadena YAML de configuración y devuelve un `Config`.

    Args:
        text: Contenido YAML.
        source: Nombre para los mensajes de error (una ruta de archivo o `<config>`).

    Returns:
        La configuración validada.

    Raises:
        ConfigError: Si el YAML tiene un error de sintaxis (con línea/columna) o
            no cumple el esquema (con la ruta de campo exacta de cada error).
    """
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f" (línea {mark.line + 1}, columna {mark.column + 1})" if mark else ""
        problem = exc.problem or "error de sintaxis YAML"
        raise ConfigError(f"YAML mal formado en {source}{where}: {problem}.") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"la configuración de {source} debe ser un mapa YAML (clave: valor) en su raíz, "
            f"no {type(data).__name__}."
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source=source)) from exc


def load_config(path: str | Path) -> Config:
    """Carga y valida un archivo YAML de configuración.

    Args:
        path: Ruta al archivo `config.yaml`.

    Returns:
        La configuración validada.

    Raises:
        ConfigError: Si el archivo no existe, no se puede leer, tiene un error de
            sintaxis YAML, o no cumple el esquema (ver `load_config_text`).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"no se pudo leer la configuración en {path}: {exc}.") from exc
    return load_config_text(text, source=str(path))
