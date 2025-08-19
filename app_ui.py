# app_ui.py
import os
import pandas as pd
import streamlit as st

from contabilizar_factura import (
    extraer_campos_azure,
    construir_asiento,
    clasificar_con_gpt,
    validar_balance,
)

st.set_page_config(page_title="App Contable - Facturas", layout="wide")
st.title("📄 Procesamiento Contable de Facturas - Synergy Pack")

# --- Helpers ---
def _empty_row_like(df: pd.DataFrame) -> dict:
    row = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("debito", "credito", "cantidad (kg)"):
            row[c] = 0
        else:
            row[c] = ""
    return row

def _insert_before_last(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    pos = max(len(df) - 1, 0)  # insert before last row (AP/CxP)
    empty = _empty_row_like(df)
    cols = list(df.columns)
    return pd.concat(
        [df.iloc[:pos], pd.DataFrame([empty])[cols], df.iloc[pos:]],
        ignore_index=True,
    )

# --- Upload & auto-process once per file ---
uploaded_file = st.file_uploader("Sube una factura PDF", type=["pdf"])

# If user clears the file (clicks ✖), wipe session so the UI goes blank
if uploaded_file is None and any(
    k in st.session_state for k in ("df_edit", "df_base", "campos", "processed_file_sig")
):
    for k in ("df_edit", "df_base", "campos", "processed_file_sig"):
        st.session_state.pop(k, None)

if uploaded_file is not None:
    # Build a simple signature so we only process once per uploaded file
    try:
        file_sig = f"{uploaded_file.name}:{len(uploaded_file.getbuffer())}"
    except Exception:
        file_bytes = uploaded_file.read()
        file_sig = f"{uploaded_file.name}:{len(file_bytes)}"
        uploaded_file.seek(0)

    if st.session_state.get("processed_file_sig") != file_sig:
        # Save temp PDF and process
        tmp_pdf = "temp_factura.pdf"
        with open(tmp_pdf, "wb") as f:
            f.write(uploaded_file.read())
        try:
            uploaded_file.seek(0)
        except Exception:
            pass

        try:
            # 👇 Animated brain next to “IA” while processing (pulse: contraction/expansion)
            brain_ui = st.empty()
            brain_ui.markdown(
                """
                <style>
                  .ia-line{display:flex;align-items:center;gap:.4rem;font-weight:600}
                  .ia-brain{
                    display:inline-block;
                    transform-origin:50% 50%;
                    will-change: transform;
                    animation:pulse 1.2s ease-in-out infinite;
                  }
                  @keyframes pulse{
                    0%,100%{ transform: scale(1) }
                    50%    { transform: scale(0.88) }
                  }
                  /* Respect users who prefer reduced motion */
                  @media (prefers-reduced-motion: reduce){
                    .ia-brain{ animation: pulse 2.4s ease-in-out infinite; }
                  }
                </style>
                <div class="ia-line">Procesando factura con ayuda de IA <span class="ia-brain">🧠</span></div>
                """,
                unsafe_allow_html=True,
            )

            with st.spinner("Procesando factura con ayuda de IA..."):
                campos = extraer_campos_azure(tmp_pdf)

                descripcion = campos.get("Descripcion", "")
                proveedor = campos.get("Proveedor", "")
                origen_destino = campos.get("Origen-Destino", "")

                # Compatibilidad: 4 valores (nuevo) o 3 valores (anterior)
                try:
                    cuenta, nombre, retention_category, tipo_transaccion = clasificar_con_gpt(
                        descripcion, proveedor, origen_destino
                    )
                except Exception:
                    cuenta, nombre, retention_category = clasificar_con_gpt(
                        descripcion, proveedor, origen_destino
                    )
                    tipo_transaccion = None

                try:
                    asiento = construir_asiento(
                        campos, cuenta, nombre, retention_category, tipo_transaccion
                    )
                except TypeError:
                    asiento = construir_asiento(campos, cuenta, nombre, retention_category)

                # Build base df and add "Centro de costos" column (empty)
                df_base = pd.DataFrame(asiento)
                if "Centro de costos" not in df_base.columns:
                    df_base["Centro de costos"] = ""

                # Persist editing session & mark processed file
                st.session_state["campos"] = campos
                st.session_state["df_base"] = df_base
                st.session_state["df_edit"] = df_base.copy()
                st.session_state["processed_file_sig"] = file_sig

            st.success("✅ Procesado. Revisa y ajusta el asiento si lo necesitas.")
        finally:
            # remove animated brain and temp file
            try:
                brain_ui.empty()
            except Exception:
                pass
            try:
                os.remove(tmp_pdf)
            except Exception:
                pass
# --- Show results if available ---
if "df_edit" in st.session_state and st.session_state["df_edit"] is not None:
    # Ensure the new column exists even if the file was processed earlier in the session
    for key in ("df_base", "df_edit"):
        df_tmp = st.session_state.get(key)
        if df_tmp is not None and "Centro de costos" not in df_tmp.columns:
            st.session_state[key] = df_tmp.assign(**{"Centro de costos": ""})

    df_base = st.session_state["df_base"]
    df_edit = st.session_state["df_edit"]
    campos = st.session_state.get("campos", {})

    st.subheader("🔎 Campos extraídos")
    st.json(campos, expanded=False)

    st.subheader("🧾 Asiento sugerido (base)")
    st.dataframe(df_base, use_container_width=True)

    st.markdown("---")
    st.subheader("✏️ Editar asiento (simple)")

    # Botón: insertar una fila vacía antes de la última (CxP)
    if st.button("➕ Insertar fila de retención (antes de la última – CxP)"):
        st.session_state["df_edit"] = _insert_before_last(st.session_state["df_edit"])
        df_edit = st.session_state["df_edit"]

    # Editor
    df_edit = st.data_editor(
        df_edit,
        num_rows="dynamic",
        use_container_width=True,
        key="editable_editor",
    )
    st.session_state["df_edit"] = df_edit

    # Validación
    if st.button("✅ Validar asiento editado"):
        valid, d, c, diff = validar_balance(df_edit.to_dict(orient="records"))
        if valid:
            st.success("✅ El asiento editado está balanceado.")
        else:
            st.error(f"❌ Asiento desbalanceado. Débitos: {d} | Créditos: {c} | Diferencia: {diff}")

    # Descarga
    st.download_button(
        "📥 Descargar CSV editado",
        df_edit.to_csv(index=False),
        file_name="asiento_editado.csv",
    )
else:
    st.info("Sube una factura para procesarla automáticamente.")
