"""
Método Bolsa Global (Art. 1653 / 1655 CC) — liquidación judicial de mora y capital limpio.

API pública separada del pipeline PDF→Excel (solo-transcripción post-PR #20).
No se invoca desde `generar_excel_lote`; importar explícitamente cuando se necesite
imputación cronológica de abonos contra causaciones.

Refactor alineado a la auditoría v2 (docs/auditoria-determinar-mora-capital-limpio-v2.md §6).
"""
from __future__ import annotations

import calendar
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Mapping, Sequence

# ---------------------------------------------------------------------------
# Contrato de salida
# ---------------------------------------------------------------------------

ClaseConcepto = Literal[
    "interes", "sancion", "capital", "gasto", "no_reconocido", "saldo_anterior"
]


@dataclass(frozen=True)
class ItemCapitalLimpio:
    fecha: str
    concepto: str
    valor_a_demandar: float
    nota: str
    mes_corte: str
    clase_concepto: ClaseConcepto
    indice_fila: int | None = None


# Cómo entró el saldo anterior COLON a la deuda certificada.
#   fila_saldo_anterior: UNA fila "SALDO ANTERIOR" (deuda más antigua, la
#                        bolsa de abonos la consume primero).
#   a_favor:             saldo negativo; se acredita a la bolsa de abonos.
SaldoAnteriorModo = Literal["fila_saldo_anterior", "a_favor"]

CONCEPTO_SALDO_ANTERIOR = "SALDO ANTERIOR"

_ALERTA_SALDO_ANTERIOR_BASE = (
    "ATENCIÓN ABOGADO: esta cuenta tiene saldo anterior COLON. "
    "El saldo anterior SIEMPRE forma parte de la deuda certificada: entra como "
    "la deuda más antigua (los abonos lo consumen primero) y se certifica en una "
    "sola fila «SALDO ANTERIOR», sin inventar cuotas mensuales. "
    "El estado de cuenta no separa qué parte del saldo anterior es capital, "
    "intereses o sanciones: puede incluir intereses de mora previos que NO "
    "deberían cobrarse como capital. Revise antes de emitir (la alerta informa, "
    "no bloquea)."
)

_ALERTA_SANCIONES_BASE = (
    "ATENCIÓN ABOGADO: el estado de cuenta tiene sanciones/multas. No son "
    "interés de mora: se certifican como capital en la columna de cuotas "
    "extraordinarias. Confirme que su cobro procede (debido proceso, "
    "reglamento de propiedad horizontal) antes de emitir."
)


@dataclass
class ResultadoMoraCapitalLimpio:
    fecha_inicio_mora: str
    capital_limpio_a_demandar: list[ItemCapitalLimpio]
    total_capital_demandado: float
    bolsa_global_inicial: float
    bolsa_remanente_final: float
    errores_procesamiento: list[str] = field(default_factory=list)
    # ΣAbono bruto y saldo anterior COLON.
    bolsa_abonos: float = 0.0
    saldo_anterior: float | None = None
    saldo_anterior_modo: SaldoAnteriorModo | None = None
    # Parte del saldo anterior que sigue viva tras los abonos (fila certificada).
    saldo_anterior_pendiente: float = 0.0
    alerta_saldo_anterior: bool = False
    alerta_saldo_anterior_mensaje: str | None = None
    # Sanciones/multas certificadas como capital (no son interés de mora).
    sanciones_certificadas: float = 0.0
    alerta_sanciones: bool = False
    alerta_sanciones_mensaje: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fecha_inicio_mora": self.fecha_inicio_mora,
            "capital_limpio_a_demandar": [
                {
                    "fecha": i.fecha,
                    "concepto": i.concepto,
                    "valor_a_demandar": i.valor_a_demandar,
                    "nota": i.nota,
                    "mes_corte": i.mes_corte,
                    "clase_concepto": i.clase_concepto,
                    "indice_fila": i.indice_fila,
                }
                for i in self.capital_limpio_a_demandar
            ],
            "total_capital_demandado": self.total_capital_demandado,
            "bolsa_global_inicial": self.bolsa_global_inicial,
            "bolsa_remanente_final": self.bolsa_remanente_final,
            "bolsa_abonos": self.bolsa_abonos,
            "saldo_anterior": self.saldo_anterior,
            "saldo_anterior_modo": self.saldo_anterior_modo,
            "saldo_anterior_pendiente": self.saldo_anterior_pendiente,
            "alerta_saldo_anterior": self.alerta_saldo_anterior,
            "alerta_saldo_anterior_mensaje": self.alerta_saldo_anterior_mensaje,
            "sanciones_certificadas": self.sanciones_certificadas,
            "alerta_sanciones": self.alerta_sanciones,
            "alerta_sanciones_mensaje": self.alerta_sanciones_mensaje,
            "errores_procesamiento": list(self.errores_procesamiento),
        }


