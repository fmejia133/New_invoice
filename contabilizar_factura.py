from azure.ai.formrecognizer import DocumentAnalysisClient, DocumentAnalysisError
from azure.core.credentials import AzureKeyCredential
from openai import OpenAI
import os
import json
import pandas as pd
import httpx

# ------------------ CONFIGURACIÓN ------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
AZURE_KEY = os.environ["AZURE_KEY"]
AZURE_ENDPOINT = os.environ["AZURE_ENDPOINT"]
AZURE_MODEL_ID = os.environ["AZURE_MODEL_ID"]

# Debug prints
print("OPENAI_API_KEY:", OPENAI_API_KEY)
print("AZURE_KEY:", AZURE_KEY)
print("AZURE_ENDPOINT:", AZURE_ENDPOINT)
print("AZURE_MODEL_ID:", AZURE_MODEL_ID)
print("Environment vars related to proxies:", {k: v for k, v in os.environ.items() if 'PROXY' in k.upper()})

# Initialize OpenAI client
http_client = httpx.Client()
client_openai = OpenAI(api_key=OPENAI_API_KEY, http_client=http_client)

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

def cargar_puc(ruta="PUC-CENTRO COSTOS SYNERGY.xlsx", sheet_name="PUC"):
    try:
        # Load Excel file and filter for valid CLASE values
        df = pd.read_excel(ruta, sheet_name=sheet_name, usecols=["CUENTA", "DESCRIPCION", "CLASE"])
        df = df.dropna(subset=["CLASE"])  # Remove rows with empty CLASE
        df = df[df["CLASE"].isin(["Act", "Pas", "Egr"])]  # Keep only relevant classes
        print(f"Debug - Loaded PUC with {len(df)} valid accounts")
        return df
    except FileNotFoundError:
        print(f"Debug - PUC file {ruta} not found, using empty fallback")
        return pd.DataFrame(columns=["CUENTA", "DESCRIPCION", "CLASE"])

def clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor):
    descripcion_lower = descripcion.lower() if descripcion else ""
    proveedor_lower = proveedor.lower() if proveedor else ""
    print(f"Debug - Input Description: {descripcion}, Lowercase: {descripcion_lower}, Proveedor: {proveedor}, Lowercase: {proveedor_lower}")

    # Pre-check for transportation supplier
    if any(keyword in proveedor_lower for keyword in ["transportes", "transportadora"]):
        print(f"Debug - Pre-classified as servicio due to supplier: {proveedor_lower}")
        return {
            "categoria": "servicio",
            "cuenta": "51-35-50",
            "nombre": "TRANSPORTE FLETES Y ACARREOS",
            "debito": subtotal,
            "credito": 0
        }

    # Pre-check for arroz paddy
    if any(keyword in descripcion_lower for keyword in ["arroz paddy", "materia prima", "insumo", "bulto"]) and "transporte" not in descripcion_lower:
        print(f"Debug - Pre-classified as inventario due to keywords: {descripcion_lower}")
        return {
            "categoria": "inventario",
            "cuenta": "14-35-05",
            "nombre": "INVENTARIO DE MATERIA PRIMA",
            "debito": subtotal,
            "credito": 0
        }

    # Proceed to AI for general classification
    prompt = f"""
Eres un contador profesional en Colombia que trabajas en una empresa que es un molino de arroz y tiene una fábrica de máquinas empaquetadoras. La principal materia prima del molino es el arroz paddy, que debe ser registrada en una cuenta de inventario. Esta aplicación se enfoca en la función de cuentas por pagar (AP), clasificando facturas en categorías como inventario, servicios, gastos, materiales indirectos o pasivos, y seleccionando la cuenta más adecuada del Plan Único de Cuentas (PUC) proporcionado, según principios contables colombianos. Clasifica cada campo como 'Egr' (P&L), 'Act' (activo) o 'Pas' (pasivo) basándote **exclusivamente** en la descripción y el nombre del proveedor, ya que estos indican la naturaleza del bien o servicio. Los campos subtotal, IVA valor y total factura se proporcionan solo para determinar los montos de débito o crédito, no para la clasificación. Basado en la siguiente información:

- Descripción: \"{descripcion}\"
- Proveedor: \"{proveedor}\"
- Subtotal: {subtotal}
- IVA Valor: {iva_valor}
- Total Factura: {total_factura}

Analiza el contexto de la descripción y el proveedor para determinar la categoría y cuenta más apropiada de la lista de cuentas AP proporcionada a continuación, con sus clases (Act=Activo, Pas=Pasivo, Egr=Gasto):
{puc_df.to_string(index=False)}

Si incluye retenciones (e.g., retefuente, IVA retenido) o es el total a pagar al proveedor, prioriza cuentas de pasivo (Pas). Si es un servicio o gasto, usa cuentas de gasto (Egr). Si es una compra de bienes (e.g., materia prima), usa cuentas de activo (Act) cuando corresponda. Si hay incertidumbre, selecciona la cuenta más lógica basada en la descripción y proveedor, y agrega un comentario en el JSON para revisión.

Devuelve un JSON válido con campos: \"categoria\", \"cuenta\", \"nombre\", \"debito\" (monto o 0), \"credito\" (monto o 0), y opcionalmente \"comentario\" para dudas, por ejemplo: {{\"categoria\": \"servicio\", \"cuenta\": \"51-35-50\", \"nombre\": \"TRANSPORTE FLETES Y ACARREOS\", \"debito\": 1000, \"credito\": 0}} o {{\"categoria\": \"pasivo\", \"cuenta\": \"23-65-20\", \"nombre\": \"RETEFUENTE REGISTRADA\", \"debito\": 0, \"credito\": 150, \"comentario\": \"Revisar retefuente aplicada\"}}.
"""
    response = client_openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    try:
        result = json.loads(response.choices[0].message.content.strip())
        print(f"Debug - AI Response: {result}")
        cuenta = result.get("cuenta")
        # Find the matching account name from puc_df if cuenta exists
        nombre = puc_df[puc_df["CUENTA"] == cuenta]["DESCRIPCION"].iloc[0] if cuenta in puc_df["CUENTA"].values and not puc_df[puc_df["CUENTA"] == cuenta]["DESCRIPCION"].empty else result.get("nombre", "COMPRAS")
        return {
            "categoria": result.get("categoria", "gasto"),
            "cuenta": cuenta if cuenta in puc_df["CUENTA"].values else "51-40-10",  # Default to COMPRAS
            "nombre": nombre,
            "debito": to_float(result.get("debito", 0)),
            "credito": to_float(result.get("credito", 0)),
            "comentario": result.get("comentario", "")
        }
    except json.JSONDecodeError:
        print(f"Debug - JSON Decode Error, falling back to default. Response: {response.choices[0].message.content}")
        return {"categoria": "gasto", "cuenta": "51-40-10", "nombre": "COMPRAS", "debito": subtotal, "credito": 0, "comentario": "Error en parsing, revisar manualmente"}

