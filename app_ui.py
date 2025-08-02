import streamlit as st
import os
from contabilizar_factura import extraer_campos_azure, construir_asiento, clasificar_con_gpt, validar_balance, validar_cuentas_puc
import pandas as pd
import json

st.set_page_config(page_title="App Contable - Facturas", layout="wide")
st.title("📄 Procesamiento Contable de Facturas - Synergy Pack")

uploaded_file = st.file_uploader("Sube una factura PDF", type=["pdf"])

if uploaded_file:
    with open("temp_factura.pdf", "wb") as f:
        f.write(uploaded_file.read())
    try:
        st.success("✅ Factura cargada exitosamente. Procesando...")

        # Placeholder for selected fields (modify as needed)
        campos_seleccionados = {
            "Proveedor": "Proveedor Ejemplo",
            "NIT": "123456789-0",
            "Regimen Tributario": "Común"
        }
        st.subheader("📋 Detalles de la Factura")
        st.write(f"**Proveedor:** {campos_seleccionados['Proveedor']}")
        st.write(f"**NIT:** {campos_seleccionados['NIT']}")
        st.write(f"**Régimen Tributario:** {campos_seleccionados['Regimen Tributario']}")

        # Proceed with classification and accounting logic
        clasificacion = clasificar_con_gpt("")  # Placeholder, adjust if needed

        asiento = construir_asiento(campos_seleccionados, clasificacion)
        valido, debitos, creditos, diferencia = validar_balance(asiento)

        st.subheader("🧾 Asiento contable generado por IA")
        df_asiento = pd.DataFrame(asiento)
        st.dataframe(df_asiento)

        if not valido:
            st.warning(f"⚠️ Asiento original no cuadra. Débitos {debitos} vs Créditos {creditos}. Diferencia: {diferencia}")

        cuentas_invalidas = validar_cuentas_puc(asiento)
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