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

def clasificar_con_gpt(descripcion, proveedor):
    # Load PUC to include in prompt
    df_puc = pd.read_excel("PUC-CENTRO COSTOS SYNERGY.xlsx", sheet_name="PUC")
    df_puc.columns = df_puc.columns.str.strip()  # Strip any leading/trailing spaces from column names
    # Apply dtype after stripping
    df_puc['CUENTA'] = df_puc['CUENTA'].astype(str)
    # Filter to relevant accounts for efficiency (inventarios, gastos, costos)
    relevant_df = df_puc[df_puc["CUENTA"].str.startswith(('14', '51', '61', '71', '72', '73'), na=False)]
    puc_list = '\n'.join([f"{row['CUENTA']} - {row['DESCRIPCION']}" for _, row in relevant_df.iterrows() if pd.notna(row['DESCRIPCION'])])

    prompt = f"""
Eres un contador profesional en Colombia. Basado únicamente en esta descripción de factura:

\"{descripcion}\"

y el proveedor:

\"{proveedor}\"

Clasifícala y selecciona la cuenta PUC apropiada para el débito principal (el subtotal de la compra) del siguiente listado de cuentas de la empresa.

La descripción es lo más importante porque indica la naturaleza del producto o servicio.
Usa el nombre del proveedor para refinar, por ejemplo, si el proveedor incluye "Transportadora" o "Transportes", es probable un servicio de fletes, usa una cuenta como 513550 o 613535 o 733550 para transporte, fletes y acarreos, dependiendo si es gasto admin, costo venta o producción.

Reglas:
- Si contiene palabras como "remesa", "manifiesto", "tiquete", "conductor", "vehículo", "transporte", "envío", "logística", o proveedor con "Transportadora/Transportes", clasifica como servicio de transporte.
- Si contiene "arroz paddy", "materia prima", "insumo", "bulto", clasifica como inventario.
- Si es bodegaje, vigilancia, mantenimiento, clasifica como servicio.
- Elige la cuenta y nombre exactamente como aparece en la lista, la más apropiada para débito: para inventarios (14xx), gastos admin (51xx), costos de venta (61xx), costos de producción (72xx/73xx), etc.

Lista de cuentas PUC disponibles de la empresa:
{puc_list}

Devuelve **solo JSON** : {{"cuenta": "codigo_cuenta", "nombre": "nombre_cuenta"}}
Por ejemplo: {{"cuenta": "143505", "nombre": "Inventario de materia prima"}}
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
    return data["cuenta"], data["nombre"]

def validar_balance(asiento):
    total_debitos = sum(to_float(l.get("debito", 0)) for l in asiento)
    total_creditos = sum(to_float(l.get("credito", 0)) for l in asiento)
    diferencia = round(total_debitos - total_creditos, 2)
    return diferencia == 0, total_debitos, total_creditos, diferencia

def validar_cuentas_puc(asiento, path_catalogo="PUC-CENTRO COSTOS SYNERGY.xlsx"):
    df_puc = pd.read_excel(path_catalogo, sheet_name="PUC")
    df_puc.columns = df_puc.columns.str.strip()  # Strip any leading/trailing spaces from column names
    df_puc['CUENTA'] = df_puc['CUENTA'].astype(str)
    cuentas_validas = set(df_puc["CUENTA"])
    cuentas_asiento = set(str(l["cuenta"]) for l in asiento)
    cuentas_invalidas = cuentas_asiento - cuentas_validas
    return cuentas_invalidas

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
def construir_asiento(campos, cuenta, nombre):
    asiento = []
    subtotal = to_float(campos.get("Subtotal"))
    iva_valor = to_float(campos.get("IVA Valor"))
    total_factura = to_float(campos.get("Total Factura"))
    nit = campos.get("NIT Proveedor", "")
    proveedor = campos.get("Proveedor", "")
    regimen = campos.get("Regimen Tributario", "")
    ciudad = campos.get("Ciudad", "").lower()
    actividad = campos.get("Actividad Economica", "")
    retefuente_valor = to_float(campos.get("Retefuente Valor"))
    descripcion = campos.get("Descripcion", "").lower()
    fomento = to_float(campos.get("Impuesto Fomento", 0))
    original_fomento = fomento  # Initialize outside the conditional block

    print(f"Debug - Campos: {campos}, Fomento: {fomento}")  # Debug output

    # 1. Subtotal => gasto/inventario
    if subtotal > 0:
        asiento.append({
            "cuenta": cuenta,
            "nombre": nombre,
            "debito": subtotal,
            "credito": 0
        })

    if iva_valor > 0:
        asiento.append({
            "cuenta": "240805",
            "nombre": "IVA descontable compras gravadas",
            "debito": iva_valor,
            "credito": 0
        })

    # 2. Retefuente si aplica
    if retefuente_valor > 0:
        asiento.append({
            "cuenta": "236520",
            "nombre": "Retefuente registrada",
            "debito": 0,
            "credito": retefuente_valor
        })

    # 3. Retención IVA si es régimen simple
    if iva_valor > 0 and es_regimen_simple(regimen):
        valor_reteiva = round(iva_valor * 0.15, 2)
        asiento.append({
            "cuenta": "236740",
            "nombre": "Retención de IVA",
            "debito": 0,
            "credito": valor_reteiva
        })

    # 4. Impuesto Fomento retention for Arroz/Arroz Paddy only
    if ("arroz" in descripcion or "arroz paddy" in descripcion):
        if "Impuesto Fomento" not in campos or fomento == 0:
            fomento = round(subtotal * 0.005, 2)
            campos["Impuesto Fomento"] = str(fomento)  # Update campos for tracking
        if fomento > 0:
            asiento.append({
                "cuenta": "236505",  # Retenciones por pagar
                "nombre": "Impuesto Fomento Retention",
                "debito": 0,
                "credito": fomento
            })
        else:
            print(f"Debug - Fomento is zero or not processed: {fomento}")  # Debug for missing case

    # 5. Cuenta por pagar al proveedor (adjusted for calculated retention only for Arroz)
    payable_amount = total_factura - (fomento - original_fomento) if ("arroz" in descripcion or "arroz paddy" in descripcion) and ("Impuesto Fomento" not in campos or original_fomento == 0) else total_factura
    print(f"Debug - Total Factura: {total_factura}, Fomento: {fomento}, Original Fomento: {original_fomento}, Payable Amount: {payable_amount}")  # Debug output
    asiento.append({
        "cuenta": "220505",
        "nombre": f"Cuentas por pagar - {proveedor} - NIT {nit}",
        "debito": 0,
        "credito": round(payable_amount, 2)
    })

    return asiento

# MAIN
def main():
    archivo_pdf = "factura_page_1.pdf"
    campos = extraer_campos_azure(archivo_pdf)
    descripcion = campos.get("Descripcion", "")
    proveedor = campos.get("Proveedor", "")
    cuenta, nombre = clasificar_con_gpt(descripcion, proveedor)
    asiento = construir_asiento(campos, cuenta, nombre)
    valido, debitos, creditos, diferencia = validar_balance(asiento)
    if not valido:
        print(f"❌ Asiento no cuadra. Débitos: {debitos}, Créditos: {creditos}, Diferencia: {diferencia}")
        return
    if cuentas := validar_cuentas_puc(asiento):
        print("❌ Cuentas inválidas:", cuentas)
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