def validar_balance(asiento):
    total_debitos = sum(to_float(l.get("debito", 0)) for l in asiento)
    total_creditos = sum(to_float(l.get("credito", 0)) for l in asiento)
    diferencia = round(total_debitos - total_creditos, 2)
    return diferencia == 0, total_debitos, total_creditos, diferencia

def validar_cuentas_puc(asiento, puc_df):
    cuentas_validas = set(puc_df["CUENTA"])
    cuentas_asiento = set(str(l["cuenta"]) for l in asiento)
    cuentas_invalidas = cuentas_asiento - cuentas_validas
    return cuentas_invalidas

def extraer_campos_azure(ruta_pdf):
    client = DocumentAnalysisClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY))
    try:
        with open(ruta_pdf, "rb") as f:
            poller = client.begin_analyze_document(model_id=AZURE_MODEL_ID, document=f)
            result = poller.result()
        campos = {}
        for key, field in result.documents[0].fields.items():
            campos[key] = str(field.value or field.content or "")
        print(f"Debug - Extracted fields: {campos}")
        return campos
    except DocumentAnalysisError as e:
        print(f"Debug - Azure Error: {str(e)}")
        return {}
    except Exception as e:
        print(f"Debug - Unexpected Error in extraer_campos_azure: {str(e)}")
        return {}

# Cargar PUC dinámicamente
puc_df = cargar_puc()

# ASIENTO CONTABLE
def construir_asiento(campos, puc_df):
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
    original_fomento = fomento

    print(f"Debug - Campos: {campos}, Fomento: {fomento}")

    # Obtener entrada P&L o inicial
    pl_entry = clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor)
    if pl_entry["debito"] > 0 or pl_entry["credito"] > 0:
        asiento.append(pl_entry)

    if iva_valor > 0:
        asiento.append({
            "cuenta": "24-08-05",
            "nombre": "IVA DESCONTABLE COMPRAS GRAVADAS",
            "debito": iva_valor,
            "credito": 0
        })

    if retefuente_valor > 0:
        asiento.append({
            "cuenta": "23-65-20",
            "nombre": "RETEFUENTE REGISTRADA",
            "debito": 0,
            "credito": retefuente_valor
        })

    if iva_valor > 0 and es_regimen_simple(regimen):
        valor_reteiva = round(iva_valor * 0.15, 2)
        asiento.append({
            "cuenta": "23-67-40",
            "nombre": "RETENCION DE IVA",
            "debito": 0,
            "credito": valor_reteiva
        })

    if ("arroz" in descripcion or "arroz paddy" in descripcion):
        if "Impuesto Fomento" not in campos or fomento == 0:
            fomento = round(subtotal * 0.005, 2)
            campos["Impuesto Fomento"] = str(fomento)
        if fomento > 0:
            asiento.append({
                "cuenta": "23-65-05",
                "nombre": "IMPUESTO FOMENTO RETENCION",
                "debito": 0,
                "credito": fomento
            })

    payable_amount = total_factura - (fomento - original_fomento) if ("arroz" in descripcion or "arroz paddy" in descripcion) and ("Impuesto Fomento" not in campos or original_fomento == 0) else total_factura
    print(f"Debug - Total Factura: {total_factura}, Fomento: {fomento}, Original Fomento: {original_fomento}, Payable Amount: {payable_amount}")
    asiento.append({
        "cuenta": "22-05-05",
        "nombre": f"CUENTAS POR PAGAR - {proveedor} - NIT {nit}",
        "debito": 0,
        "credito": round(payable_amount, 2)
    })

    return asiento

# MAIN
def main():
    archivo_pdf = "factura_page_1.pdf"  # Test with a general invoice
    campos = extraer_campos_azure(archivo_pdf)
    descripcion = campos.get("Descripcion", "")
    asiento = construir_asiento(campos, puc_df)
    valido, debitos, creditos, diferencia = validar_balance(asiento)
    if not valido:
        print(f"❌ Asiento no cuadra. Débitos: {debitos}, Créditos: {creditos}, Diferencia: {diferencia}")
        return
    if cuentas := validar_cuentas_puc(asiento, puc_df):
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