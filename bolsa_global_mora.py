"""
Método Bolsa Global (Art. 1653 / 1655 CC) — liquidación judicial de mora y capital limpio.

API pública separada del pipeline PDF→Excel (solo-transcripción post-PR #20).
No se invoca desde `generar_excel_lote`; importar explícitamente cuando se necesite
imputación cronológica de abonos contra causaciones.

Refactor alineado a la auditoría v2 (docs/auditoria-determinar-mora-capital-limpio-v2.md §6).
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Mapping, Sequence

# ---------------------------------------------------------------------------
# Contrato de salida
# ---------------------------------------------------------------------------

ClaseConcepto = Literal["interes", "capital", "gasto", "no_reconocido"]


@dataclass(frozen=True)
class ItemCapitalLimpio:
    fecha: str
    concepto: str
    valor_a_demandar: float
    nota: str
    mes_corte: str
    clase_concepto: ClaseConcepto
    indice_fila: int | None = None


@dataclass
class ResultadoMoraCapitalLimpio:
    fecha_inicio_mora: str
    capital_limpio_a_demandar: list[ItemCapitalLimpio]
    total_capital_demandado: float
    bolsa_global_inicial: float
    bolsa_remanente_final: float
    errores_procesamiento: list[str] = field(default_factory=list)

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
KW_INTERESES: frozenset[str] = frozenset(
    {
        "INTERES",
        "INTERESES",
        "INT",
        "MORA",
        "MORAS",
        "MORATORIO",
        "MORATORIA",
        "SANCION",
        "SANCIONES",
        "MULTA",
        "MULTAS",
        "PENALIDAD",
        "PENALIDADES",
    }
)
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


def _redondear_cop(valor: float) -> float:
    return round(float(valor), _PESOS_DECIMALES)


def _normalizar_texto(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and (math.isnan(valor) or math.isinf(valor)):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.upper().strip()
    texto = re.sub(r"[^A-Z0-9 ]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _tokens(concepto_norm: str) -> list[str]:
    return concepto_norm.split() if concepto_norm else []


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

    m = re.match(r"^(\d{4})\.(\d{1,2})(?:\.(\d{1,2}))?", texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", texto)
    if m:
        return _mes_valido(int(m.group(3)), int(m.group(2)))

    m = re.match(r"^(\d{4})[.-](\d{1,2})$", texto)
    if m:
        return _mes_valido(int(m.group(1)), int(m.group(2)))

    return None


def _token_en_kw(token: str, keywords: frozenset[str]) -> bool:
    """Match exacto o prefijo seguro (SANCION*, INTERES*). Evita INT⊂PINTURA."""
    if token in keywords:
        return True
    for kw in ("SANCION", "INTERES", "MULTA", "PENALIDAD", "MORA"):
        if kw in keywords and token.startswith(kw):
            return True
    return False


def _clasificar_concepto(concepto_raw: Any) -> ClaseConcepto:
    """
    Prioridad: interés > gasto > capital > no_reconocido.
    Usa tokens normalizados (NFKD) — nunca substring corto suelto tipo INT.
    """
    normal = _normalizar_texto(concepto_raw)
    if not normal:
        return "no_reconocido"
    toks = _tokens(normal)

    if any(_token_en_kw(t, KW_INTERESES) for t in toks):
        return "interes"
    if any(t.startswith("INTERES") for t in toks):
        return "interes"

    if any(_token_en_kw(t, KW_GASTOS) for t in toks):
        return "gasto"
    if any(_token_en_kw(t, KW_CAPITAL) for t in toks):
        return "capital"
    return "no_reconocido"


def clasificar_concepto_bolsa(concepto_raw: Any) -> ClaseConcepto:
    """API pública de clasificación (útil en tests y wrappers)."""
    return _clasificar_concepto(concepto_raw)


def determinar_mora_y_capital_limpio(
    rows: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """
    Método Bolsa Global (Art. 1653 / 1655 CC) — reglas estrictas:

    1. Sumar todos los Abono → bolsa.
    2. Agrupar Valor > 0 por mes YYYY.MM.
    3. Clasificar: intereses / capital / gastos; no reconocido → alerta + capital.
    4. Consumir mes a mes: intereses primero, luego capital.
    5. Mes Limpio: bolsa no cubre todos los intereses → residual intereses
       perdonado; capital del mes 100% intacto; ese mes = fecha_inicio_mora.
    6. Con bolsa = 0: solo capital futuro; intereses posteriores descartados.
    7. No crash; errores por fila; corruptos → 0.0.

    Retorna dict (vía ResultadoMoraCapitalLimpio.as_dict) para consumo fácil
    desde JSON/API sin acoplar el Excel de descarga PDF→Excel.
    """
    errores: list[str] = []
    bolsa = 0.0
    meses: dict[str, _GrupoMes] = {}
    capital_fecha_invalida: list[_ItemMes] = []

    if rows is None:
        errores.append("Entrada 'rows' es None; se procesa como lista vacía.")
        rows = []

    if not isinstance(rows, (list, tuple)):
        try:
            rows = list(rows)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            return ResultadoMoraCapitalLimpio(
                fecha_inicio_mora="Sin deuda",
                capital_limpio_a_demandar=[],
                total_capital_demandado=0.0,
                bolsa_global_inicial=0.0,
                bolsa_remanente_final=0.0,
                errores_procesamiento=[f"Entrada 'rows' no iterable: {exc}"],
            ).as_dict()

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

            clase = _clasificar_concepto(concepto_raw)
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

    bolsa_inicial = _redondear_cop(bolsa)
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
                # Cubre intereses; remanente reduce capital
                sobrante = _redondear_cop(bolsa - total_intereses)
                bolsa = 0.0
                capitales_restantes = 0.0
                for item in grupo.capitales:
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
                                nota="Capital reducido por remanente de abono.",
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

    total = _redondear_cop(sum(i.valor_a_demandar for i in capital_limpio))

    return ResultadoMoraCapitalLimpio(
        fecha_inicio_mora=fecha_inicio_mora or "Sin deuda",
        capital_limpio_a_demandar=capital_limpio,
        total_capital_demandado=total,
        bolsa_global_inicial=bolsa_inicial,
        bolsa_remanente_final=_redondear_cop(max(0.0, bolsa)),
        errores_procesamiento=errores,
    ).as_dict()
