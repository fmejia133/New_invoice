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

def obtener_tarifa_ica(codigo_ciiu, path="tarifas_ica_ibague.csv"):
    try:
        df_tarifas = pd.read_csv(path, dtype=str)
        row = df_tarifas[df_tarifas["CIIU"] == codigo_ciiu]
        if not row.empty:
            return float(row.iloc[0]["tarifa_por_mil"]) / 1000
    except:
        pass
    return 0.0

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
Usa el nombre del proveedor para refinar, por ejemplo, si el proveedor incluye "Transportadora" o "Transportes", es probable un servicio de fletes, usa una cuenta como 513550 o 613535 o 733550 para transporte, fletes y acarreos, dependiendo si es gasto admin, costo venta o producción.
Reglas:
- La principal materia prima del molino de arroz es el arroz paddy que debe ser registrado en una cuenta de inventario (14xx) y clasificado como 'agricultural_goods' para retención, incluyendo productos relacionados como fungicidas.
- Las facturas de transporte de arroz son de arroz blanco, excepto cuando indiquen que es arroz paddy o cuando el campo 'Descripción' o 'Origen-Destino' indiquen que la carga viene de algún municipio del departamento del Casanare como Yopal. Cuando son de arroz blanco deben ser registradas en la cuenta de costo de ventas 61200707 'SERVICIO TRANSPORTE' y clasificadas como 'transportation' para retención, cuando son de arroz paddy o del Casanare para Ibagué, deben ser registradas en una cuenta de inventario y 'agricultural_goods'.
- Elige la cuenta y categoría exactamente como aparece en la lista, la más apropiada para débito: para inventarios (14xx), gastos operacionales de administración (51xx), etc.
- Categorías de retención: 'agricultural_goods' (1.5%), 'transportation' (1%), 'general_goods' (2.5%), 'services_general' (3.5%), 'services_technical' (6%), 'honorarios' (11%), 'commissions' (10%), 'rental' (3.5%).
Lista de cuentas PUC disponibles de la empresa:
{puc_list}

Devuelve **solo JSON**: {{"cuenta": "codigo_cuenta", "nombre": "nombre_cuenta", "retention_category": "categoria_retencion"}}
Por ejemplo: {{"cuenta": "14051001", "nombre": "MATERIA PRIMA MOLINO", "retention_category": "agricultural_goods"}}
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
    return cuenta, nombre, retention_category

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

def extraer_campos_azure(ruta_pdf):
    client = DocumentAnalysisClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY))
    with open(ruta_pdf, "rb") as f:
        poller = client.begin_analyze_document(model_id=AZURE_MODEL_ID, document=f)
        result = poller.result()
    campos = {}
    for key, field in result.documents[0].fields.items():
        campos[key] = str(field.value or field.content or "")
    return campos