@dataclass
class _ItemMes:
    indice: int
    fecha: str
    concepto: str
    valor: float
    clase: ClaseConcepto


@dataclass
class _GrupoMes:
    capitales: list[_ItemMes] = field(default_factory=list)
    intereses: list[_ItemMes] = field(default_factory=list)


# Palabras clave de negocio (matching por token / límite, no substring corto)
# Interés de mora REAL: palabra inequívoca (INTERES*, MORATORIO/A).
KW_INTERESES_FUERTES: frozenset[str] = frozenset(
    {"INTERES", "INTERESES", "MORATORIO", "MORATORIA"}
)
# Palabras sueltas que SOLO cuentan como interés si el concepto no es una cuota
# (CUOTA ADMINISTRACION MORA es una cuota, no un interés).
KW_INTERESES_DEBILES: frozenset[str] = frozenset({"INT", "MORA", "MORAS"})
KW_INTERESES: frozenset[str] = KW_INTERESES_FUERTES | KW_INTERESES_DEBILES
# Sanciones y multas: NO son interés; se cobran como capital (extraordinarias).
KW_SANCIONES: frozenset[str] = frozenset(
    {
        "SANCION",
        "SANCIONES",
        "MULTA",
        "MULTAS",
        "PENALIDAD",
        "PENALIDADES",
    }
)
_PREFIJOS_SANCION: tuple[str, ...] = ("SANCION", "MULTA", "PENALIDAD")
KW_CAPITAL: frozenset[str] = frozenset(
    {
        "CUOTA",
        "ADMINISTRACION",
        "ADMON",
        "EXTRA",
        "RETROACTIVO",
        "FACHADA",
        "PINTURA",
        "CAPITAL",
    }
)
KW_GASTOS: frozenset[str] = frozenset(
    {"PREJURIDICO", "HONORARIOS", "ABOGADO", "COBRO"}
)

_PESOS_DECIMALES = 2
_EPS = 0.005  # medio centavo COP

# Regex de fecha precompilados (hot path en lotes grandes).
_RE_FECHA_PUNTO = re.compile(r"^(\d{4})\.(\d{1,2})(?:\.(\d{1,2}))?")
_RE_FECHA_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_RE_FECHA_DMY = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})")
_RE_FECHA_YM = re.compile(r"^(\d{4})[.-](\d{1,2})$")
_RE_NO_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_RE_SPACES = re.compile(r"\s+")


def _redondear_cop(valor: float) -> float:
    return round(float(valor), _PESOS_DECIMALES)


def resultado_vacio(*, errores: list[str] | None = None) -> dict[str, Any]:
    """Contrato vacío estable (omitidos / entrada inválida) sin barrer filas."""
    return ResultadoMoraCapitalLimpio(
        fecha_inicio_mora="Sin deuda",
        capital_limpio_a_demandar=[],
        total_capital_demandado=0.0,
        bolsa_global_inicial=0.0,
        bolsa_remanente_final=0.0,
        errores_procesamiento=list(errores or []),
        bolsa_abonos=0.0,
        saldo_anterior=None,
        saldo_anterior_modo=None,
        alerta_saldo_anterior=False,
        alerta_saldo_anterior_mensaje=None,
    ).as_dict()


def _mes_anterior_corte(mes_corte: str) -> str | None:
    """YYYY.MM → mes calendario anterior, o None si inválido."""
    try:
        anio_s, mes_s = mes_corte.split(".", 1)
        anio, mes = int(anio_s), int(mes_s)
    except (TypeError, ValueError):
        return None
    if mes <= 1:
        return _mes_valido(anio - 1, 12)
    return _mes_valido(anio, mes - 1)


def _fmt_cop_alerta(valor: float) -> str:
    """Formato compacto COP para alertas (miles con punto)."""
    entero = int(round(float(valor)))
    signo = "-" if entero < 0 else ""
    return f"{signo}${abs(entero):,}".replace(",", ".")


def _ultimo_dia_mes(mes_corte: str) -> str | None:
    """YYYY.MM → 'YYYY.MM.DD' con el último día calendario del mes."""
    try:
        anio_s, mes_s = mes_corte.split(".", 1)
        anio, mes = int(anio_s), int(mes_s)
        return f"{anio:04d}.{mes:02d}.{calendar.monthrange(anio, mes)[1]:02d}"
    except (TypeError, ValueError):
        return None


