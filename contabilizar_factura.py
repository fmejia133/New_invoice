# contabilizar_factura.py
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from openai import OpenAI
import os
import json
import pandas as pd
import httpx  # Import httpx to create a custom client

# ------------------ CONFIGURACIÓN ------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
AZURE_KEY = os.environ["AZURE_KEY"]
AZURE_ENDPOINT = os.environ["AZURE_ENDPOINT"]
AZURE_MODEL_ID = os.environ["AZURE_MODEL_ID"]

# =====================  IVA DESCONTABLE: helpers (minimal)  =====================
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class IVAAccount:
    numero: str
    nombre: str

ACCOUNTS_IVA = {
    ("compras",   0.19): IVAAccount("24080501", "IVA DESCONTABLE POR COMPRAS 19%"),
    ("gastos",    0.19): IVAAccount("24080502", "IVA DESCONTABLE POR GASTOS 19%"),
    ("servicios", 0.19): IVAAccount("24080503", "IVA DESCONTABLE POR SERVICIOS 19%"),
    ("compras",   0.05): IVAAccount("24080505", "IVA DESCONTABLE POR COMPRAS 5%"),
    ("gastos",    0.05): IVAAccount("24080506", "IVA DESCONTABLE POR GASTOS 5%"),
    ("servicios", 0.05): IVAAccount("24080507", "IVA DESCONTABLE POR SERVICIOS 5%"),
}

import unicodedata, re

