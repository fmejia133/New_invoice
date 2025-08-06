from azure.ai.formrecognizer import DocumentAnalysisClient
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

def clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura):
    prompt = f"""
Eres un contador profesional en Colombia. Basado en la siguiente descripción de factura y valores:

- Descripción: \"{descripcion}\"
- Subtotal: {subtotal}
- IVA Valor: {iva_valor}
- Total Factura: {total_factura}

Clasifica la factura en una de estas categorías: inventario, servicio, gasto, material indirecto, pasivo, y selecciona la cuenta más adecuada del Plan Único de Cuentas (PUC) proporcionado, determinando si el monto debe ser un débito o crédito según principios contables colombianos. A continuación, se listan las cuentas relevantes con sus clases (Act=Activo, Pas=Pasivo, Egr=Gasto):
{puc_df.to_string(index=False)}

Reglas estrictas (prioridad máxima):
- Si incluye \"arroz paddy\", \"materia prima\", \"insumo\", \"bulto\" (sin transporte), clasifica como **inventario** y usa una cuenta Activo (e.g., \"14-35-05\") con débito.
- Si incluye \"transporte\", \"flete\", clasifica como **servicio** y usa \"51-35-50\" (Egr) con débito.
- Si incluye \"bodegaje\", \"vigilancia\", \"mantenimiento\", clasifica como **servicio** y usa \"51-35-05\" (Egr) con débito.
- Si es una compra general, clasifica como **gasto** y usa una cuenta Egr adecuada (e.g., \"51-35-05\") con débito.
- Si incluye retenciones, retefuente, o es el total a pagar al proveedor, clasifica como **pasivo** y usa cuentas Pasivo (e.g., \"22-05-05\" para AP, \"23-65-20\" para Retefuente) con crédito.

Devuelve un JSON válido con campos: \"categoria\", \"cuenta\", \"nombre\", \"debito\" (monto o 0), \"credito\" (monto o 0), por ejemplo: {{\"categoria\": \"servicio\", \"cuenta\": \"51-35-50\", \"nombre\": \"TRANSPORTE FLETES Y ACARREOS\", \"debito\": 1000, \"credito\": 0}}.
"""
    response = client_openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    try:
        result = json.loads(response.choices[0].message.content.strip())
        cuenta = result.get("cuenta")
        return {
            "categoria": result.get("categoria", "gasto"),
            "cuenta": cuenta if cuenta in puc_df['CUENTA'].values else "51-35-05",
            "nombre": result.get("nombre", "Gasto General"),
            "debito": to_float(result.get("debito", 0)),
            "credito": to_float(result.get("credito", 0))
        }
    except json.JSONDecodeError:
        return {"categoria": "gasto", "cuenta": "51-35-05", "nombre": "Gasto General", "debito": subtotal, "credito": 0}

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
    with open(ruta_pdf, "rb") as f:
        poller = client.begin_analyze_document(model_id=AZURE_MODEL_ID, document=f)
        result = poller.result()
    campos = {}
    for key, field in result.documents[0].fields.items():
        campos[key] = str(field.value or field.content or "")
    return campos

# Cargar PUC (simplificado, ajusta con el archivo completo)
puc_df = pd.DataFrame([
    {"CUENTA": "11-05-10", "DESCRIPCION": "CAJA GENERAL", "CLASE": "Act"},
    {"CUENTA": "14-35-05", "DESCRIPCION": "INVENTARIO DE MATERIA PRIMA", "CLASE": "Act"},
    {"CUENTA": "22-05-05", "DESCRIPCION": "CUENTAS POR PAGAR", "CLASE": "Pas"},
    {"CUENTA": "23-65-20", "DESCRIPCION": "RETEFUENTE REGISTRADA", "CLASE": "Pas"},
    {"CUENTA": "24-08-05", "DESCRIPCION": "IVA DESCONTABLE COMPRAS GRAVADAS", "CLASE": "Act"},
    {"CUENTA": "23-67-40", "DESCRIPCION": "RETENCION DE IVA", "CLASE": "Pas"},
    {"CUENTA": "23-65-05", "DESCRIPCION": "IMPUESTO FOMENTO RETENCION", "CLASE": "Pas"},
    {"CUENTA": "51-35-05", "DESCRIPCION": "ASEO Y VIGILANCIA", "CLASE": "Egr"},
    {"CUENTA": "51-35-50", "DESCRIPCION": "TRANSPORTE FLETES Y ACARREOS", "CLASE": "Egr"}
])

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
    pl_entry = clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura)
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
    archivo_pdf = "factura_page_1.pdf"
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