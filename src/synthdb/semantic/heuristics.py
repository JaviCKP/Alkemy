"""Heurísticas deterministas nombre+tipo → generador (T2.4, especificacion.md §7.1).

Cuarta fuente de la cadena de prioridad del fusor (usuario > IR > LLM >
**heurística** > fallback): un diccionario ordenado de patrones que, sin
modelo ni red, reconoce el rol semántico de una columna por su nombre (es/en)
y su tipo, y propone un `GeneratorSpec` del catálogo de la sesión A. Son
rápidas, testeables y funcionan con `--no-llm`.

Contrato de esta entrega:

- **`infer_column(table, column)`** recorre `PATTERNS` en orden y devuelve el
  primer `HeuristicResult` cuyo patrón case en nombre **y** tipo; si ninguno
  casa, devuelve `None` (es el *fusor* quien decide el fallback, no estas
  heurísticas: aquí «no lo sé» se dice con `None`, no inventando un generador).
- **El orden importa y es parte del contrato** (se testea): los patrones van
  del más específico al más genérico, de modo que `codigo_postal` gane a
  `codigo`, `fecha_nacimiento` a `fecha`, o `usuario` a `nombre`. El
  identificador/FK (`id`, `*_id`) va al final: es una señal estructural débil
  que solo debe ganar cuando ningún patrón semántico casa.
- **Confianzas honestas por patrón** (0.6–0.95), no un 0.9 uniforme: un
  `email` es casi seguro; un `descripcion` reconoce el rol pero su generador
  (texto de relleno) es pobre —el modo IA es del H3B—; `id`/`*_id` van a 0.6,
  por debajo del `min_confidence` por defecto (0.7), para que en el fusor caigan
  al fallback: el valor real de una FK lo pone el selector de claves de la
  sesión C (T2.8), no una secuencia inventada aquí.

Alcance: las heurísticas NO leen enums ni cotas de CHECK (eso es la IR, y el
fusor la aplica por encima de esta fuente); NO eligen claves foráneas; y para
`password`/`hash` producen SIEMPRE un marcador inerte (`template`), jamás un
valor de Faker que parezca real (CLAUDE.md, privacidad).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from synthdb.ir.schema import ColumnSpec, GeneratorSpec, TableSpec

_TEXT_KINDS = frozenset({"text", "varchar", "char"})
_NUMBER_KINDS = frozenset({"integer", "numeric"})
_DATE_KINDS = frozenset({"date", "timestamp"})

# Contexto de tabla para desambiguar `nombre` (persona vs producto).
_PERSON_TABLE = re.compile(
    r"cliente|persona|emplead|usuari|\buser|autor|author|contacto|responsable|"
    r"propietari|comprador|vendedor|proveedor|paciente|alumn|estudiante|profesor|"
    r"socio|miembro|invitad|huesped|agente|gerente|manager",
    re.IGNORECASE,
)
_PRODUCT_TABLE = re.compile(
    r"product|articul|\bitem|pieza|material|categor|category|marca|modelo|"
    r"servicio|\bplan\b|catalog|mercanci|inventari",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HeuristicResult:
    """Propuesta de una heurística para una columna: rol, generador y confianza."""

    role: str
    generator: GeneratorSpec
    confidence: float


@dataclass(frozen=True)
class _Pattern:
    """Una regla del diccionario: nombre, regex, tipos admitidos, builder y confianza.

    `kinds=None` significa «cualquier tipo». `build` recibe la tabla y la columna
    (para desambiguar por contexto) y devuelve `(rol, GeneratorSpec)`; la
    confianza vive en el patrón porque es una propiedad de la regla, no del valor.
    """

    name: str
    regex: re.Pattern[str]
    kinds: frozenset[str] | None
    confidence: float
    build: Callable[[TableSpec, ColumnSpec], tuple[str, GeneratorSpec]]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _faker(provider: str) -> GeneratorSpec:
    return GeneratorSpec(type="faker", params={"provider": provider})


def _const(
    role: str, gen: GeneratorSpec
) -> Callable[[TableSpec, ColumnSpec], tuple[str, GeneratorSpec]]:
    """Builder que ignora el contexto y devuelve siempre el mismo `(rol, gen)`."""

    def build(_table: TableSpec, _column: ColumnSpec) -> tuple[str, GeneratorSpec]:
        return role, gen

    return build


# --- Builders con contexto (dependen del nombre concreto o de la tabla) --------


def _build_nombre(table: TableSpec, column: ColumnSpec) -> tuple[str, GeneratorSpec]:
    """`nombre`/`name`: persona (por defecto) o producto según la tabla (§7.1)."""
    if _PRODUCT_TABLE.search(table.name) and not _PERSON_TABLE.search(table.name):
        return "nombre_producto", _faker("word")
    return "nombre_persona", _faker("name")


def _build_pais(_table: TableSpec, column: ColumnSpec) -> tuple[str, GeneratorSpec]:
    """`pais`/`country`: ISO alfa-2 si el tipo es de 2 caracteres, si no el nombre."""
    if column.type.length == 2:
        return "pais", _faker("country_code")
    return "pais", _faker("country")


def _build_money(_table: TableSpec, column: ColumnSpec) -> tuple[str, GeneratorSpec]:
    """Precio/importe/coste/salario: `numeric_range` con paso según la escala."""
    name = column.name.lower()
    if re.search(r"precio|price", name):
        role = "precio"
    elif re.search(r"coste|costo|cost", name):
        role = "coste"
    elif re.search(r"salario|sueldo|nomina|nómina", name):
        role = "salario"
    else:
        role = "importe"
    params: dict[str, object] = {"min": 0, "max": 1_000_000}
    if column.type.kind == "numeric":
        scale = column.type.scale if column.type.scale is not None else 2
        params["round_to"] = round(10.0**-scale, 10)
    return role, GeneratorSpec(type="numeric_range", params=params)


def _numeric_range(
    role: str, low: float, high: float, round_to: float | None = None
) -> Callable[[TableSpec, ColumnSpec], tuple[str, GeneratorSpec]]:
    """Builder de un `numeric_range` con rango fijo (el fusor lo recorta por la IR)."""

    def build(_table: TableSpec, _column: ColumnSpec) -> tuple[str, GeneratorSpec]:
        params: dict[str, object] = {"min": low, "max": high}
        if round_to is not None:
            params["round_to"] = round_to
        return role, GeneratorSpec(type="numeric_range", params=params)

    return build


def _datetime_range(
    role: str, low: str | None = None, high: str | None = None
) -> Callable[[TableSpec, ColumnSpec], tuple[str, GeneratorSpec]]:
    """Builder de un `datetime_range`; sin cotas usa la década por defecto del generador."""

    def build(_table: TableSpec, _column: ColumnSpec) -> tuple[str, GeneratorSpec]:
        params: dict[str, object] = {}
        if low is not None:
            params["min"] = low
        if high is not None:
            params["max"] = high
        return role, GeneratorSpec(type="datetime_range", params=params)

    return build


def _identifier(_table: TableSpec, column: ColumnSpec) -> tuple[str, GeneratorSpec]:
    """`id`/`*_id`: `uuid` si el tipo es UUID, si no `sequence`. Rol PK vs FK por el nombre."""
    role = "identificador" if column.name.lower() == "id" else "fk"
    if column.type.kind == "uuid":
        return role, GeneratorSpec(type="uuid")
    return role, GeneratorSpec(type="sequence")


# --- El diccionario, del patrón más específico al más genérico -----------------
# El ORDEN es contrato (se testea): un patrón anterior gana a uno posterior.

_PATTERNS: list[_Pattern] = [
    _Pattern(
        "password",
        _rx(r"contrase[nñ]a|password|passwd|\bpwd\b|secret|hash|token|api[_-]?key"),
        _TEXT_KINDS,
        0.8,
        _const("password", GeneratorSpec(type="template")),
    ),
    _Pattern(
        "email", _rx(r"e[_-]?mail|correo"), _TEXT_KINDS, 0.95, _const("email", _faker("email"))
    ),
    _Pattern(
        "iban",
        _rx(r"\biban\b|cuenta_bancaria|numero_cuenta"),
        _TEXT_KINDS,
        0.9,
        _const("iban", _faker("iban")),
    ),
    _Pattern(
        "dni_nif",
        _rx(r"\bdni\b|\bnif\b|\bnie\b|\bcif\b|documento_identidad|\bssn\b|\bvat\b"),
        _TEXT_KINDS,
        0.75,
        _const("documento_identidad", _faker("ssn")),
    ),
    _Pattern(
        "codigo_postal",
        _rx(r"codigo_postal|cod_postal|\bcp\b|zip(_?code)?|postal_?code|postcode"),
        _TEXT_KINDS | frozenset({"integer"}),
        0.85,
        _const("codigo_postal", _faker("postcode")),
    ),
    _Pattern(
        "usuario",
        _rx(r"usuario|username|user_?name|\blogin\b|\bnick\b|\balias\b|handle"),
        _TEXT_KINDS,
        0.8,
        _const("usuario", _faker("user_name")),
    ),
    _Pattern(
        "ip",
        _rx(r"\bip\b|ip_address|direccion_ip|\bipv4\b|\bipv6\b"),
        _TEXT_KINDS,
        0.85,
        _const("ip", _faker("ipv4")),
    ),
    _Pattern(
        "url",
        _rx(r"\burl\b|\buri\b|web(site)?|sitio_web|enlace|\blink\b|homepage"),
        _TEXT_KINDS,
        0.85,
        _const("url", _faker("url")),
    ),
    _Pattern(
        "imagen",
        _rx(r"imagen|\bimage\b|\bfoto\b|photo|avatar|thumbnail|\blogo\b|url_imagen"),
        _TEXT_KINDS,
        0.7,
        _const("imagen", _faker("image_url")),
    ),
    _Pattern(
        "telefono_movil",
        _rx(r"movil|móvil|mobile|celular|\bcell\b|whatsapp"),
        _TEXT_KINDS,
        0.8,
        _const("telefono_movil", _faker("phone_number")),
    ),
    _Pattern(
        "telefono",
        _rx(r"telefono|teléfono|\btel\b|\btlf\b|\bphone\b|\bfax\b"),
        _TEXT_KINDS,
        0.85,
        _const("telefono", _faker("phone_number")),
    ),
    _Pattern(
        "apellidos",
        _rx(r"apellidos?|surname|last_?name|primer_apellido|segundo_apellido"),
        _TEXT_KINDS,
        0.85,
        _const("apellidos", _faker("last_name")),
    ),
    _Pattern(
        "nombre_completo",
        _rx(r"nombre_completo|full_?name|nombre_y_apellidos"),
        _TEXT_KINDS,
        0.85,
        _const("nombre_persona", _faker("name")),
    ),
    _Pattern(
        "empresa",
        _rx(r"empresa|compa[nñ]ia|company|organizaci[oó]n|razon_social|razón_social"),
        _TEXT_KINDS,
        0.75,
        _const("empresa", _faker("company")),
    ),
    _Pattern(
        "puesto",
        _rx(r"puesto|\bcargo\b|\brol\b|position|\bjob\b|job_?title|ocupaci[oó]n|profesi[oó]n"),
        _TEXT_KINDS,
        0.7,
        _const("puesto", _faker("job")),
    ),
    _Pattern(
        "nombre",
        _rx(r"nombre|\bname\b|\bnom\b|first_?name|denominacion|denominación"),
        _TEXT_KINDS,
        0.7,
        _build_nombre,
    ),
    _Pattern(
        "direccion",
        _rx(r"direccion|dirección|address|domicilio|\bcalle\b|street|via_publica"),
        _TEXT_KINDS,
        0.8,
        _const("direccion", _faker("street_address")),
    ),
    _Pattern(
        "ciudad",
        _rx(r"ciudad|\bcity\b|localidad|municipio|poblaci[oó]n|\btown\b"),
        _TEXT_KINDS,
        0.8,
        _const("ciudad", _faker("city")),
    ),
    _Pattern(
        "provincia",
        _rx(r"provincia|\bestado\b|\bregion\b|región|\bstate\b|comunidad_autonoma"),
        _TEXT_KINDS,
        0.7,
        _const("provincia", _faker("region")),
    ),
    _Pattern(
        "pais",
        _rx(r"\bpais\b|país|country|nacionalidad|nationality"),
        _TEXT_KINDS,
        0.85,
        _build_pais,
    ),
    _Pattern(
        "moneda",
        _rx(r"moneda|currency|divisa|codigo_moneda"),
        _TEXT_KINDS,
        0.75,
        _const("moneda", _faker("currency_code")),
    ),
    _Pattern(
        "color", _rx(r"\bcolor\b|colour"), _TEXT_KINDS, 0.7, _const("color", _faker("color_name"))
    ),
    _Pattern(
        "matricula",
        _rx(r"matricula|matrícula|\bplaca\b|license_plate|num_placa"),
        _TEXT_KINDS,
        0.8,
        _const("matricula", _faker("license_plate")),
    ),
    _Pattern(
        "slug",
        _rx(r"\bslug\b|url_slug|permalink"),
        _TEXT_KINDS,
        0.8,
        _const("slug", _faker("slug")),
    ),
    _Pattern(
        "titulo",
        _rx(r"\btitulo\b|título|\btitle\b|asunto|subject"),
        _TEXT_KINDS,
        0.65,
        _const("titulo", _faker("catch_phrase")),
    ),
    _Pattern(
        "descripcion",
        _rx(
            r"descripci[oó]n|description|observaci[oó]n|notas?|comentario|comment|\bnote\b|"
            r"detalle|detail|resumen|summary|biograf[ií]a|\bbio\b|mensaje|message|contenido|content|"
            r"cuerpo|\bbody\b|texto"
        ),
        _TEXT_KINDS,
        0.7,
        _const("descripcion", GeneratorSpec(type="template")),
    ),
    _Pattern(
        "codigo",
        _rx(
            r"codigo|código|\bcode\b|referencia|\bref\b|\bsku\b|\bean\b|\bisbn\b|barcode|codigo_barras|"
            r"numero_serie|num_serie|\bserial\b|expediente|\bfolio\b"
        ),
        _TEXT_KINDS | frozenset({"integer"}),
        0.7,
        _const("codigo", GeneratorSpec(type="template")),
    ),
    _Pattern(
        "edad",
        _rx(r"\bedad\b|\bage\b"),
        frozenset({"integer"}),
        0.85,
        _numeric_range("edad", 0, 120),
    ),
    _Pattern(
        "anio",
        _rx(
            r"\banio\b|\baño\b|\banyo\b|\byear\b|ejercicio|anio_|año_|_anio\b|_año\b|year_|_year\b"
        ),
        frozenset({"integer"}),
        0.8,
        _numeric_range("anio", 1900, 2100),
    ),
    _Pattern(
        "porcentaje",
        _rx(r"porcentaje|percent(age)?|\bpct\b|\bratio\b|\btasa\b|\brate\b|descuento|discount"),
        _NUMBER_KINDS,
        0.7,
        _numeric_range("porcentaje", 0, 100, round_to=0.01),
    ),
    _Pattern(
        "latitud",
        _rx(r"latitud|latitude|\blat\b"),
        _NUMBER_KINDS,
        0.8,
        _numeric_range("latitud", -90, 90, round_to=0.000001),
    ),
    _Pattern(
        "longitud",
        _rx(r"longitud|longitude|\blon\b|\blng\b|\blong\b"),
        _NUMBER_KINDS,
        0.65,
        _numeric_range("longitud", -180, 180, round_to=0.000001),
    ),
    _Pattern(
        "precio",
        _rx(
            r"precio|\bprice\b|importe|\bamount\b|coste|costo|\bcost\b|monto|\btotal\b|subtotal|"
            r"salario|sueldo|nomina|nómina|tarifa|\bfee\b|\bsaldo\b|balance|ingreso|gasto"
        ),
        _NUMBER_KINDS,
        0.75,
        _build_money,
    ),
    _Pattern(
        "cantidad",
        _rx(
            r"cantidad|quantity|\bqty\b|\bstock\b|unidades|existencias|inventario|num_unidades|"
            r"\bnum\b|numero_de|number_of"
        ),
        _NUMBER_KINDS,
        0.7,
        _numeric_range("cantidad", 0, 1000),
    ),
    _Pattern(
        "capacidad",
        _rx(r"capacidad|aforo|\bcupo\b|plazas|asientos"),
        _NUMBER_KINDS,
        0.7,
        _numeric_range("capacidad", 1, 1000),
    ),
    _Pattern(
        "superficie",
        _rx(
            r"superficie|\barea\b|área|metros_cuadrados|\bm2\b|metraje|tama[nñ]o|\bsize\b|"
            r"dimension|dimensión|\bancho\b|\balto\b|\blargo\b|profundidad|\bpeso\b|weight|altura"
        ),
        _NUMBER_KINDS,
        0.7,
        _numeric_range("superficie", 1, 10000),
    ),
    _Pattern(
        "fecha_nacimiento",
        _rx(r"nacimiento|birth|\bdob\b|fecha_nac|\bnac\b|natalicio"),
        _DATE_KINDS,
        0.85,
        _datetime_range("fecha_nacimiento", "1930-01-01", "2010-01-01"),
    ),
    _Pattern(
        "fecha_caducidad",
        _rx(r"caducidad|caduca|vencimiento|vence|expiraci[oó]n|expira|expiry|expire|valid_until"),
        _DATE_KINDS,
        0.8,
        _datetime_range("fecha_caducidad", "2025-01-01", "2035-01-01"),
    ),
    _Pattern(
        "fecha_alta",
        _rx(r"fecha_alta|\balta\b|creaci[oó]n|\bcreated\b|created_at|registro|fecha_registro"),
        _DATE_KINDS,
        0.8,
        _datetime_range("fecha_alta", "2015-01-01", "2025-01-01"),
    ),
    _Pattern(
        "fecha",
        _rx(r"fecha|\bdate\b|_at\b|_date\b|_on\b|datetime|timestamp|\bhora\b|momento"),
        _DATE_KINDS,
        0.7,
        _datetime_range("fecha"),
    ),
    _Pattern(
        "booleano",
        _rx(
            r"^activ|^inactiv|habilitad|enabled|disabled|^es_|^is_|^has_|^tiene_|visible|"
            r"borrado|eliminado|deleted|vigente|pagad|confirmad|verificad|\bflag\b|acepta|aprobad"
        ),
        frozenset({"boolean"}),
        0.8,
        _const("booleano", GeneratorSpec(type="choice", params={"values": [True, False]})),
    ),
    _Pattern(
        "uuid",
        _rx(r"\buuid\b|\bguid\b|\buid\b"),
        _TEXT_KINDS | frozenset({"uuid"}),
        0.85,
        _const("uuid", GeneratorSpec(type="uuid")),
    ),
    _Pattern(
        "identificador",
        _rx(r"^id$|_id$|^id_|_uuid$|_key$"),
        frozenset({"integer", "uuid"}),
        0.6,
        _identifier,
    ),
]


def patterns() -> list[str]:
    """Nombres de los patrones en su orden de prioridad (contrato, se testea)."""
    return [p.name for p in _PATTERNS]


def infer_column(table: TableSpec, column: ColumnSpec) -> HeuristicResult | None:
    """Infiere rol y generador de una columna por nombre y tipo, o `None`.

    Recorre `_PATTERNS` en orden y devuelve el primero que case en nombre y tipo.
    Devolver `None` cuando ninguno casa es deliberado: el fusor (T2.6) es quien
    aplica el fallback seguro y lo marca con aviso, de modo que la ausencia de
    señal semántica quede registrada en el plan y no disfrazada de acierto.

    Args:
        table: Tabla propietaria de la columna (contexto para desambiguar roles
            como `nombre`: persona vs producto).
        column: Columna de la IR a clasificar.

    Returns:
        Un `HeuristicResult` con `role`, `generator` y `confidence`, o `None` si
        ningún patrón reconoce la columna.
    """
    name = column.name.lower()
    kind = column.type.kind
    for pattern in _PATTERNS:
        if pattern.kinds is not None and kind not in pattern.kinds:
            continue
        if pattern.regex.search(name):
            role, generator = pattern.build(table, column)
            return HeuristicResult(role=role, generator=generator, confidence=pattern.confidence)
    return None