def mensaje_alerta_saldo_anterior(
    *,
    saldo: float,
    modo: SaldoAnteriorModo,
    pendiente: float = 0.0,
    abonos_aplicados: float = 0.0,
) -> str:
    """Texto visible para el abogado (UI Bolsa / certificados)."""
    monto = _fmt_cop_alerta(saldo)
    if modo == "a_favor":
        return (
            f"AVISO ABOGADO: el estado de cuenta trae un saldo anterior A FAVOR "
            f"del propietario ({monto}). Se acreditó a la bolsa de abonos "
            "(reduce la deuda certificada); no se convirtió en cero."
        )
    if pendiente <= _EPS:
        detalle = (
            f" Los abonos ({_fmt_cop_alerta(abonos_aplicados)}) lo cubrieron por "
            "completo; no aparece fila «SALDO ANTERIOR» en el certificado."
        )
    elif abonos_aplicados > _EPS:
        detalle = (
            f" Tras aplicar abonos por {_fmt_cop_alerta(abonos_aplicados)} quedan "
            f"{_fmt_cop_alerta(pendiente)} vivos, certificados en la fila "
            "«SALDO ANTERIOR»."
        )
    else:
        detalle = (
            f" Sin abonos que lo cubran: {_fmt_cop_alerta(pendiente)} certificados "
            "en la fila «SALDO ANTERIOR»."
        )
    return f"{_ALERTA_SALDO_ANTERIOR_BASE} Saldo anterior: {monto}.{detalle}"