def _norm_simple(s: str) -> str:
    s = (s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s

def normalize_retention_category(cat: str, descripcion: str = "", cuenta: str = "") -> str:
    """
    Coerce GPT outputs (e.g., 'Servicios 1 %', 'SERVICIOS FLETES 1%') to your exact keys.
    Falls back to 'SERVICIOS 1%' when the description looks like fletes/transporte.
    """
    KEYS = {
        "PERSONAS JURIDICAS 11%": "PERSONAS JURIDICAS 11%",
        "PERSONAS NO DECLARANTES PN 10%": "PERSONAS NO DECLARANTES PN 10%",
        "SERVICIOS 1%": "SERVICIOS 1%",
        "SERVICIOS 4%": "SERVICIOS 4%",
        "SERVICIOS 2%": "SERVICIOS 2%",
        "SERVICIOS 3.5%": "SERVICIOS 3.5%",
        "ARRENDAMIENTO BIENES INMUEBLES 3.5%": "ARRENDAMIENTO BIENES INMUEBLES 3.5%",
        "ARRENDAMIENTO BIENES MUEBLES 4%": "ARRENDAMIENTO BIENES MUEBLES 4%",
        "COMBUSTIBLE 0.1%": "COMBUSTIBLE 0.1%",
        "COMPRAS 2.5%": "COMPRAS 2.5%",
        "COMPRAS 1.5%": "COMPRAS 1.5%",
    }

    t = _norm_simple(cat)
    if t in KEYS:
        return KEYS[t]

    # Fix spacing like 'SERVICIOS 1 %'
    t2 = t.replace(" %", "%")
    if t2 in KEYS:
        return KEYS[t2]

    # Heuristics for fletes/transporte
    d = _norm_simple(descripcion)
    if any(k in d for k in ("FLETE", "FLETES", "TRANSPORTE", "ACARREO")):
        return "SERVICIOS 1%"

    # Heuristic on debit account family (e.g., 5235… -> transporte/fletes)
    c = str(cuenta or "")
    if c.startswith(("5235", "5105")):
        return "SERVICIOS 1%"

    return ""  # unknown


import re
import unicodedata

def _norm_txt(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


import re
import unicodedata

def es_autorretenedor_renta(texto: str) -> bool:
    """
    TRUE only if the supplier explicitly says AUTORRETENEDOR DE RENTA.
    Handles 'iva\\nNo', glued NO, accents, and avoids confusing ICA with renta.
    """
    s = (texto or "")
    # Normalize newlines/tabs first so 'iva\\nNo' -> 'iva No'
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    # Remove accents, lowercase, collapse whitespace
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\s+", " ", s).strip()

    # Explicit ICA => NOT renta
    if "autorretenedor de ica" in s or "autoretenedor de ica" in s:
        return False

    # Robust negations: "... no somos autorretenedores", "... autorretenedores ... no"
    if re.search(r"\bno\b\s*(somos|soy|es)?\s*autor?retened(?:or|ores)\b", s):
        return False
    if re.search(r"\bautor?retened(?:or|ores)\b.{0,12}\bno\b", s):
        return False

    # Positive renta statements
    if "autorretenedor de renta" in s or "autoretenedor de renta" in s:
        return True
    if "autorretencion a titulo de renta" in s or "autorretencion renta" in s:
        return True

    return False
import os
import re
import unicodedata
import pandas as pd
from functools import lru_cache

# ======================  Helpers de normalización  ======================

def _strip_accents_lower(s: str) -> str:
    s = (s or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def _only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

import re
import unicodedata

def _norm_basic(s: str) -> str:
    s = (s or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().upper()

def es_flete(descripcion: str, proveedor: str = "") -> bool:
    """Detecta si la factura es de fletes/transporte por keywords conservadoras."""
    t = _norm_basic(descripcion + " " + (proveedor or ""))
    KW = ("FLETE", "FLETES", "TRANSPORTE", "ACARREO")
    return any(k in t for k in KW)

def extraer_origen(origen_destino: str) -> str:
    """
    Intenta extraer el ORIGEN del campo 'Origen-Destino'.
    Admite separadores comunes: '->', '→', '-', ' a ', '/', ','.
    Retorna cadena MAYÚSCULAS (sin tildes) del origen o '' si no se pudo.
    """
    t = (origen_destino or "").strip()
    if not t:
        return ""
    # Normaliza tildes/espacios para detectar separadores de forma robusta
    t_norm = _norm_basic(t)

    # Reemplaza separadores a un único símbolo '→'
    seps = [r"\s*->\s*", r"\s*→\s*", r"\s*-\s*", r"\s+A\s+", r"\s*/\s*", r"\s*,\s*"]
    for pat in seps:
        t_norm = re.sub(pat, " → ", t_norm)

    parts = [p for p in t_norm.split("→") if p and p.strip()]
    if not parts:
        return ""
    # ORIGEN = primer tramo
    return parts[0].strip()

def calcular_ica_bomberil_consolidado(campos: dict, base_subtotal: float):
    """
    Territorialidad ICA (criterio operativo):
      • Si ES FLETE/TRANSPORTE: usar SOLO el ORIGEN del campo 'Origen-Destino'.
        - Si NO hay ORIGEN, NO practicar reteICA Ibagué (ni bomberil): retorno (0.0, 0.0).
      • Si NO es flete: usar la 'Ciudad' del proveedor como hasta ahora.

    Delega el cálculo real en tu función existente `calcular_ica_bomberil(campos_mod, base_subtotal)`.
    """
    # Entradas base
    descripcion = (campos.get("Descripcion") or campos.get("Descripción") or "").strip()
    proveedor   = (campos.get("Proveedor") or "").strip()
    od          = (campos.get("Origen-Destino") or campos.get("Origen - Destino") or "").strip()
    ciudad_prov = (campos.get("Ciudad") or "").strip()

    if es_flete(descripcion, proveedor):
        # Transporte: la territorialidad se fija en el ORIGEN del viaje.
        origen = extraer_origen(od)
        if origen:
            tmp = dict(campos)
            tmp["Ciudad"] = origen  # fuerza la ciudad efectiva al ORIGEN
            return calcular_ica_bomberil(tmp, base_subtotal)
        else:
            # Sin ORIGEN, no se puede determinar territorialidad -> no reteICA Ibagué
            print("Debug - ICA: flete sin ORIGEN; no se practica reteICA/bomberil (pendiente confirmar origen).")
            return 0.0, 0.0
    else:
        # No es flete: mantenemos la ciudad del proveedor
        tmp = dict(campos)
        tmp["Ciudad"] = ciudad_prov
        return calcular_ica_bomberil(tmp, base_subtotal)


# ======================  Detección ciudad Ibagué  =======================

def proveedor_en_ibague(ciudad_texto: str) -> bool:
    """Devuelve True si el texto de ciudad contiene 'Ibagué/Ibague'."""
    t = _strip_accents_lower(ciudad_texto)
    return "ibague" in t or "ibague," in t or t.endswith(" ibague")

# ===================  Detección autorretenedor de ICA  ===================

import re, unicodedata

def _norm_ica_txt(s: str) -> str:
    s = (s or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def es_autorretenedor_ica(texto: str) -> bool:
    """
    True si el proveedor declara ser autorretenedor/retendedor de ICA
    o incluye una advertencia de 'no practicar reteICA'.
    """
    t = _norm_ica_txt(texto)

    # Mensajes de 'no practicar retención' (bloquea reteICA)
    if ("abstenerse de efectuar retencion de ica" in t or
        "abstenerse de efectuar retencion ica" in t or
        "no efectuar retencion de ica" in t or
        "no practicar reteica" in t):
        return True  # tratar como autorretenedor a efectos de no retener

    # Declaraciones explícitas de autorretención/retención en ICA
    keys = [
        "retendedor de ica",
        "retenedor de ica",
        "agente retenedor de ica",
        "agente de retencion en ica",
        "agente de retencion de ica",
        "somos autorretenedores de ica",
        "autorretenedor de ica",
        "autoretenedor de ica",
    ]
    if any(k in t for k in keys):
        return True

    # Evita falsos positivos por negaciones directas
    if re.search(r"\bno\b\s*(somos|soy|es)?\s*(autorretened|retened)", t):
        return False

    return False

# =====================  Parseo de CIIU desde actividad  =====================

import re, unicodedata

def parse_ciiu(actividad_economica: str) -> str:
    """
    Extracts the 4-digit CIIU class from strings like:
    'Actividad Económica 2511 ... Tarifa 5'  -> '2511'
    Avoids picking trailing tariff digits.
    """
    s = (actividad_economica or "")
    s = s.replace("\r"," ").replace("\n"," ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    # Prefer a 4-digit right after 'CIIU' or 'Actividad Económica'
    m = re.search(r'(?:\bCIIU\b|\bActividad\s*Econ(?:o?mica)?)\D{0,10}(\d{4})(?!\d)', s, flags=re.I)
    if m:
        return m.group(1)

    # Else take the first standalone 4-digit block (not followed by another digit)
    m = re.search(r'\b(\d{4})(?!\d)\b', s)
    if m:
        return m.group(1)

    # Fallback: first 4 of all digits if nothing else
    digits = re.sub(r'\D','', s)
    return digits[:4] if len(digits) >= 4 else ""

# ====================  Carga tarifas ICA Ibagué (CSV)  ====================

@lru_cache(maxsize=1)
def _load_tarifas_ica_ibague(csv_path: str = "tarifas_ica_ibague.csv") -> pd.DataFrame:
    """
    Carga el CSV y normaliza nombres de columnas.
    Columnas aceptadas (flexible):
      - ciiu
      - reteica_tarifa | tarifa | tarifa_por_mil
      - base_minima | base
      - bomberil_tarifa | bomberil | tasa_bomberil
    Las tarifas pueden venir en %, por mil o decimales.
    """
    df = pd.read_csv(csv_path, encoding="utf-8")
    cols = {c.lower().strip(): c for c in df.columns}
    def pick(*names):
        for n in names:
            if n in cols: 
                return cols[n]
        return None

    col_ciiu = pick("ciiu")
    col_tarifa = pick("reteica_tarifa", "tarifa", "tarifa_por_mil")
    col_base  = pick("base_minima", "base")
    col_bomb  = pick("bomberil_tarifa", "bomberil", "tasa_bomberil")

    if not col_ciiu or not col_tarifa:
        raise ValueError("CSV de tarifas ICA: faltan columnas obligatorias (ciiu, tarifa).")

    df["_ciiu"] = df[col_ciiu].astype(str).str.strip().str[:6]
    # Normaliza tarifas a DECIMAL (0.XX)
    def _to_rate(x):
        try:
            v = float(str(x).replace(",", "."))
        except:
            return 0.0
        # Heurística: si v > 1, puede venir en 'por mil' o en '%'
        # Intento 1: por mil
        if v > 1.5:  # ej. 8.3 por mil => 0.0083 ; 11 => 0.011
            # Si está claramente <= 100 y no es por mil alto, asúmelo %:
            # - Si 8.3: puede ser por mil (0.0083) o 8.3% (0.083).
            #   La práctica en ICA suele ser por mil. Priorizamos por mil.
            return v / 1000.0
        return v  # ya decimal (ej. 0.0083)
    df["_tarifa"] = df[col_tarifa].apply(_to_rate)

    df["_base_min"] = 0.0
    if col_base:
        def _to_money(x):
            s = str(x).replace(",", "").replace(".", "")
            try:
                return float(re.sub(r"[^0-9.]", "", str(x)).replace(",", ""))  # flexible
            except:
                return 0.0
        df["_base_min"] = df[col_base].apply(_to_money)

    df["_bomberil"] = 0.0
    if col_bomb:
        def _to_bomb(x):
            try:
                v = float(str(x).replace(",", "."))
            except:
                return 0.0
            # bomberil casi siempre es % del ICA; si >1, interpretamos % (20 -> 0.20)
            return v / 100.0 if v > 1 else v
        df["_bomberil"] = df[col_bomb].apply(_to_bomb)

    return df[["_ciiu", "_tarifa", "_base_min", "_bomberil"]]

def _lookup_tarifas_ibague(ciiu: str):
    df = _load_tarifas_ica_ibague()
    row = df[df["_ciiu"] == (ciiu or "")].head(1)
    if row.empty:
        return 0.0, 0.0, 0.0
    return float(row["_tarifa"].iloc[0]), float(row["_base_min"].iloc[0]), float(row["_bomberil"].iloc[0])

# ===================  Cálculo ICA + Tasa Bomberil  ===================

# Cuentas por defecto (puedes mover a .env/variables)
ICA_ACCOUNT_DEFAULT       = os.getenv("PUC_RETEICA_IBAGUE", "2368050000")   # Pasivo: ReteICA Ibagué
BOMBERIL_ACCOUNT_DEFAULT  = os.getenv("PUC_BOMBERIL_IBAGUE", "2368400000")  # Pasivo: Sobretasa bomberil Ibagué

def calcular_ica_bomberil(campos: dict, base_subtotal: float):
    """
    Retorna (reteICA_val, bomberil_val, cuenta_ica, cuenta_bomberil, motes) para logging.
    Requiere: Ciudad, Actividad Económica (CIIU), Régimen Tributario.
    """
    ciudad = campos.get("Ciudad", "")
    actividad = campos.get("Actividad Economica", "") or campos.get("Actividad Económica", "")
    regimen = campos.get("Regimen Tributario", "") or campos.get("Régimen Tributario", "")

    # 1) Territorialidad práctica de tu cliente: retener ICA Ibagué solo si proveedor está en Ibagué
    if not proveedor_en_ibague(ciudad):
        return 0.0, 0.0, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, "Proveedor NO domiciliado en Ibagué"

    # 2) Exclusiones: autorretenedor ICA o RST
    if es_autorretenedor_ica(regimen):
        return 0.0, 0.0, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, "Autorretenedor ICA"
    try:
        is_simple = es_regimen_simple(regimen)  # usa tu función existente
    except NameError:
        # Fallback simple si no existiera
        t = _strip_accents_lower(regimen)
        is_simple = ("regimen simple" in t) or ("régimen simple" in t) or ("simple" in t)
    if is_simple:
        return 0.0, 0.0, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, "Régimen Simple"

    # 3) Tarifa por CIIU
    ciiu = parse_ciiu(actividad)
    tarifa_ica, base_min, tarifa_bomb = _lookup_tarifas_ibague(ciiu)
    if tarifa_ica <= 0.0:
        return 0.0, 0.0, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, f"Tarifa ICA=0 para CIIU {ciiu or 'N/A'}"
    if base_subtotal <= (base_min or 0.0):
        return 0.0, 0.0, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, f"Base menor a base_mínima ({base_min})"

    # 4) Cálculo
    reteica_val = round(base_subtotal * tarifa_ica, 2)
    bomberil_val = round(reteica_val * (tarifa_bomb or 0.0), 2)

    return reteica_val, bomberil_val, ICA_ACCOUNT_DEFAULT, BOMBERIL_ACCOUNT_DEFAULT, f"OK CIIU {ciiu}, tarifa {tarifa_ica}, bomberil {tarifa_bomb}"



def _normalize_tx_type(s: str) -> str:
    s = (s or "").strip().lower()
    if s in {"servicio", "servicios", "service", "services"}:
        return "servicios"
    return "bienes"

def _clean_cuenta(code: str) -> str:
    return (code or "").replace("-", "").replace(" ", "").strip()

def _is_inventory_account(cuenta_debito: str) -> bool:
    return _clean_cuenta(cuenta_debito).startswith("14")

def _is_pnl_account(cuenta_debito: str) -> bool:
    c = _clean_cuenta(cuenta_debito)
    return (c.startswith("5") or c.startswith("6") or c.startswith("7")) and not c.startswith("14")

def detect_iva_rate(iva_valor: float, subtotal: float, tolerance: float = 0.01) -> Optional[float]:
    try:
        iva_val = float(iva_valor or 0)
        sub = float(subtotal or 0)
    except Exception:
        return None
    if iva_val <= 0 or sub <= 0:
        return None
    def close(a, b): return abs(a - b) <= tolerance * max(b, 1e-9)
    if close(iva_val, sub * 0.19): return 0.19
    if close(iva_val, sub * 0.05): return 0.05
    return None

def get_iva_account(transaction_type_from_gpt: str, cuenta_debito: str, iva_valor: float, subtotal: float) -> Optional[IVAAccount]:
    rate = detect_iva_rate(iva_valor, subtotal)
    if rate is None:
        return None
    tx = _normalize_tx_type(transaction_type_from_gpt)
    if tx == "servicios":
        key = ("servicios", rate)
    else:
        if _is_inventory_account(cuenta_debito): key = ("compras", rate)
        elif _is_pnl_account(cuenta_debito):     key = ("gastos", rate)
        else:                                    key = ("gastos", rate)
    return ACCOUNTS_IVA.get(key)

def build_iva_asiento_line(
    transaction_type_from_gpt: str,
    cuenta_debito: str,
    iva_valor: float,
    subtotal: float,
    tercero: str = "",
    detalle: str = "IVA descontable"
):
    acc = get_iva_account(transaction_type_from_gpt, cuenta_debito, iva_valor, subtotal)
    if not acc or not iva_valor:
        return None
    # IMPORTANT: lowercase keys to match the rest of your app (validar_balance, etc.)
    return {
        "cuenta": acc.numero,
        "nombre": acc.nombre,
        "debito": round(float(iva_valor), 2),
        "credito": 0.0,
        "Cantidad (Kg)": 0,
        "Tercero": tercero or "",
        "Detalle": detalle or "IVA descontable",
    }
# ===================  /IVA DESCONTABLE: helpers  =====================

# =====================  CxP selection from pairs (data-driven)  =====================
import os
import pandas as pd
from functools import lru_cache

def _normalize_code(x) -> str:
    s = str(x).strip()
    return "".join(ch for ch in s if ch.isdigit())

@lru_cache(maxsize=1)
def _load_ap_pairs(csv_path: str):
    # Robust load (encoding + flexible columns)
    last_err = None
    for enc in ("latin-1", "cp1252", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(csv_path, encoding=enc)
            break
        except Exception as e:
            last_err = e
            df = None
    if df is None:
        raise last_err

    def _find(cols, *needles):
        lc = {c.lower(): c for c in cols}
        for k, v in lc.items():
            if all(n in k for n in needles):
                return v
        return None

    deb_col = _find(df.columns, "debito", "cuenta")
    ap_col  = _find(df.columns, "ap", "cuenta")
    ap_desc_col = _find(df.columns, "ap", "descr")

    if not deb_col or not ap_col:
        raise ValueError("CSV must include columns for 'Débito - Cuenta' and 'AP - Cuenta'.")

    work = df[[deb_col, ap_col] + ([ap_desc_col] if ap_desc_col else [])].dropna(how="all").copy()
    work["deb_code"] = work[deb_col].apply(_normalize_code)
    work["ap_code"]  = work[ap_col].apply(_normalize_code)
    work["ap_desc"]  = (work[ap_desc_col] if ap_desc_col else "").astype(str).str.strip()
    work = work[(work["deb_code"] != "") & (work["ap_code"] != "")]
    work["ap_desc_len"] = work["ap_desc"].str.len()

    # Exact mapping: choose most frequent; tiebreak by longer description
    freq = (
        work.groupby(["deb_code", "ap_code", "ap_desc"])
            .size().reset_index(name="n")
    )
    freq = freq.merge(
        work[["ap_code", "ap_desc", "ap_desc_len"]].drop_duplicates(),
        on=["ap_code", "ap_desc"], how="left"
    )

    exact_map = {}
    for deb, grp in freq.groupby("deb_code"):
        grp = grp.sort_values(["n", "ap_desc_len"], ascending=[False, False])
        exact_map[deb] = grp.iloc[0]["ap_code"]

    # Canonical AP name (by frequency then length)
    ap_name_rank = (
        work.groupby(["ap_code", "ap_desc"])
            .size().reset_index(name="n")
            .merge(work[["ap_code","ap_desc","ap_desc_len"]].drop_duplicates(),
                   on=["ap_code","ap_desc"], how="left")
            .sort_values(["ap_code","n","ap_desc_len"], ascending=[True, False, False])
            .drop_duplicates(subset=["ap_code"], keep="first")
    )
    ap_name_map = dict(zip(ap_name_rank["ap_code"], ap_name_rank["ap_desc"]))

    # Prefix maps for fallbacks 10→…→4
    prefix_maps = {}
    for plen in (10, 9, 8, 7, 6, 5, 4):
        tmp = work.copy()
        tmp["deb_prefix"] = tmp["deb_code"].str[:plen]
        pfreq = (
            tmp.groupby(["deb_prefix", "ap_code", "ap_desc"])
               .size().reset_index(name="n")
        )
        pfreq = pfreq.merge(
            tmp[["ap_code","ap_desc","ap_desc_len"]].drop_duplicates(),
            on=["ap_code","ap_desc"], how="left"
        ).sort_values(["deb_prefix","n","ap_desc_len"], ascending=[True, False, False])

        best_pref = {}
        for pref, g in pfreq.groupby("deb_prefix"):
            if not isinstance(pref, str) or pref == "":
                continue
            best_pref[pref] = g.iloc[0]["ap_code"]
        prefix_maps[plen] = best_pref

    return exact_map, prefix_maps, ap_name_map

def seleccionar_cuenta_cxp_por_pares(cuenta_debito: str,
                                     csv_path: str = "Pares_Debito-AP_extra_dos.csv",
                                     fallback: str = "220505") -> tuple[str, str]:
    """
    Returns (ap_code, ap_name) using the pairs CSV:
      1) exact debit match,
      2) prefix fallback (10→9→…→4),
      3) fallback account.
    Prints a small diagnostic about how it matched.
    """
    # Resolve CSV path relative to this file to avoid working-dir issues
    here = os.path.dirname(os.path.abspath(__file__))
    candidate_paths = [
        csv_path,
        os.path.join(here, csv_path),
        os.path.join(here, os.path.basename(csv_path)),
    ]
    real_csv = next((p for p in candidate_paths if os.path.exists(p)), None)
    if real_csv is None:
        print(f"[CxP map] CSV not found. Tried: {candidate_paths}. Using fallback {fallback}.")
        return fallback, "Cuentas por pagar - Proveedores"

    try:
        exact_map, prefix_maps, ap_name_map = _load_ap_pairs(real_csv)
    except Exception as e:
        print(f"[CxP map] Failed to load '{real_csv}': {e}. Using fallback {fallback}.")
        return fallback, "Cuentas por pagar - Proveedores"

    deb = _normalize_code(cuenta_debito)
    mode = "fallback"
    ap = exact_map.get(deb)
    if ap:
        mode = "exact"
    else:
        for plen in (10, 9, 8, 7, 6, 5, 4):
            if len(deb) < plen:
                continue  # skip impossible prefix length
            pref = deb[:plen]
            ap = prefix_maps.get(plen, {}).get(pref)
            if ap:
                mode = f"prefix-{plen}"
                break

    if not ap:
        ap = fallback

    name = ap_name_map.get(ap, "Cuentas por pagar - Proveedores")
    print(f"[CxP map] debit {deb} -> AP {ap} ({mode}) from {os.path.basename(real_csv)}")
    return ap, name
# =================== /CxP selection from pairs  =====================


# Debug prints to verify values and environment
print("OPENAI_API_KEY:", OPENAI_API_KEY)
print("AZURE_KEY:", AZURE_KEY)
print("AZURE_ENDPOINT:", AZURE_ENDPOINT)
print("AZURE_MODEL_ID:", AZURE_MODEL_ID)
print("Environment vars related to proxies:", {k: v for k, v in os.environ.items() if 'PROXY' in k.upper()})

# Initialize OpenAI client with a custom httpx client (no proxies by default)
http_client = httpx.Client()  # Default client without proxies unless env vars are set
client_openai = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=http_client
)

# FUNCIONES AUXILIARES
def to_float(valor):
    try:
        return float(str(valor).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0

def es_regimen_simple(texto):
    texto = texto.lower()
    return "régimen simple" in texto or "regimen simple" in texto or "simple" in texto

def es_autorretenedor_ica(texto):
    texto = texto.lower()
    return any(p in texto for p in ["autorretenedor", "tarifa 0", "no aplicar", "regimen simple", "régimen simple"])

import os, re
import pandas as pd

import os, re
import pandas as pd

import os, re
import pandas as pd

import os, re, pandas as pd

def obtener_tarifa_ica(codigo_ciiu, path="tarifas_ica_ibague.csv"):
    """
    Devuelve la tarifa ICA en DECIMAL (p.ej., 8.3‰ -> 0.0083) para el CIIU dado.
    Lee CSV o XLSX. Tolera encabezados: CIIU / codigo / actividad, y tarifa / tarifa_por_mil.
    Incluye prints de diagnóstico para ver ruta, columnas y valor crudo leído.
    """
    import os, re
    import pandas as pd

    # --- Resolver archivo (CSV junto al .py; si no, XLSX; o 'path' si viene) ---
    base_dir = os.path.dirname(os.path.abspath(__file__))
    default_csv  = os.path.join(base_dir, "tarifas_ica_ibague.csv")
    default_xlsx = os.path.join(base_dir, "tarifas_ica_ibague.xlsx")
    chosen = path or (default_csv if os.path.exists(default_csv) else default_xlsx)
    ext = os.path.splitext(chosen)[1].lower()

    # --- Abrir robusto (CSV/XLSX + encoding fallback) ---
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(chosen, dtype=str)
        else:
            try:
                df = pd.read_csv(chosen, dtype=str, sep=None, engine="python",
                                 encoding="utf-8", keep_default_na=False)
            except Exception:
                df = pd.read_csv(chosen, dtype=str, sep=None, engine="python",
                                 encoding="latin-1", keep_default_na=False)
    except Exception as e:
        # Intento alterno: si era CSV y existe XLSX al lado, probar XLSX
        if ext not in (".xlsx", ".xls") and os.path.exists(default_xlsx):
            try:
                df = pd.read_excel(default_xlsx, dtype=str)
                chosen = default_xlsx
                ext = ".xlsx"
            except Exception as e2:
                print("DEBUG TARIFAS: cannot open:", os.path.abspath(chosen), "error:", e2)
                return 0.0
        else:
            print("DEBUG TARIFAS: cannot open:", os.path.abspath(chosen), "error:", e)
            return 0.0

    print("DEBUG TARIFAS: reading", os.path.abspath(chosen), "columns:", list(df.columns))

    # --- Selección de columnas tolerante a nombres ---
    def k(s): return re.sub(r'[\s_]+', '', str(s).lower().strip())
    colmap = {k(c): c for c in df.columns}

    col_ciiu = None
    for key in ("ciiu", "codigociiu", "codigo", "actividadeconomica", "actividad"):
        if key in colmap:
            col_ciiu = colmap[key]; break

    col_tar = None
    for key in ("tarifa_por_mil", "tarifa", "reteica_tarifa"):
        if key in colmap:
            col_tar = colmap[key]; break

    if not col_ciiu or not col_tar:
        print("DEBUG TARIFAS: missing columns. Have:", list(df.columns), "| need CIIU + tarifa/tarifa_por_mil")
        return 0.0

    # --- CIIU a 4 dígitos (evita '25115' por dígito pegado) ---
    m = re.search(r'\d{4}', str(codigo_ciiu) or "")
    code = m.group(0) if m else ""
    print("DEBUG TARIFAS: CIIU searched:", code)
    if not code:
        return 0.0

    df["_ciiu_4"] = df[col_ciiu].astype(str).str.extract(r'(\d{4})', expand=True)[0]
    row = df[df["_ciiu_4"] == code].head(1)
    print("DEBUG TARIFAS: matches:", len(row))
    if row.empty:
        return 0.0

    # --- Leer la celda de tarifa y normalizar ---
    raw_tar = str(row.iloc[0][col_tar]).strip()
    raw_std = raw_tar.replace(" ", "").replace(",", ".")
    print(f"DEBUG TARIFAS: col_tar='{col_tar}' raw='{raw_tar}' std='{raw_std}'")

    try:
        if raw_std.endswith("%"):
            v = float(raw_std[:-1])
            tarifa = v / 100.0
        else:
            v = float(raw_std)
            # Si el encabezado dice 'por mil' -> dividir entre 1000. Si no, heurística:
            if k(col_tar) == "tarifapormil" or "por_mil" in col_tar.lower() or "pormil" in col_tar.lower():
                tarifa = v / 1000.0            # 1 -> 0.001 ; 8.3 -> 0.0083
            else:
                tarifa = v if v <= 1.0 else v / 1000.0  # 0.0083 -> 0.0083 ; 8.3 -> 0.0083
    except Exception as e:
        print("DEBUG TARIFAS: cannot parse tariff value:", raw_tar, "error:", e)
        return 0.0

    print("DEBUG TARIFAS: resolved tarifa_ica_decimal =", tarifa)
    return round(tarifa, 10)

def clasificar_con_gpt(descripcion, proveedor, origen_destino):
    # Load PUC to include in prompt
    df_puc = pd.read_excel("PUC-CENTRO COSTOS SYNERGY.xlsx", sheet_name="PUC")
    df_puc.columns = df_puc.columns.str.strip()  # Strip column names
    # Strip values and normalize CUENTA by removing hyphens and spaces
    df_puc['CUENTA'] = df_puc['CUENTA'].astype(str).str.replace('-', '').str.strip()
    df_puc['DESCRIPCION'] = df_puc['DESCRIPCION'].astype(str).str.strip()
    # Filter to relevant accounts for efficiency (inventarios, gastos, costos)
    relevant_df = df_puc[df_puc["CUENTA"].str.startswith(('14', '51', '61', '71', '72', '73'), na=False)]
    puc_list = '\n'.join([f"{row['CUENTA']} - {row['DESCRIPCION']}" for _, row in relevant_df.iterrows() if pd.notna(row['DESCRIPCION'])])

    prompt = f"""
Eres un contador profesional en Colombia que trabajas en un molino de arroz que también fabrica maquinas empaquetadoras de granos. Basado únicamente en esta descripción de factura:
\"{descripcion}\"
y el proveedor:
\"{proveedor}\"
y el origen-destino:
\"{origen_destino}\"
Clasifícala y selecciona la cuenta PUC apropiada para el débito principal (el subtotal de la compra) del siguiente listado de cuentas de la empresa, y determina la categoría de retención en la fuente sobre renta según las reglas DIAN.
La descripción es lo más importante porque indica la naturaleza del producto o servicio.
Usa el nombre del proveedor para refinar, por ejemplo, si el proveedor incluye "Transportadora" or "Transportes", es probable un servicio de fletes, usa una cuenta como 513550 or 613535 or 733550 for transporte, fletes y acarreos, dependiendo si es gasto admin, costo venta or producción.
Reglas:
- La principal materia prima del molino de arroz es el arroz paddy que debe ser registrado en una cuenta de inventario (14xx) y clasificado como 'COMPRAS 1.5%' para retención, incluyendo productos relacionados como fungicidas.
- Las facturas de transporte de arroz son de arroz blanco y deben ser registradas en la cuenta 5235500000 TRANSPORTE  FLETES Y ACARREOS  y clasificadas como 'SERVICIOS 1%' para retención
- Elige la cuenta y categoría exactamente como aparece en la lista, la más apropiada para débito: para inventarios (14xx), gastos operacionales de administración (51xx), etc.
- Categorías de retención: Use exactly one of these categories based on the description and examples:
  - 'PERSONAS JURIDICAS 11%': Honorarios o servicios de consultoria.
  - 'PERSONAS NO DECLARANTES PN 10%': Honorarios o servicios de consultoria for personas naturales no declarantes hasta 3300 UVT, si the factura passes this value it is registered in PERSONAS JURIDICAS 11%.
  - 'SERVICIOS 1%': Fletes, servicio de transporte.
  - 'SERVICIOS 4%': Mantenimiento Reparaciones servicios de aseo y limpieza de declarantes.
  - 'SERVICIOS 2%': Examenes de ingreso de personal.
  - 'SERVICIOS 3.5%': Hoteles hospedaje.
  - 'ARRENDAMIENTO BIENES INMUEBLES 3.5%': Arrendamiento de bodegas, oficinas, casas.
  - 'ARRENDAMIENTO BIENES MUEBLES 4%': Arrendamiento de maquinaria, equipos, estibas, vehiculos.
  - 'COMBUSTIBLE 0.1%': Combustibles.
  - 'COMPRAS 2.5%': Compras de todo tipo de productos que no pertenecen a las demas categorias.
  - 'COMPRAS 1.5%': Arroz Paddy.
- If no match, use 'COMPRAS 2.5%' as default.
Lista de cuentas PUC disponibles de la empresa:
{puc_list}

Devuelve **solo JSON**: {{"cuenta": "codigo_cuenta", "nombre": "nombre_cuenta", "retention_category": "categoria_retencion"}}
Por ejemplo: {{"cuenta": "14051001", "nombre": "MATERIA PRIMA MOLINO", "retention_category": "COMPRAS 1.5%"}}
Your response must be a valid JSON object.
""".strip()

    response = client_openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"}
    )
    resp = response.choices[0].message.content.strip()
    data = json.loads(resp)
    # Normalize returned cuenta by removing hyphens (if any)
    cuenta = data["cuenta"].replace('-', '').strip()
    nombre = data["nombre"].strip()
    retention_category = data["retention_category"]
    tipo_transaccion = (data.get("tipo_transaccion") or "").strip()
    return cuenta, nombre, retention_category, tipo_transaccion

def validar_balance(asiento):
    total_debitos = sum(to_float(l.get("debito", 0)) for l in asiento)
    total_creditos = sum(to_float(l.get("credito", 0)) for l in asiento)
    diferencia = round(total_debitos - total_creditos, 2)
    return diferencia == 0, total_debitos, total_creditos, diferencia

def validar_cuentas_puc(asiento, path_catalogo="PUC-CENTRO COSTOS SYNERGY.xlsx"):
    try:
        df_puc = pd.read_excel(path_catalogo, sheet_name="PUC", dtype={"CUENTA": str})  # Load with explicit dtype
        df_puc.columns = [col.strip() for col in df_puc.columns]  # Strip whitespace from column names
        df_puc["CUENTA"] = df_puc["CUENTA"].str.strip()  # Strip whitespace from CUENTA values
        # Extract only the numeric part of the account code (should already be numeric)
        df_puc["CUENTA"] = df_puc["CUENTA"].str.extract(r'(\d+)', expand=False).fillna('')  # Ensure numeric only
        print(f"Debug - PUC columns (after stripping): {df_puc.columns.tolist()}")  # Verify column names
        print(f"Debug - Unique CUENTA values in PUC: {sorted(df_puc['CUENTA'].unique().tolist())}")  # Log unique accounts
        if "CUENTA" not in df_puc.columns:
            raise KeyError(f"Column 'CUENTA' not found in {path_catalogo}. Available columns: {df_puc.columns.tolist()}")
        cuentas_validas = set(df_puc["CUENTA"])  # Use cleaned numeric codes
        cuentas_asiento = set(str(l["cuenta"]) for l in asiento)
        print(f"Debug - Accounts in asiento: {sorted(cuentas_asiento)}")  # Log accounts from asiento
        cuentas_invalidas = cuentas_asiento - cuentas_validas
        if cuentas_invalidas:
            print(f"Debug - Invalid accounts: {sorted(cuentas_invalidas)}")
        return cuentas_invalidas
    except Exception as e:
        print(f"Error loading PUC catalog: {str(e)}")
        return set()  # Return empty set to avoid crashing if file is invalid

# FUNCION AUXILIAR PARA EXTRAER CAMPOS DE AZURE
def extraer_campos_azure(ruta_pdf):
    document_analysis_client = DocumentAnalysisClient(
        endpoint=AZURE_ENDPOINT,
        credential=AzureKeyCredential(AZURE_KEY)
    )
    with open(ruta_pdf, "rb") as f:
        poller = document_analysis_client.begin_analyze_document(
            model_id=AZURE_MODEL_ID,
            document=f
        )
    result = poller.result()

    campos = {}
    for doc in result.documents:
        for name, field in doc.fields.items():
            if name == "Cantidad":
                # Use field.content for "Cantidad" to get the raw string
                value = field.content if field.content is not None else field.value if field.value is not None else ""
            else:
                value = field.value if field.value is not None else field.content if field.content is not None else ""
            campos[name] = value
    return campos

# ASIENTO CONTABLE
def construir_asiento(campos, cuenta, nombre, retention_category, tipo_transaccion):

    asiento = []
    subtotal = to_float(campos.get("Subtotal"))
    fletes = to_float(campos.get("Fletes", 0))  # New field for freight charges
    adjusted_subtotal = subtotal + fletes  # Combine for debit and retention calculations
    iva_valor = to_float(campos.get("IVA Valor"))
    total_factura = to_float(campos.get("Total Factura"))
    nit = str(campos.get("NIT Proveedor", ""))  # Convert to string to fix TypeError
    proveedor = campos.get("Proveedor", "")
    regimen = campos.get("Regimen Tributario", "")
    ciudad = campos.get("Ciudad", "").lower()
    actividad = campos.get("Actividad Economica", "")
    retefuente_valor = to_float(campos.get("Retefuente Valor"))
    descripcion = campos.get("Descripcion", "").lower()
    origen_destino = campos.get("Origen-Destino", "").lower()
    fomento = to_float(campos.get("Impuesto Fomento", 0))
    original_fomento = fomento  # Initialize outside the conditional block

    # Parse and sum Cantidad for invoices booked to 14051001 (arroz paddy or Casanare)
    cantidad_total = 0
    if cuenta == "14051001":
        cantidad = campos.get("Cantidad", "")
        try:
            quantities = []
            if isinstance(cantidad, str):
                import re
                # Split by newlines and clean each line
                lines = [line.strip() for line in cantidad.split("\n") if line.strip()]
                for line in lines:
                    # Remove thousands separators (commas), keep decimal point
                    clean_line = line.replace(",", "")
                    value = to_float(clean_line)
                    if value > 0:  # Only accept positive quantities
                        quantities.append(value)
                    else:
                        print(f"Debug - Skipping invalid quantity: {line} (negative or zero)")
            elif isinstance(cantidad, (list, tuple)):
                quantities = [to_float(q) for q in cantidad if to_float(q) > 0]
            elif isinstance(cantidad, (int, float)):
                value = to_float(cantidad)
                if value > 0:
                    quantities = [value]
                else:
                    print(f"Debug - Skipping invalid quantity: {cantidad} (negative or zero)")
            else:
                quantities = []
            cantidad_total = round(sum(quantities), 2) if quantities else 0
            print(f"Debug - Cantidad Parsed: Raw={cantidad}, Lines={lines}, Quantities={quantities}, Total={cantidad_total}")
        except Exception as e:
            print(f"Warning - Failed to parse Cantidad: {cantidad}, Error: {str(e)}. Setting cantidad_total to 0.")
            cantidad_total = 0

    # Check for imbalance risk
    expected_total = adjusted_subtotal + iva_valor
    if expected_total != total_factura:
        print(f"Warning - Potential imbalance: Expected Total ({expected_total}) != Azure Total Factura ({total_factura}). Fletes may be already included in Subtotal or Total Factura. Using Subtotal only for safety.")
        adjusted_subtotal = subtotal  # Fallback to subtotal to avoid double-counting

    print(f"Debug - Campos: {campos}, Fletes: {fletes}, Adjusted Subtotal: {adjusted_subtotal}, Fomento: {fomento}, Cantidad Total (Kg): {cantidad_total}")  # Debug output

    # 1. Subtotal + Fletes => gasto/inventario
    if adjusted_subtotal > 0:
        entry = {
            "cuenta": cuenta,
            "nombre": nombre,
            "debito": adjusted_subtotal,
            "credito": 0,
            "Cantidad (Kg)": cantidad_total if cuenta == "14051001" else 0
        }
        asiento.append(entry)
             # === IVA descontable (auto-selección de cuenta) ===
        try:
            iva_valor = float(campos.get("IVA Valor") or 0)
            subtotal  = float(campos.get("Subtotal") or 0)
        except Exception:
            iva_valor, subtotal = 0.0, 0.0

        tipo_tx = (tipo_transaccion or "").strip()
        iva_line = build_iva_asiento_line(
            transaction_type_from_gpt=tipo_tx,
            cuenta_debito=cuenta,  # el débito principal
            iva_valor=iva_valor,
            subtotal=subtotal,
            tercero=str(campos.get("NIT Proveedor") or campos.get("Proveedor") or ""),
            detalle=descripcion or "IVA descontable",
        )
        if iva_line:
            asiento.append(iva_line)
        # ================================================


    
        # 2. Retefuente si aplica
    original_retefuente = retefuente_valor
    calculated_retefuente = 0.0

    # Normalize regimen text and detect flags (handles 'iva\nNo ...')
    regimen_norm = (campos.get("Regimen Tributario", "") or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    is_autorretenedor = es_autorretenedor_renta(regimen_norm)
    is_simple = es_regimen_simple(regimen_norm)

    # Normalize/repair retention category from GPT and fall back for fletes
    cat_in = retention_category
    retention_category = normalize_retention_category(retention_category, descripcion=descripcion, cuenta=str(cuenta))

    print(f"Debug - RTE cat IN='{cat_in}' -> NORM='{retention_category}', "
          f"AutoRenta={is_autorretenedor}, Simple={is_simple}, Base={adjusted_subtotal}, OrigRF={original_retefuente}")

    retention_mapping = {
        "PERSONAS JURIDICAS 11%": {"account": "23651502", "rate": 0.11},
        "PERSONAS NO DECLARANTES PN 10%": {"account": "23651503", "rate": 0.10},
        "SERVICIOS 1%": {"account": "23652501", "rate": 0.01},
        "SERVICIOS 4%": {"account": "23652502", "rate": 0.04},
        "SERVICIOS 2%": {"account": "23652503", "rate": 0.02},
        "SERVICIOS 3.5%": {"account": "23652505", "rate": 0.035},
        "ARRENDAMIENTO BIENES INMUEBLES 3.5%": {"account": "23653004", "rate": 0.035},
        "ARRENDAMIENTO BIENES MUEBLES 4%": {"account": "23653005", "rate": 0.04},
        "COMBUSTIBLE 0.1%": {"account": "23657001", "rate": 0.001},
        "COMPRAS 2.5%": {"account": "23657003", "rate": 0.025},
        "COMPRAS 1.5%": {"account": "23657004", "rate": 0.015},
    }
    retefuente_account = "236520"
    retefuente_name = "Retefuente registrada"

    # Apply retention when: we have a category, Azure didn't bring a retefuente value,
    # base mínima is met, and supplier is NOT autorretenedor and NOT RST.
    if (
        retention_category
        and original_retefuente == 0
        and adjusted_subtotal > 1271000
        and not is_autorretenedor
        and not is_simple
    ):
        if retention_category in retention_mapping:
            retefuente_account = retention_mapping[retention_category]["account"]
            retefuente_name = retention_category
            rate = retention_mapping[retention_category]["rate"]
            calculated_retefuente = round(adjusted_subtotal * rate, 2)
            print(f"Debug - Applying retefuente: {retention_category} -> {rate * 100}% = {calculated_retefuente}")
            asiento.append({
                "cuenta": retefuente_account,
                "nombre": retefuente_name,
                "debito": 0,
                "credito": calculated_retefuente,
                "Cantidad (Kg)": 0
            })
        else:
            print(f"Debug - Unknown retention category after normalize: '{retention_category}'")
    elif retention_category and original_retefuente > 0:
        if retention_category in retention_mapping:
            retefuente_account = retention_mapping[retention_category]["account"]
            retefuente_name = retention_category
        asiento.append({
            "cuenta": retefuente_account,
            "nombre": retefuente_name,
            "debito": 0,
            "credito": original_retefuente,
            "Cantidad (Kg)": 0
        })


    print(f"Debug - Retefuente: Original={original_retefuente}, Calculated={calculated_retefuente}")  # Debug retefuente values

        # === 3) ReteICA Ibagué + Tasa Bomberil (solo si proveedor es de Ibagué) ===
    try:
        reteica_val, bomberil_val, acc_ica, acc_bomb, note_ica = calcular_ica_bomberil_consolidado(campos, adjusted_subtotal)
        print(f"Debug - ICA/Bomberil: {note_ica}, reteICA={reteica_val}, bomberil={bomberil_val}")
        if reteica_val > 0:
            asiento.append({
                "cuenta": acc_ica,
                "nombre": "ReteICA Ibagué",
                "debito": 0.0,
                "credito": reteica_val,
                "Cantidad (Kg)": 0
            })
        if bomberil_val > 0:
            asiento.append({
                "cuenta": acc_bomb,
                "nombre": "Tasa bomberil Ibagué",
                "debito": 0.0,
                "credito": bomberil_val,
                "Cantidad (Kg)": 0
            })
    except Exception as e:
        print(f"Warn - cálculo ICA/Bomberil: {e}")

    # 4. Impuesto Fomento retention for Arroz Paddy only
    if ("arroz paddy" in descripcion) and cuenta == "14051001":
        if "Impuesto Fomento" not in campos or fomento == 0:
            fomento = round(adjusted_subtotal * 0.005, 2)
            campos["Impuesto Fomento"] = str(fomento)
        if fomento > 0:
            asiento.append({
                "cuenta": "246005",
                "nombre": "Cuota Fomento Arrocero",
                "debito": 0,
                "credito": fomento,
                "Cantidad (Kg)": 0
            })
        else:
            print(f"Debug - Fomento is zero or not processed: {fomento}")  # Debug for missing case

          # 5. Cuenta por pagar al proveedor (adjusted for retentions)
    def _to_float(v):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    payable_amount = total_factura

    # Ajuste de fomento (igual que tenías)
    if ("arroz paddy" in descripcion) and cuenta == "14051001" and ("Impuesto Fomento" not in campos or original_fomento == 0):
        payable_amount -= (fomento - original_fomento)

    # Deducir SOLO las retenciones que calcula tu app (no las que ya vienen en la factura)
    if calculated_retefuente > 0:
        payable_amount -= calculated_retefuente

    # --- NUEVO: restar reteICA y bomberil si los calculaste y NO vienen en la factura ---
    reteica_in_doc  = _to_float(campos.get("RetICA Valor") or campos.get("ReteICA Valor") or campos.get("Retención ICA") or 0)
    bomberil_in_doc = _to_float(campos.get("Bomberil Valor") or campos.get("Sobretasa Bomberil") or campos.get("Tasa Bomberil") or 0)

    reteica_calc   = float(locals().get("reteica_val", 0.0) or 0.0)
    bomberil_calc  = float(locals().get("bomberil_val", 0.0) or 0.0)

    if reteica_calc > 0 and reteica_in_doc <= 0:
        payable_amount -= reteica_calc
    if bomberil_calc > 0 and bomberil_in_doc <= 0:
        payable_amount -= bomberil_calc
    # --- fin NUEVO ---

    # >>> Usa tu archivo de pares para decidir la cuenta de CxP <<<
    cxp_cuenta, cxp_nombre_base = seleccionar_cuenta_cxp_por_pares(
        cuenta_debito=cuenta,
        csv_path="Pares_Debito-AP_extra_dos.csv",
    )

    print(
        f"Debug - Total Factura: {total_factura}, Adjusted Subtotal: {adjusted_subtotal}, "
        f"Fomento: {fomento}, Original Fomento: {original_fomento}, "
        f"Retefuente: {retefuente_valor}, Calculated Retefuente: {calculated_retefuente}, "
        f"ReteICA Calc: {reteica_calc}, Bomberil Calc: {bomberil_calc}, "
        f"ReteICA Doc: {reteica_in_doc}, Bomberil Doc: {bomberil_in_doc}, "
        f"Payable Account: {cxp_cuenta} - {cxp_nombre_base}, Payable Amount: {round(payable_amount, 2)}"
    )

    asiento.append({
        "cuenta": cxp_cuenta,
        "nombre": f"{cxp_nombre_base} - {proveedor} - NIT {nit}",
        "debito": 0,
        "credito": round(payable_amount, 2),
        "Cantidad (Kg)": 0
    })


    return asiento

# MAIN
def main():
    archivo_pdf = "factura_page_1.pdf"
    campos = extraer_campos_azure(archivo_pdf)
    descripcion = campos.get("Descripcion", "")
    proveedor = campos.get("Proveedor", "")
    origen_destino = campos.get("Origen-Destino", "")
    cuenta, nombre, retention_category, tipo_transaccion = clasificar_con_gpt(descripcion, proveedor, origen_destino)
    asiento = construir_asiento(campos, cuenta, nombre, retention_category, tipo_transaccion=tipo_transaccion)
    valido, debitos, creditos, diferencia = validar_balance(asiento)
    if not valido:
        print(f"❌ Asiento no cuadra. Débitos: {debitos}, Créditos: {creditos}, Diferencia: {diferencia}")
        return
    if cuentas := validar_cuentas_puc(asiento):
        print(f"❌ Cuentas inválidas:", cuentas)
        return
    nombre_base = os.path.splitext(os.path.basename(archivo_pdf))[0]
    df = pd.DataFrame(asiento)
    df.to_csv(f"asiento_{nombre_base}.csv", index=False)
    with open(f"asiento_{nombre_base}.json", "w", encoding="utf-8") as f:
        json.dump(asiento, f, indent=2, ensure_ascii=False)
    print(f"✅ Exportado: asiento_{nombre_base}.csv y .json")
    print(df)

if __name__ == "__main__":
    main()