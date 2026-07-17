# Contribuir a SynthDB

Antes de tocar nada, lee [CLAUDE.md](CLAUDE.md): recoge las convenciones de
trabajo, los principios innegociables (la IR como única fuente de verdad, las
restricciones de la BD por encima de toda inferencia, determinismo y
reproducibilidad) y lo que **no** se hace en este repositorio. Las decisiones de
diseño no triviales se registran como un ADR corto en [`docs/adr/`](docs/adr/);
un ADR posterior gana a la especificación.

## Entorno de desarrollo

El proyecto usa [uv](https://docs.astral.sh/uv/). Comandos habituales:

```bash
uv sync                                  # entorno
uv run pytest                            # tests estándar (sin modelo, sin Docker)
uv run pytest -m integration             # requiere Docker (testcontainers)
uv run pytest -m llm                     # requiere Ollama local con qwen2.5:7b-instruct
uv run mypy src/                         # estricto; debe quedar a cero
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
```

### Nota para Windows

De la validación contra un esquema real (`docs/validaciones/`), dos ajustes que
evitan fallos de permisos ajenos al código:

- **uv necesita una caché escribible.** La caché por defecto
  (`%LOCALAPPDATA%\uv\cache`, p. ej. `C:\Users\<usuario>\AppData\Local\uv\cache`)
  puede estar bloqueada por permisos y hacer que `uv run` ni siquiera llegue al
  proyecto. Si te ocurre, apunta la caché a una ruta escribible antes de
  ejecutar:

  ```bash
  export UV_CACHE_DIR="$PWD/.uv-cache"   # Git Bash; o setea UV_CACHE_DIR en el entorno
  ```

- **pytest puede requerir `--basetemp` dentro del repo.** El directorio temporal
  por defecto puede dar errores de permisos en algunos equipos Windows (se ven
  como unos pocos tests que fallan solo por no poder crear su `tmp`, no por el
  código). Se resuelven fijando un `basetemp` escribible dentro del repositorio:

  ```bash
  uv run pytest --basetemp=.pytest_tmp
  ```

  Ninguno de los dos ajustes toca el código, `pyproject.toml` ni `uv.lock`.

## Flujo de trabajo

- **Una tarea (issue) por rama/PR.** El ID del plan (T1.3, T2.9…) define el
  alcance; no implementes tareas vecinas «ya que estás».
- **Tests primero** en parsers, intérpretes y fusor. Los snapshots golden de IR
  (`tests/unit/parsing/__snapshots__/`) son la red de seguridad: si un cambio los
  altera, justifícalo en el PR; nunca los regeneres en bloque para «poner verde»
  (la única regeneración en bloque legítima es la que ordena un ADR, como
  [ADR-004](docs/adr/004-lecciones-esquema-real.md)).
- Actualiza [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog) en cada PR, y
  [`docs/limitations.md`](docs/limitations.md) cuando cambie qué DDL se soporta.
- Docstrings estilo Google en todo `src/`. Mensajes de error orientados a acción:
  qué pasó, en qué tabla/columna y qué puede hacer el usuario.