def _normalizar_texto(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and (math.isnan(valor) or math.isinf(valor)):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.upper().strip()
    texto = _RE_NO_ALNUM.sub(" ", texto)
    return _RE_SPACES.sub(" ", texto).strip()


def _tokens(concepto_norm: str) -> list[str]:
    return concepto_norm.split() if concepto_norm else []


# Tokens que, junto a ADMIN*, tipifican gasto/extra y NO cuota ordinaria.
_KW_ADMIN_NO_ORDINARIA: frozenset[str] = frozenset(
    {
        "EXTRA",
        "EXTRAORDINARIA",
        "EXTRAORDINARIO",
        "EXTRAORDINARIAS",
        "PINTURA",
        "FACHADA",
        "RETROACTIVO",
        "HONORARIO",
        "HONORARIOS",
        "PREJURIDICO",
        "ABOGADO",
        "COBRO",
        "GASTO",
        "GASTOS",
    }
)


def _es_cuota_ordinaria(
    concepto: str,
    *,
    forzados_ordinaria: frozenset[str] | set[str] | None = None,
) -> bool:
    """
    Heurística Word/Bolsa: cuota de administración → ordinaria.

    - CUOTA/COUTA + ADMIN*/ADMON → ordinaria.
    - Solo ADMINISTRACION / ADMINISTRACIÓN / ADMON (sin EXTRA/gasto) → ordinaria
      (PDFs COLON a veces omiten la palabra CUOTA).
    - Override manual: `forzados_ordinaria` con texto normalizado o crudo.
    - EXTRA*, pintura, honorarios, etc. → no ordinaria.
    """
    normal = _normalizar_texto(concepto)
    if not normal or "INTERES" in normal:
        return False
    if forzados_ordinaria:
        crudo = str(concepto or "").strip().upper()
        if normal in forzados_ordinaria or crudo in forzados_ordinaria:
            return True
    tokens = normal.split()
    if any(_es_token_sancion(tok) for tok in tokens):
        return False
    if any(
        tok.startswith("EXTRA") or tok in _KW_ADMIN_NO_ORDINARIA
        for tok in tokens
    ):
        # EXTRA* siempre fuera; otros keywords solo bloquean el camino "solo ADMIN".
        if any(tok.startswith("EXTRA") for tok in tokens):
            return False
    tiene_cuota = any(tok.startswith("CUOT") or tok.startswith("COUT") for tok in tokens)
    tiene_admin = any(
        tok.startswith("ADMIN")
        or tok.startswith("ADMON")
        or tok in {"ADMINISTRATIVA", "ADMINISTRATIVO"}
        for tok in tokens
    )
    if tiene_cuota and tiene_admin:
        return True
    if tiene_admin and not tiene_cuota:
        # ADMINISTRACION suelta (o con DE/DEL/LA): ordinaria salvo ruido tipificado.
        if any(
            tok in _KW_ADMIN_NO_ORDINARIA or tok.startswith("HONOR")
            for tok in tokens
        ):
            return False
        return True
    return False


def _capitales_prelacion_b_remanente(capitales: Sequence[_ItemMes]) -> list[_ItemMes]:
    """
    Orden de imputación del sobrante en B-Remanente (tras intereses):
    1) extraordinarias / gastos / no reconocidos / capital no-ordinaria
    2) cuota ordinaria (administración) al final
    Dentro de cada tramo se preserva el orden de aparición (índice de fila).
    """
    no_ordinarias = [i for i in capitales if not _es_cuota_ordinaria(i.concepto)]
    ordinarias = [i for i in capitales if _es_cuota_ordinaria(i.concepto)]
    return no_ordinarias + ordinarias


def _a_float_seguro(valor: Any) -> float | None:
    """
    Parsea montos COP/latam. None = ilegible (no confundir con 0.0 legítimo).
    Acepta: 1234.56 | 1.234,56 | 1,234.56 | (1.234,56) | $ 1.234
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
        if math.isnan(numero) or math.isinf(numero):
            return None
        return numero

    texto = str(valor).strip()
    if not texto or texto.startswith("#") or set(texto) <= {"#"}:
        return None

    negativo = texto.startswith("(") and texto.endswith(")")
    limpio = (
        texto.replace("(", "")
        .replace(")", "")
        .replace("$", "")
        .replace(" ", "")
        .replace("\u00a0", "")
    )
    if not limpio:
        return None

    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        partes = limpio.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            limpio = limpio.replace(",", ".")
        else:
            limpio = limpio.replace(",", "")

    try:
        numero = float(limpio)
    except ValueError:
        return None
    if math.isnan(numero) or math.isinf(numero):
        return None
    return -numero if negativo else numero


def _mes_valido(anio: int, mes: int) -> str | None:
    if anio < 1900 or anio > 2100 or mes < 1 or mes > 12:
        return None
    return f"{anio:04d}.{mes:02d}"


def _extraer_mes_corte(fecha_raw: Any) -> str | None:
    """
    Devuelve 'YYYY.MM' o None.
    Acepta: YYYY.MM[.DD], YYYY-MM-DD[ hora], DD/MM/YYYY, date/datetime.
    """
    if fecha_raw is None:
        return None
    if isinstance(fecha_raw, float) and (math.isnan(fecha_raw) or math.isinf(fecha_raw)):
        return None
    if isinstance(fecha_raw, datetime):
        return f"{fecha_raw.year:04d}.{fecha_raw.month:02d}"
    if isinstance(fecha_raw, date):
        return f"{fecha_raw.year:04d}.{fecha_raw.month:02d}"

    texto = str(fecha_raw).strip()
    if not texto:
        return None

    m = _RE_FECHA_PUNTO.match(texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    m = _RE_FECHA_ISO.match(texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    m = _RE_FECHA_DMY.match(texto)
    if m:
        return _mes_valido(int(m.group(3)), int(m.group(2)))

    m = _RE_FECHA_YM.match(texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    return None


def _es_token_sancion(token: str) -> bool:
    return token in KW_SANCIONES or token.startswith(_PREFIJOS_SANCION)


def _es_token_interes_fuerte(token: str) -> bool:
    return (
        token in KW_INTERESES_FUERTES
        or token.startswith("INTERES")
        or token.startswith("MORATORI")
    )


def _es_token_cuota(token: str) -> bool:
    return token.startswith("CUOT") or token.startswith("COUT")


def _es_token_admin(token: str) -> bool:
    return token.startswith("ADMIN") or token.startswith("ADMON")


def concepto_mora_ambiguo(concepto_raw: Any) -> bool:
    """MORA/INT suelto en un concepto que parece cuota/administración (E6)."""
    toks = _tokens(_normalizar_texto(concepto_raw))
    if not toks or any(_es_token_interes_fuerte(t) for t in toks):
        return False
    if any(_es_token_sancion(t) for t in toks):
        return False
    tiene_debil = any(t in KW_INTERESES_DEBILES for t in toks)
    return tiene_debil and any(_es_token_cuota(t) or _es_token_admin(t) for t in toks)


def _clasificar_concepto(concepto_raw: Any, cache: dict[str, ClaseConcepto] | None = None) -> ClaseConcepto:
    """
    Prioridad: interés (INTERES*/MORATORIO) > sanción/multa > mora suelta
    (solo si no es cuota/administración) > gasto > capital > no_reconocido.

    Sanciones y multas NO son interés de mora: clase propia `sancion`
    (se cobran como capital, columna de extraordinarias).
    Usa tokens normalizados (NFKD) — nunca substring corto suelto tipo INT.
    `cache` opcional memoriza por texto crudo (misma cuenta / lote).
    """
    clave = "" if concepto_raw is None else str(concepto_raw)
    if cache is not None and clave in cache:
        return cache[clave]

    normal = _normalizar_texto(concepto_raw)
    if not normal:
        clase: ClaseConcepto = "no_reconocido"
    else:
        toks = _tokens(normal)
        if any(_es_token_interes_fuerte(t) for t in toks):
            clase = "interes"
        elif any(_es_token_sancion(t) for t in toks):
            clase = "sancion"
        elif any(t in KW_INTERESES_DEBILES for t in toks) and not any(
            _es_token_cuota(t) or _es_token_admin(t) for t in toks
        ):
            clase = "interes"
        elif any(t in KW_GASTOS for t in toks):
            clase = "gasto"
        elif any(t in KW_CAPITAL for t in toks):
            clase = "capital"
        else:
            clase = "no_reconocido"

    if cache is not None:
        cache[clave] = clase
    return clase


def clasificar_concepto_bolsa(concepto_raw: Any) -> ClaseConcepto:
    """API pública de clasificación (útil en tests y wrappers)."""
    return _clasificar_concepto(concepto_raw)


def determinar_mora_y_capital_limpio(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    saldo_anterior: float | None = None,
    extender_certificado_saldo_anterior: bool = False,
) -> dict[str, Any]:
    """
    Método Bolsa Global (Art. 1653 / 1655 CC) — reglas estrictas:

    1. Sumar todos los Abono → bolsa bruta (`bolsa_abonos`).
    2. Saldo anterior COLON (regla del usuario, 2026-10): SIEMPRE forma parte de
       la deuda certificada, sin depender de ningún flag.
       - saldo > 0: entra como la deuda MÁS ANTIGUA, en UNA fila
         «SALDO ANTERIOR» fechada el último día del mes previo a la ventana
         (sin inventar cuotas mensuales). Los abonos la consumen primero; lo que
         quede vivo se certifica y fecha el inicio de mora.
       - saldo < 0 (a favor): se acredita a la bolsa de abonos.
       Siempre se enciende `alerta_saldo_anterior` (informa; no bloquea): el
       saldo anterior puede traer intereses/sanciones previos sin separar.
       `extender_certificado_saldo_anterior` está DEPRECADO y se ignora
       (se acepta solo por compatibilidad con clientes antiguos).
    3. Agrupar Valor > 0 por mes YYYY.MM.
    4. Clasificar: intereses / sanciones / capital / gastos; no reconocido →
       alerta + capital. Sanciones y multas NO son interés: van a capital
       (extraordinarias) con alerta visible (`alerta_sanciones`).
    5. Consumir mes a mes (más antiguo primero): intereses primero, luego
       capital (las sanciones consumen abono como el resto del capital no
       ordinario: antes de la cuota ordinaria).
    6. Mes Limpio: bolsa no cubre todos los intereses → residual intereses
       perdonado; capital del mes 100% intacto; ese mes = fecha_inicio_mora.
    7. B-Remanente (cubre I_m, no todo C_m): el sobrante B−I_m amortiza
       primero extras/sanciones/gastos/no reconocidos; la cuota ordinaria al final.
    8. Con bolsa = 0: solo capital futuro; intereses de mora posteriores descartados.
    9. No crash; errores por fila; corruptos → 0.0.

    Retorna dict (vía ResultadoMoraCapitalLimpio.as_dict) para consumo fácil
    desde JSON/API sin acoplar el Excel de descarga PDF→Excel.
    """
    errores: list[str] = []
    bolsa = 0.0
    meses: dict[str, _GrupoMes] = {}
    capital_fecha_invalida: list[_ItemMes] = []
    clase_cache: dict[str, ClaseConcepto] = {}

    if rows is None:
        errores.append("Entrada 'rows' es None; se procesa como lista vacía.")
        rows = []

    if not isinstance(rows, (list, tuple)):
        try:
            rows = list(rows)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            return resultado_vacio(errores=[f"Entrada 'rows' no iterable: {exc}"])

    # ---- 1) Barrido: bolsa + agrupación por mes ----
    for idx, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping):
                errores.append(
                    f"Fila {idx}: tipo no-dict ({type(row).__name__}); omitida."
                )
                continue

            fecha_str = "" if row.get("Fecha") is None else str(row.get("Fecha")).strip()
            concepto_raw = row.get("Concepto", "")
            concepto_norm = _normalizar_texto(concepto_raw)

            valor_parsed = _a_float_seguro(row.get("Valor"))
            abono_parsed = _a_float_seguro(row.get("Abono"))

            if row.get("Valor") not in (None, "") and valor_parsed is None:
                errores.append(f"Fila {idx}: Valor ilegible '{row.get('Valor')}' → 0.0.")
            if row.get("Abono") not in (None, "") and abono_parsed is None:
                errores.append(f"Fila {idx}: Abono ilegible '{row.get('Abono')}' → 0.0.")

            valor = 0.0 if valor_parsed is None else valor_parsed
            abono = 0.0 if abono_parsed is None else abono_parsed

            if abono < 0:
                errores.append(
                    f"Fila {idx}: Abono negativo ({abono}); no se suma a la bolsa."
                )
            else:
                bolsa += abono

            if valor < 0:
                errores.append(
                    f"Fila {idx}: Valor negativo ({valor}); no se agrupa como causación."
                )
                continue

            if valor <= _EPS:
                continue

            clase = _clasificar_concepto(concepto_raw, clase_cache)
            if clase == "no_reconocido":
                errores.append(
                    f"Fila {idx}: Concepto no reconocido -> '{concepto_norm or concepto_raw}'. "
                    "Procesado como capital por defecto."
                )

            if clase == "gasto":
                errores.append(
                    f"Fila {idx}: Concepto de gasto/cobranza '{concepto_norm}' "
                    "tratado como capital demandable (revisar pretensión judicial)."
                )

            if clase == "sancion":
                errores.append(
                    f"Fila {idx}: Sanción/multa '{concepto_norm}' "
                    f"({_fmt_cop_alerta(valor)}) NO es interés de mora: se cobra "
                    "como capital en cuotas extraordinarias (confirme que procede)."
                )
            elif concepto_mora_ambiguo(concepto_raw):
                errores.append(
                    f"Fila {idx}: Concepto '{concepto_norm}' menciona MORA pero "
                    "parece una cuota; se trató como capital, no como interés "
                    "(revise si en realidad es interés de mora)."
                )

            item = _ItemMes(
                indice=idx,
                fecha=fecha_str,
                concepto=concepto_norm or str(concepto_raw or "").strip(),
                valor=_redondear_cop(valor),
                clase=clase,
            )

            mes_corte = _extraer_mes_corte(row.get("Fecha"))
            if mes_corte is None:
                errores.append(
                    f"Fila {idx}: Fecha no parseable '{fecha_str}'; "
                    "excluida del quiebre de mora (queda en revisión)."
                )
                capital_fecha_invalida.append(item)
                continue

            grupo = meses.setdefault(mes_corte, _GrupoMes())
            if clase == "interes":
                grupo.intereses.append(item)
            else:
                # capital | gasto | no_reconocido → cola de capital (regla usuario)
                grupo.capitales.append(item)

        except Exception as exc:  # noqa: BLE001
            errores.append(f"Fila {idx}: Error inesperado — {exc}")
            continue

    bolsa_abonos = _redondear_cop(bolsa)
    saldo_ant_val: float | None = None
    saldo_modo: SaldoAnteriorModo | None = None
    saldo_pendiente = 0.0
    alerta_sa = False
    alerta_sa_msg: str | None = None
    saldo_item_sin_mes: _ItemMes | None = None

    saldo_ant = 0.0
    if saldo_anterior is not None and saldo_anterior != "":
        parsed_sa = _a_float_seguro(saldo_anterior)
        if parsed_sa is None:
            errores.append(
                f"saldo_anterior ilegible '{saldo_anterior}'; se ignora "
                "(REVISE: la deuda certificada podría quedar incompleta)."
            )
        else:
            saldo_ant = _redondear_cop(parsed_sa)
            saldo_ant_val = saldo_ant

    if extender_certificado_saldo_anterior:
        errores.append(
            "extender_certificado_saldo_anterior está deprecado y se ignora: el "
            "saldo anterior siempre se certifica en una fila «SALDO ANTERIOR»."
        )

    bolsa_inicial = bolsa_abonos
    if saldo_ant < -_EPS:
        credito = _redondear_cop(-saldo_ant)
        saldo_modo = "a_favor"
        alerta_sa = True
        bolsa_inicial = _redondear_cop(bolsa_abonos + credito)
        errores.append(
            f"Saldo anterior a favor ({_fmt_cop_alerta(saldo_ant)}): se acredita "
            f"a la bolsa de abonos (bolsa = ΣAbono {_fmt_cop_alerta(bolsa_abonos)} "
            f"+ {_fmt_cop_alerta(credito)})."
        )
        alerta_sa_msg = mensaje_alerta_saldo_anterior(saldo=saldo_ant, modo="a_favor")
    elif saldo_ant > _EPS:
        saldo_modo = "fila_saldo_anterior"
        alerta_sa = True
        fechas_validas = sorted(
            m
            for m in (_extraer_mes_corte(r.get("Fecha")) for r in rows if isinstance(r, Mapping))
            if m
        )
        primer_mes = min(meses.keys()) if meses else (fechas_validas[0] if fechas_validas else None)
        mes_antes = _mes_anterior_corte(primer_mes) if primer_mes else None
        item_sa = _ItemMes(
            indice=-1,
            fecha=(_ultimo_dia_mes(mes_antes) or "") if mes_antes else "",
            concepto=CONCEPTO_SALDO_ANTERIOR,
            valor=saldo_ant,
            clase="saldo_anterior",
        )
        if mes_antes is not None:
            meses.setdefault(mes_antes, _GrupoMes()).capitales.append(item_sa)
        else:
            saldo_item_sin_mes = item_sa
            errores.append(
                "Saldo anterior sin ninguna fecha de referencia en el extracto: "
                "se certifica al final como «REQUIERE REVISION»."
            )
    bolsa = bolsa_inicial

    # ---- 2) Consumir bolsa cronológicamente ----
    capital_limpio: list[ItemCapitalLimpio] = []
    fecha_inicio_mora: str | None = None
    en_mora = False

    for mes in sorted(meses.keys()):
        grupo = meses[mes]
        total_intereses = _redondear_cop(sum(i.valor for i in grupo.intereses))
        total_capital = _redondear_cop(sum(i.valor for i in grupo.capitales))
        total_mes = _redondear_cop(total_intereses + total_capital)

        if total_mes <= _EPS:
            continue  # mes vacío: no quiebre falso

        # Post-quiebre: solo capital; intereses descartados
        if en_mora:
            for item in grupo.capitales:
                if item.valor > _EPS:
                    capital_limpio.append(
                        ItemCapitalLimpio(
                            fecha=item.fecha,
                            concepto=item.concepto,
                            valor_a_demandar=_redondear_cop(item.valor),
                            nota="Capital pleno (post-quiebre; intereses históricos descartados).",
                            mes_corte=mes,
                            clase_concepto=item.clase,
                            indice_fila=item.indice,
                        )
                    )
            continue

        # Escenario A: bolsa cubre todo el mes
        if bolsa + _EPS >= total_mes:
            bolsa = _redondear_cop(bolsa - total_mes)
            continue

        # Escenario B: bolsa > 0 pero no alcanza para todo el mes
        if bolsa > _EPS:
            if bolsa + _EPS < total_intereses:
                # Mes Limpio: no cubre todos los intereses → perdón residual;
                # capital 100% intacto; este mes = fecha_inicio_mora
                bolsa = 0.0
                fecha_inicio_mora = mes
                en_mora = True
                for item in grupo.capitales:
                    if item.valor > _EPS:
                        capital_limpio.append(
                            ItemCapitalLimpio(
                                fecha=item.fecha,
                                concepto=item.concepto,
                                valor_a_demandar=_redondear_cop(item.valor),
                                nota=(
                                    "Capital intacto. Remanente agotado en intereses; "
                                    "residual de intereses perdonado (Mes Limpio)."
                                ),
                                mes_corte=mes,
                                clase_concepto=item.clase,
                                indice_fila=item.indice,
                            )
                        )
            else:
                # Cubre intereses; remanente reduce capital con prelación:
                # extras/gastos/no reconocidos primero; cuota ordinaria al final.
                sobrante = _redondear_cop(bolsa - total_intereses)
                bolsa = 0.0
                capitales_restantes = 0.0
                for item in _capitales_prelacion_b_remanente(grupo.capitales):
                    valor_restante = item.valor
                    if sobrante > _EPS:
                        if sobrante + _EPS >= valor_restante:
                            sobrante = _redondear_cop(sobrante - valor_restante)
                            valor_restante = 0.0
                        else:
                            valor_restante = _redondear_cop(valor_restante - sobrante)
                            sobrante = 0.0
                    if valor_restante > _EPS:
                        capitales_restantes += valor_restante
                        capital_limpio.append(
                            ItemCapitalLimpio(
                                fecha=item.fecha,
                                concepto=item.concepto,
                                valor_a_demandar=_redondear_cop(valor_restante),
                                nota=(
                                    "Capital reducido por remanente de abono "
                                    "(prelación: extras/gastos antes que ordinaria)."
                                ),
                                mes_corte=mes,
                                clase_concepto=item.clase,
                                indice_fila=item.indice,
                            )
                        )
                if capitales_restantes > _EPS:
                    fecha_inicio_mora = mes
                    en_mora = True
                # Si intereses+capital del mes quedaron extinguidos: no marcar mora aquí;
                # bolsa=0 → el próximo mes con capital cae en Escenario C.
            continue

        # Escenario C: bolsa == 0 antes de tocar este mes
        if total_capital > _EPS:
            fecha_inicio_mora = mes
            en_mora = True
            for item in grupo.capitales:
                if item.valor > _EPS:
                    capital_limpio.append(
                        ItemCapitalLimpio(
                            fecha=item.fecha,
                            concepto=item.concepto,
                            valor_a_demandar=_redondear_cop(item.valor),
                            nota="Capital pleno (bolsa agotada antes del mes).",
                            mes_corte=mes,
                            clase_concepto=item.clase,
                            indice_fila=item.indice,
                        )
                    )
        # Intereses del mes ignorados a propósito

    # Fechas inválidas: fuera del quiebre; capital/gasto/no_reconocido anexados
    # para no perder pretensión (intereses con fecha inválida: solo error).
    for item in capital_fecha_invalida:
        if item.clase == "interes":
            errores.append(
                f"Fila {item.indice}: interés con fecha inválida omitido del capital a demandar."
            )
            continue
        capital_limpio.append(
            ItemCapitalLimpio(
                fecha=item.fecha,
                concepto=item.concepto,
                valor_a_demandar=_redondear_cop(item.valor),
                nota=(
                    "REQUIERE REVISION: fecha no parseable; excluido del quiebre de mora. "
                    "Incluido al final para no perder capital."
                ),
                mes_corte="FECHA_INVALIDA",
                clase_concepto=item.clase,
                indice_fila=item.indice,
            )
        )

    if saldo_item_sin_mes is not None:
        consumido = _redondear_cop(min(bolsa, saldo_item_sin_mes.valor))
        bolsa = _redondear_cop(bolsa - consumido)
        restante = _redondear_cop(saldo_item_sin_mes.valor - consumido)
        if restante > _EPS:
            capital_limpio.append(
                ItemCapitalLimpio(
                    fecha="",
                    concepto=CONCEPTO_SALDO_ANTERIOR,
                    valor_a_demandar=restante,
                    nota=(
                        "REQUIERE REVISION: saldo anterior sin fecha de referencia; "
                        "incluido al final para no perder deuda."
                    ),
                    mes_corte="FECHA_INVALIDA",
                    clase_concepto="saldo_anterior",
                    indice_fila=None,
                )
            )

    # El saldo anterior que sigue vivo se explica en su propia nota.
    capital_limpio = [
        ItemCapitalLimpio(
            fecha=i.fecha,
            concepto=i.concepto,
            valor_a_demandar=i.valor_a_demandar,
            nota=(
                "Saldo anterior COLON: deuda más antigua, consumida primero por "
                "los abonos; lo que queda vivo se certifica. "
                + i.nota
            )
            if i.clase_concepto == "saldo_anterior"
            and not i.nota.startswith("Saldo anterior COLON")
            else i.nota,
            mes_corte=i.mes_corte,
            clase_concepto=i.clase_concepto,
            indice_fila=i.indice_fila,
        )
        for i in capital_limpio
    ]

    saldo_pendiente = _redondear_cop(
        sum(i.valor_a_demandar for i in capital_limpio if i.clase_concepto == "saldo_anterior")
    )
    sanciones_cert = _redondear_cop(
        sum(i.valor_a_demandar for i in capital_limpio if i.clase_concepto == "sancion")
    )
    if saldo_modo == "fila_saldo_anterior":
        aplicado = _redondear_cop(saldo_ant - saldo_pendiente)
        alerta_sa_msg = mensaje_alerta_saldo_anterior(
            saldo=saldo_ant,
            modo="fila_saldo_anterior",
            pendiente=saldo_pendiente,
            abonos_aplicados=aplicado,
        )
        errores.append(
            f"Saldo anterior {_fmt_cop_alerta(saldo_ant)} tratado como deuda más "
            f"antigua: abonos aplicados {_fmt_cop_alerta(aplicado)}, pendiente "
            f"certificado {_fmt_cop_alerta(saldo_pendiente)}."
        )

    alerta_sanc = sanciones_cert > _EPS
    alerta_sanc_msg = (
        f"{_ALERTA_SANCIONES_BASE} Sanciones/multas certificadas: "
        f"{_fmt_cop_alerta(sanciones_cert)}."
        if alerta_sanc
        else None
    )

    total = _redondear_cop(sum(i.valor_a_demandar for i in capital_limpio))

    return ResultadoMoraCapitalLimpio(
        fecha_inicio_mora=fecha_inicio_mora or "Sin deuda",
        capital_limpio_a_demandar=capital_limpio,
        total_capital_demandado=total,
        bolsa_global_inicial=bolsa_inicial,
        bolsa_remanente_final=_redondear_cop(max(0.0, bolsa)),
        errores_procesamiento=errores,
        bolsa_abonos=bolsa_abonos,
        saldo_anterior=saldo_ant_val,
        saldo_anterior_modo=saldo_modo,
        saldo_anterior_pendiente=saldo_pendiente,
        alerta_saldo_anterior=alerta_sa,
        alerta_saldo_anterior_mensaje=alerta_sa_msg,
        sanciones_certificadas=sanciones_cert,
        alerta_sanciones=alerta_sanc,
        alerta_sanciones_mensaje=alerta_sanc_msg,
    ).as_dict()
