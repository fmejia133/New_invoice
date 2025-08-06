import streamlit as st
import os
from contabilizar_factura import extraer_campos_azure, construir_asiento, clasificar_y_obtener_cuenta, validar_balance, validar_cuentas_puc, puc_df
import pandas as pd
import json

# Helper function to convert to float
def to_float(valor):
    try:
        return float(str(valor).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0

st.set_page_config(page_title="App Contable - Facturas", layout="wide")
st.title("📄 Procesamiento Contable de Facturas - Synergy Pack")

uploaded_file = st.file_uploader("Sube una factura PDF", type=["pdf"])

if uploaded_file:
    with open("temp_factura.pdf", "wb") as f:
        f.write(uploaded_file.read())
    try:
        st.success("✅ Factura cargada exitosamente. Procesando...")

        campos = extraer_campos_azure("temp_factura.pdf")
        # Display only selected fields under a subtitle
        st.subheader("📋 Detalles de la Factura")
        st.write(f"**Proveedor:** {campos.get('Proveedor', 'No disponible')}")
        st.write(f"**NIT:** {campos.get('NIT Proveedor', 'No disponible')}")
        st.write(f"**Régimen Tributario:** {campos.get('Regimen Tributario', 'No disponible')}")

        descripcion = campos.get("Descripcion", "")
        subtotal = to_float(campos.get("Subtotal"))
        iva_valor = to_float(campos.get("IVA Valor"))
        total_factura = to_float(campos.get("Total Factura"))

        # Get classification and account details from the new function
        result = clasificar_y_obtener_cuenta(descripcion, puc_df, subtotal, iva_valor, total_factura)
        categoria = result["categoria"]
        initial_entry = {
            "cuenta": result["cuenta"],
            "nombre": result["nombre"],
            "debito": result["debito"],
            "credito": result["credito"]
        }

        # Construct the full asiento using the initial entry
        asiento = construir_asiento(campos, puc_df)
        # Ensure the initial entry from AI is included if not already handled by construir_asiento
        if not any(entry["cuenta"] == initial_entry["cuenta"] for entry in asiento):
            asiento.insert(0, initial_entry)

        valido, debitos, creditos, diferencia = validar_balance(asiento)

        st.subheader("🧾 Asiento contable generado por IA")
        df_asiento = pd.DataFrame(asiento)
        st.dataframe(df_asiento)

        if not valido:
            st.warning(f"⚠️ Asiento original no cuadra. Débitos {debitos} vs Créditos {creditos}. Diferencia: {diferencia}")

        cuentas_invalidas = validar_cuentas_puc(asiento, puc_df)
        if cuentas_invalidas:
            st.warning(f"⚠️ Cuentas no válidas en el PUC: {cuentas_invalidas}")
        else:
            st.info("📘 Todas las cuentas son válidas según el PUC.")

        # Exportación por defecto del asiento generado por IA
        st.download_button("📤 Descargar CSV generado por IA", df_asiento.to_csv(index=False), file_name="asiento_generado.csv")
        json_export_ia = json.dumps(asiento, indent=2, ensure_ascii=False)
        st.download_button("📤 Descargar JSON generado por IA", json_export_ia, file_name="asiento_generado.json")

        st.markdown("---")
        st.subheader("✏️ Editar manualmente el asiento contable")
        edited_df = st.data_editor(df_asiento, num_rows="dynamic", use_container_width=True, key="editable")

        if st.button("✅ Validar asiento editado"):
            valid, d, c, diff = validar_balance(edited_df.to_dict(orient="records"))
            if valid:
                st.success("✅ El asiento editado está balanceado.")
            else:
                st.error(f"❌ Asiento desbalanceado. Débitos: {d} | Créditos: {c} | Diferencia: {diff}")

        st.download_button("📥 Descargar CSV editado", edited_df.to_csv(index=False), file_name="asiento_editado.csv")

        json_export = json.dumps(edited_df.to_dict(orient="records"), indent=2, ensure_ascii=False)
        st.download_button("📥 Descargar JSON editado", json_export, file_name="asiento_editado.json")
    finally:
        os.remove("temp_factura.pdf")