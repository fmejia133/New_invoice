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

    # Check for missing critical data
    if not descripcion or not proveedor:
        print(f"Debug - Missing critical data: Descripción={descripcion}, Proveedor={proveedor}, using fallback")
        return {
            "categoria": "gasto",
            "cuenta": "51-40-10",
            "nombre": "COMPRAS",
            "debito": subtotal,
            "credito": 0,
            "comentario": "Datos incompletos desde OCR, revisar manualmente"
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
        nombre = puc_df[puc_df["CUENTA"] == cuenta]["DESCRIPCION"].iloc[0] if cuenta in puc_df["CUENTA"].values and not puc_df[puc_df["CUENTA"] == cuenta]["DESCRIPCION"].empty else result.get("nombre", "COMPRAS")
        return {
            "categoria": result.get("categoria", "gasto"),
            "cuenta": cuenta if cuenta in puc_df["CUENTA"].values else "51-40-10",
            "nombre": nombre,
            "debito": to_float(result.get("debito", 0)),
            "credito": to_float(result.get("credito", 0)),
            "comentario": result.get("comentario", "")
        }
    except json.JSONDecodeError:
        print(f"Debug - JSON Decode Error, falling back to default. Response: {response.choices[0].message.content}")
        return {"categoria": "gasto", "cuenta": "51-40-10", "nombre": "COMPRAS", "debito": subtotal, "credito": 0, "comentario": f"Error en parsing, revisar manualmente. Respuesta AI: {response.choices[0].message.content}"}