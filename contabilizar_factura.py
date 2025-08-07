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

def cargar_puc(ruta="PUC-CENTRO COSTOS SYNERGY_fixed.xlsx", sheet_name="PUC"):
    try:
        import os
        print(f"Debug - File exists: {os.path.isfile(ruta)}")
        xls = pd.ExcelFile(ruta)
        print(f"Debug - Available sheets: {xls.sheet_names}")
        df_all = pd.read_excel(xls, sheet_name=sheet_name, header=0)
        print(f"Debug - All columns in sheet '{sheet_name}': {df_all.columns.tolist()}")
        # Select only the required columns, ignoring extras
        required_cols = ["CUENTA", "DESCRIPCION", "CLASE"]
        df = df_all[required_cols].copy()
        df = df.dropna(subset=["CLASE"])
        df = df[df["CLASE"].isin(["Act", "Pas", "Egr"])]
        print(f"Debug - Loaded PUC with {len(df)} valid accounts")
        return df
    except FileNotFoundError:
        print(f"Debug - PUC file {ruta} not found, using empty fallback")
        return pd.DataFrame(columns=["CUENTA", "DESCRIPCION", "CLASE"])
    except ValueError as e:
        print(f"Debug - ValueError in cargar_puc: {str(e)}")
        df_all = pd.read_excel(ruta, sheet_name=sheet_name, header=0)
        print(f"Debug - All data columns: {df_all.columns.tolist()}")
        required_cols = ["CUENTA", "DESCRIPCION", "CLASE"]
        df = df_all[required_cols].copy() if all(col in df_all.columns for col in required_cols) else pd.DataFrame(columns=required_cols)
        df = df.dropna(subset=["CLASE"])
        df = df[df["CLASE"].isin(["Act", "Pas", "Egr"])]
        print(f"Debug - Loaded PUC with {len(df)} valid accounts after fallback")
        return df
    except Exception as e:
        print(f"Debug - Unexpected error in cargar_puc: {str(e)}")
        return pd.DataFrame(columns=["CUENTA", "DESCRIPCION", "CLASE"])

def clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor):
    descripcion_lower = descripcion.lower() if descripcion else ""
    proveedor_lower = proveedor.lower() if proveedor else ""
    print(f"Debug - Input Description: {descripcion}, Lowercase: {descripcion_lower}, Proveedor: {proveedor}, Lowercase: {proveedor_lower}")

    if any(keyword in proveedor_lower for keyword in ["transportes", "transportadora"]):
        print(f"Debug - Pre-classified as servicio due to supplier: {proveedor_lower}")
        return {
            "categoria": "servicio",
            "cuenta": "51-35-50",
            "nombre": "TRANSPORTE FLETES Y ACARREOS",
            "debito": subtotal,
            "credito": 0
        }

    if any(keyword in descripcion_lower for keyword in ["arroz paddy", "materia prima", "insumo", "bulto"]) and "transporte" not in descripcion_lower:
        print(f"Debug - Pre-classified as inventario due to keywords: {descripcion_lower}")
        return {
            "categoria": "inventario",
            "cuenta": "14-35-05",
            "nombre": "INVENTARIO DE MATERIA PRIMA",
            "debito": subtotal,
            "credito": 0
        }

    if not descripcion or not proveedor:
        print(f"Debug - Missing critical data: Descripción={descripcion}, Proveedor={proveedor}, using fallback")
        return {
            "categoria": "gasto",
            "cuenta": "51-40-10",
            "nombre": "GASTOS LEGALES",
            "debito": subtotal,
            "credito": 0,
            "comentario": "Datos incompletos desde OCR, revisar manualmente"
        }

    prompt = f"""
Eres un contador profesional en Colombia que trabajas en una empresa que es un molino de arroz y tiene una fábrica de máquinas empaquetadoras. La principal materia prima del molino es el arroz paddy, que debe ser registrada en una cuenta de inventario. Esta aplicación se enfoca en la función de cuentas por pagar (AP), clasificando facturas en categorías como inventario, servicios, gastos, materiales indirectos o pasivos, y seleccionando la cuenta más adecuada del Plan Único de Cuentas (PUC) proporcionado, según principios contables colombianos. Clasifica cada campo como 'Egr' (P&L), 'Act' (activo) o 'Pas' (pasivo) basándote **exclusivamente** en la descripción y el nombre del proveedor. Nota: 'estibas' (pallets) deben clasificarse como inventario (Act); la cuenta '51-40-10' (GASTOS LEGALES) es exclusivamente para legal expenses. Los campos subtotal, IVA valor y total factura se proporcionan solo para determinar los montos de débito o crédito, no para la clasificación. Basado en la siguiente información:

- Descripción: \"{descripcion}\"
- Proveedor: \"{proveedor}\"
- Subtotal: {subtotal}
- IVA Valor: {iva_valor}
- Total Factura: {total_factura}

Analiza el contexto de la descripción y el proveedor para determinar la categoría y cuenta más apropiada de la lista de cuentas AP proporcionada a continuación, con sus clases (Act=Activo, Pas=Pasivo, Egr=Gasto):
{puc_df.to_string(index=False)}

Si incluye retenciones (e.g., retefuente, IVA retenido) o es el total a pagar al proveedor, prioriza cuentas de pasivo (Pas). Si es un servicio o gasto (excepto legal expenses), usa cuentas de gasto (Egr). Si es una compra de bienes (e.g., estibas o materia prima), usa cuentas de activo (Act). Si hay incertidumbre, selecciona la cuenta más lógica basada en la descripción y proveedor, y agrega un comentario en el JSON para revisión.

Devuelve un JSON válido con campos: \"categoria\", \"cuenta\", \"nombre\", \"debito\" (monto o 0), \"credito\" (monto o 0), y opcionalmente \"comentario\" para dudas.
"""
    response = client_openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    try:
        response_text = response.choices[0].message.content.strip()
        print(f"Debug - Full AI Response: {response_text}")
        import re
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(1))
        else:
            raise json.JSONDecodeError("No JSON block found", response_text, 0)
        print(f"Debug - AI Response: {result}")
        cuenta = result.get("cuenta")
        if not puc_df.empty and cuenta not in puc_df["CUENTA"].values:
            print(f"Debug - Account {cuenta} not found in PUC, available accounts: {puc_df['CUENTA'].tolist()}")
            cuenta = next((acc for acc in puc_df["CUENTA"] if puc_df[puc_df["CUENTA"] == acc]["CLASE"].iloc[0] == "Egr"), "51-40-10")
        nombre = puc_df[puc_df["CUENTA"] == cuenta]["DESCRIPCION"].iloc[0] if cuenta in puc_df["CUENTA"].values else "GASTOS LEGALES"
        return {
            "categoria": result.get("categoria", "gasto"),
            "cuenta": cuenta,
            "nombre": nombre,
            "debito": to_float(result.get("debito", 0)),
            "credito": to_float(result.get("credito", 0)),
            "comentario": result.get("comentario", f"Account {cuenta} selected from PUC")
        }
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"Debug - JSON Decode Error: {str(e)}, falling back to default. Response: {response.choices[0].message.content}")
        return {"categoria": "gasto", "cuenta": "51-40-10", "nombre": "GASTOS LEGALES", "debito": subtotal, "credito": 0, "comentario": f"Error en parsing, revisar manualmente. Respuesta AI: {response.choices[0].message.content}"}

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
    total
    total_factura = to_float(campos.get("Total Factura"))
    proveedor = campos.get("Proveedor", "")
    descripcion = campos.get("Descripcion", "")

    # Clasificación principal
    clasificacion = clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura, proveedor)
    asiento.append(clasificacion)

    # IVA Descontable
    if iva_valor > 0 and not es_regimen_simple(campos.get("Regimen Tributario", "")):
        asiento.append({
            "categoria": "impuesto",
            "cuenta": "24-08-05",
            "nombre": "IVA DESCONTABLE",
            "debito": iva_valor,
            "credito": 0
        })

    # Cuentas por pagar
    if total_factura > 0:
        asiento.append({
            "categoria": "pasivo",
            "cuenta": "22-05-05",
            "nombre": "CUENTAS POR PAGAR",
            "debito": 0,
            "credito": total_factura
        })

    # Validaciones
    balance_valido, total_debitos, total_creditos, diferencia = validar_balance(asiento)
    cuentas_invalidas = validar_cuentas_puc(asiento, puc_df)

    if not balance_valido:
        print(f"Debug - Balance no valido: Debitos={total_debitos}, Creditos={total_creditos}, Diferencia={diferencia}")
    if cuentas_invalidas:
        print(f"Debug - Cuentas invalidas en PUC: {cuentas_invalidas}")

    return asiento, balance_valido, cuentas_invalidas

# FUNCIÓN PRINCIPAL (para pruebas locales)
if __name__ == "__main__":
    # Ejemplo de uso local
    campos_ejemplo = {
        "Proveedor": "ESTIBAS RETORNABLES DE COLOMBIA LTDA",
        "Descripcion": "(001001) 00PRN39828 OC 4502812740\n(001001) 00PRN39829 OC 4502812737",
        "Subtotal": "6523800.0",
        "IVA Valor": "1239522.0",
        "Total Factura": "7763322.0",
        "Regimen Tributario": "Responsables de IVA"
    }
    asiento, balance_valido, cuentas_invalidas = construir_asiento(campos_ejemplo, puc_df)
    print(f"Asiento: {asiento}")
    print(f"Balance valido: {balance_valido}")
    print(f"Cuentas invalidas: {cuentas_invalidas}")