# ASIENTO CONTABLE
def construir_asiento(campos, cuenta, nombre, retention_category=None):
    asiento = []
    subtotal = to_float(campos.get("Subtotal"))
    fletes = to_float(campos.get("Fletes", 0))  # New field for freight charges
    adjusted_subtotal = subtotal + fletes  # Combine for debit and retention calculations
    iva_valor = to_float(campos.get("IVA Valor"))
    total_factura = to_float(campos.get("Total Factura"))
    nit = campos.get("NIT Proveedor", "")
    proveedor = campos.get("Proveedor", "")
    regimen = campos.get("Regimen Tributario", "")
    ciudad = campos.get("Ciudad", "").lower()
    actividad = camsubtotal = to_float(campos.get("Subtotal", 0))
    fletes = to_float(campos.get("Fletes", 0))
    iva_valor = to_float(campos.get("IVA Valor", 0))
    adjusted_subtotal = subtotal + fletes
    regimen = str(campos.get("Regimen Tributario", "") or "").lower()
    retefuente_valor = to_float(campos.get("Retefuente Valor"))
    descripcion = str(campos.get("Descripcion", "") or "").lower()
    origen_destino = str(campos.get("Origen-Destino", "") or "").lower()
    fomento = to_float(campos.get("Impuesto Fomento", 0))
    original_fomento = fomento  # Initialize outside the conditional block

    # Debug campos content
    print(f"Debug - Campos content: {campos}")

    # Parse and sum Cantidad for invoices booked to 14051001 (arroz paddy or Casanare)
    cantidad_total = 0
    if cuenta == "14051001":
        cantidad = campos.get("Cantidad", "")
        try:
            quantities = []
            if isinstance(cantidad, str):
                # Split by newline first, then process each line
                import re
                lines = [line.strip() for line in cantidad.split("\n") if line.strip()]
                for line in lines:
                    # Remove thousands separators and convert to float
                    clean_line = line.replace(",", "")
                    quantities.append(to_float(clean_line))
            elif isinstance(cantidad, (list, tuple)):
                quantities = [to_float(q) for q in cantidad if q]
            else:
                quantities = [to_float(cantidad)] if cantidad else []
            cantidad_total = round(sum(quantities), 2)
            print(f"Debug - Cantidad Parsed: Raw={cantidad}, Lines={lines}, Quantities={quantities}, Total={cantidad_total}")  # Enhanced debug
        except Exception as e:
            print(f"Warning - Failed to parse Cantidad: {cantidad}, Error: {str(e)}. Setting cantidad_total to 0.")

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

    if iva_valor > 0:
        asiento.append({
            "cuenta": "240805",
            "nombre": "IVA descontable compras gravadas",
            "debito": iva_valor,
            "credito": 0,
            "Cantidad (Kg)": 0
        })

    # 2. Retefuente si aplica
    original_retefuente = retefuente_valor
    calculated_retefuente = 0.0  # Initialize to track calculated value
    if retention_category and original_retefuente == 0 and ("no somos autorretenedores" in regimen.lower() or "no somos autoretenedores" in regimen.lower()) and adjusted_subtotal > 1271000:
        # Calculate retefuente only if retention_category is provided and conditions are met
        retention_rates = {
            "agricultural_goods": 0.015,
            "transportation": 0.01,
            "general_goods": 0.025,
            "services_general": 0.035,
            "services_technical": 0.06,
            "honorarios": 0.11,
            "commissions": 0.10,
            "rental": 0.035
        }
        calculated_retefuente = round(adjusted_subtotal * retention_rates.get(retention_category, 0.025), 2)
        print(f"Debug - Applying retefuente rate: {retention_category} -> {retention_rates.get(retention_category, 0.025) * 100}%")
        asiento.append({
            "cuenta": "23657501",
            "nombre": f"OTRAS RETEFTE {int(retention_rates.get(retention_category, 0.025) * 100)}%",
            "debito": 0,
            "credito": calculated_retefuente,
            "Cantidad (Kg)": 0
        })
    elif original_retefuente > 0:
        # Determine retefuente account based on percentage of Subtotal
        retefuente_percentage = retefuente_valor / subtotal if subtotal > 0 else 0
        if abs(retefuente_percentage - 0.015) < 0.001:  # 1.5%
            retefuente_account = "23652001"
            retefuente_name = "Retefuente 1.5%"
        elif abs(retefuente_percentage - 0.025) < 0.001:  # 2.5%
            retefuente_account = "23652002"
            retefuente_name = "Retefuente 2.5%"
        elif abs(retefuente_percentage - 0.035) < 0.001:  # 3.5%
            retefuente_account = "23652003"
            retefuente_name = "Retefuente 3.5%"
        elif abs(retefuente_percentage - 0.01) < 0.001:  # 1% (new)
            retefuente_account = "23652501"
            retefuente_name = "SERVICIOS 1%"
        else:
            retefuente_account = "236520"  # Default account if percentage doesn't match
            retefuente_name = "Retefuente registrada"
        print(f"Debug - Retefuente percentage: {retefuente_percentage * 100}%, Account: {retefuente_account}")
        asiento.append({
            "cuenta": retefuente_account,
            "nombre": retefuente_name,
            "debito": 0,
            "credito": original_retefuente,
            "Cantidad (Kg)": 0
        })

    print(f"Debug - Retefuente: Original={original_retefuente}, Calculated={calculated_retefuente}")  # Debug retefuente values

    # 3. Retención IVA si es régimen simple
    if iva_valor > 0 and es_regimen_simple(regimen):
        valor_reteiva = round(iva_valor * 0.15, 2)
        asiento.append({
            "cuenta": "236740",
            "nombre": "Retención de IVA",
            "debito": 0,
            "credito": valor_reteiva,
            "Cantidad (Kg)": 0
        })

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
    payable_amount = total_factura
    if ("arroz paddy" in descripcion) and cuenta == "14051001" and ("Impuesto Fomento" not in campos or original_fomento == 0):
        payable_amount -= (fomento - original_fomento)
    if calculated_retefuente > 0:  # Deduct only the calculated retefuente, not Azure's
        payable_amount -= calculated_retefuente
    print(f"Debug - Total Factura: {total_factura}, Adjusted Subtotal: {adjusted_subtotal}, Fomento: {fomento}, Original Fomento: {original_fomento}, Retefuente: {retefuente_valor}, Calculated Retefuente: {calculated_retefuente}, Payable Amount: {payable_amount}")  # Debug output
    asiento.append({
        "cuenta": "220505",
        "nombre": f"Cuentas por pagar - {proveedor} - NIT {nit}",
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
    cuenta, nombre, retention_category = clasificar_con_gpt(descripcion, proveedor, origen_destino)
    asiento = construir_asiento(campos, cuenta, nombre, retention_category)
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