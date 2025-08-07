from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from openai import OpenAI
import os
import pandas as pd

# ------------------ CONFIGURACIÓN ------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
AZURE_KEY = os.environ["AZURE_KEY"]
AZURE_ENDPOINT = os.environ["AZURE_ENDPOINT"]
AZURE_MODEL_ID = os.environ["AZURE_MODEL_ID"]

# Initialize clients
client_openai = OpenAI(api_key=OPENAI_API_KEY)
client_azure = DocumentAnalysisClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY))

# FUNCIONES AUXILIARES
def to_float(valor):
    try:
        return float(str(valor).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0

def es_regimen_simple(texto):
    texto = texto.lower()
    return "régimen simple" in texto or "regimen simple" in texto or "simple" in texto

def cargar_puc(ruta="PUC-CENTRO COSTOS SYNERGY_fixed.xlsx", sheet_name="PUC"):
    try:
        xls = pd.ExcelFile(ruta)
        df_all = pd.read_excel(xls, sheet_name=sheet_name, header=0)
        required_cols = ["CUENTA", "DESCRIPCION", "CLASE"]
        df = df_all[required_cols].copy()
        df = df.dropna(subset=["CLASE"])
        df = df[df["CLASE"].isin(["Act", "Pas", "Egr"])]
        print(f"Loaded PUC with {len(df)} accounts")
        return df
    except Exception as e:
        print(f"Error loading PUC: {str(e)}")
        return pd.DataFrame(columns=["CUENTA", "DESCRIPCION", "CLASE"])

def clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor):
    descripcion_lower = descripcion.lower() if descripcion else ""
    proveedor_lower = proveedor.lower() if proveedor else ""

    if any(keyword in proveedor_lower for keyword in ["transportes", "transportadora"]):
        return {"categoria": "servicio", "cuenta": "51-35-50", "nombre": "TRANSPORTE FLETES Y ACARREOS", "debito": subtotal, "credito": 0}

    if any(keyword in descripcion_lower for keyword in ["arroz paddy", "materia prima", "insumo", "bulto"]) and "transporte" not in descripcion_lower:
        return {"categoria": "inventario", "cuenta": "14-35-05", "nombre": "INVENTARIO DE MATERIA PRIMA", "debito": subtotal, "credito": 0}

    if not descripcion or not proveedor:
        return {"categoria": "gasto", "cuenta": "51-40-10", "nombre": "GASTOS LEGALES", "debito": subtotal, "credito": 0, "comentario": "Datos incompletos"}

    prompt = f"""
Eres un contador profesional en Colombia. Clasifica esta factura usando el PUC: {puc_df.to_string(index=False)}. Descripción: \"{descripcion}\", Proveedor: \"{proveedor}\", Subtotal: {subtotal}, IVA: {iva_valor}, Total: {total_factura}. Devuelve un JSON con \"categoria\", \"cuenta\", \"nombre\", \"debito\", \"credito\".
"""
    response = client_openai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], temperature=0)
    result = response.choices[0].message.content.strip()
    import re
    json_match = re.search(r'\{.*\}', result)
    if json_match:
        import json
        return json.loads(json_match.group(0))
    return {"categoria": "gasto", "cuenta": "51-40-10", "nombre": "GASTOS LEGALES", "debito": subtotal, "credito": 0}

def extraer_campos_azure(ruta_pdf):
    try:
        with open(ruta_pdf, "rb") as f:
            poller = client_azure.begin_analyze_document(model_id=AZURE_MODEL_ID, document=f)
            result = poller.result()
        campos = {}
        for key, field in result.documents[0].fields.items():
            campos[key] = str(field.value or field.content or "")
        print(f"Extracted fields: {campos}")
        return campos
    except Exception as e:
        print(f"Error extracting fields: {str(e)}")
        return {}

def construir_asiento(campos, puc_df):
    asiento = []
    subtotal = to_float(campos.get("Subtotal"))
    iva_valor = to_float(campos.get("IVA Valor"))
    total_factura = to_float(campos.get("Total Factura"))
    proveedor = campos.get("Proveedor", "")
    descripcion = campos.get("Descripcion", "")

    clasificacion = clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor)
    asiento.append(clasificacion)

    if iva_valor > 0 and not es_regimen_simple(campos.get("Regimen Tributario", "")):
        asiento.append({"categoria": "impuesto", "cuenta": "24-08-05", "nombre": "IVA DESCONTABLE", "debito": iva_valor, "credito": 0})

    if total_factura > 0:
        asiento.append({"categoria": "pasivo", "cuenta": "22-05-05", "nombre": "CUENTAS POR PAGAR", "debito": 0, "credito": total_factura})

    return asiento

# Cargar PUC
puc_df = cargar_puc()

# Ejemplo de uso local
if __name__ == "__main__":
    campos_ejemplo = {
        "Proveedor": "Arroz Ltda",
        "Descripcion": "Arroz Paddy 1000kg",
        "Subtotal": "5000000",
        "IVA Valor": "950000",
        "Total Factura": "5950000",
        "Regimen Tributario": "Responsables de IVA"
    }
    asiento = construir_asiento(campos_ejemplo, puc_df)
    print(f"Asiento: {asiento}")