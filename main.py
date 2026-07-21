import streamlit as st
import pandas as pd
from datetime import datetime
import re

# -----------------------
# CONFIGURACIÓN DE PÁGINA
# -----------------------
st.set_page_config(
    page_title="Conciliación Diaria — Local",
    page_icon="📄",
    layout="wide"
)

TOLERANCIA_MONTO = 0.01

# -----------------------
# SESSION STATE
# -----------------------
if "resultado_importes" not in st.session_state:
    st.session_state.resultado_importes = None
if "resultado_detalle" not in st.session_state:
    st.session_state.resultado_detalle = None
if "codigo_conciliacion" not in st.session_state:
    st.session_state.codigo_conciliacion = None


# -----------------------
# FUNCIONES DE UTILIDAD
# -----------------------
def generate_session_id():
    """Identificador único de la corrida (solo trazabilidad en pantalla)."""
    import random
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_part = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    return f"{timestamp}_{random_part}"


def parsear_gmoney_qr(archivo):
    """
    Parsea el TXT QR de PayIns (RTPTXN...) de ancho fijo (200 chars/línea).

    Offsets confirmados contra archivo real:
      [0:42]    CCI/referencia (empieza con 'MV') — no se usa para match
      [42:57]   IMPORTE principal en céntimos, alineado a la derecha → /100
      [57:73]   COMISIÓN/fee en céntimos con signo '+' (vacío si no aplica) → /100
      [117:145] ID transacción de 28 dígitos → KEY (== PPY_external_id del CSV)
      [152:163] estado (ej. '490000A0922'); letra A/R

    Retorna (df, fecha_min, fecha_max).
    """
    contenido = archivo.read().decode("latin-1")
    archivo.seek(0)

    filas = []
    for linea in contenido.splitlines():
        if len(linea) < 163:
            continue

        importe_str        = linea[42:57].strip()
        comision_str       = linea[57:73].strip().rstrip("+")
        id_transaccion_cce = linea[117:145].strip()
        estado_bloque      = linea[152:163].strip()

        m = re.search(r"[AR]", estado_bloque)
        estado_ar = m.group() if m else ""

        try:
            monto_gmoney = round(int(importe_str) / 100, 2)
        except (ValueError, TypeError):
            monto_gmoney = None

        if comision_str == "":
            comision_gmoney = 0.00
        else:
            try:
                comision_gmoney = round(int(comision_str) / 100, 2)
            except (ValueError, TypeError):
                comision_gmoney = None

        fecha_gmoney = ""
        hora_completa = ""
        if len(id_transaccion_cce) >= 14:
            y, mth, d = id_transaccion_cce[0:4], id_transaccion_cce[4:6], id_transaccion_cce[6:8]
            hh, mm, ss = id_transaccion_cce[8:10], id_transaccion_cce[10:12], id_transaccion_cce[12:14]
            fecha_gmoney = f"{y}-{mth}-{d}"
            hora_completa = f"{hh}:{mm}:{ss}"

        filas.append({
            "id_transaccion_cce": id_transaccion_cce,
            "estado_ar":          estado_ar,
            "monto_gmoney":       monto_gmoney,
            "comision_gmoney":    comision_gmoney,
            "fecha_gmoney":       fecha_gmoney,
            "hora_completa":      hora_completa,
        })

    df = pd.DataFrame(filas, columns=[
        "id_transaccion_cce", "estado_ar", "monto_gmoney",
        "comision_gmoney", "fecha_gmoney", "hora_completa"
    ])

    fecha_min = fecha_max = None
    if not df.empty:
        dt = pd.to_datetime(df["fecha_gmoney"] + " " + df["hora_completa"], errors="coerce")
        fecha_min, fecha_max = dt.min(), dt.max()

    return df, fecha_min, fecha_max


def mapear_metabase_payin(df_metabase, codigo_conciliacion):
    """Renombra columnas del CSV Metabase PayIns al esquema unificado."""
    MAPA = {
        "Comercio_Nombre":         "comercio_nombre",
        "Deudor_Documento":        "deudor_documento",
        "Deudor_Nombre":           "deudor_nombre",
        "Deuda_public_id":         "deuda_id_interno",
        "currency_code":           "currency_code",
        "amount":                  "amount",
        "Deuda_Estado":            "estado",
        "PC_create_date_GMT_Peru": "fecha_operacion",
        "PPY_external_id":         "id_operacion",
    }
    df = df_metabase.rename(columns=MAPA)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["fecha_operacion"] = pd.to_datetime(df["fecha_operacion"], errors="coerce", dayfirst=True)
    df["codigo_conciliacion"] = codigo_conciliacion

    COLUMNAS = [
        "codigo_conciliacion", "comercio_nombre", "deudor_documento", "deudor_nombre",
        "deuda_id_interno", "currency_code", "amount", "estado",
        "fecha_operacion", "id_operacion",
    ]
    return df[[c for c in COLUMNAS if c in df.columns]]


def conciliar_qr(df_metabase_mapeado, df_gmoney, tolerancia=TOLERANCIA_MONTO):
    """
    Concilia Metabase vs GMoney QR localmente.
    Key: id_operacion (PPY_external_id) == id_transaccion_cce (28 dígitos).

    Categorías de resultado:
      OK                  - casa en ambos, mismo monto, estado A
      Diferencia de monto - casa pero montos distintos, estado A
      Rechazada (R)       - operación rechazada en GMoney (categoría aparte)
      Solo en Metabase    - sin match en GMoney
      Solo en GMoney      - sin match en Metabase
    """
    df_met = df_metabase_mapeado.copy()
    df_met["join_key"] = df_met["id_operacion"].astype(str).str.strip()
    df_met = df_met.drop(columns=["id_operacion"])

    df_gm = df_gmoney.copy()
    df_gm["join_key"] = df_gm["id_transaccion_cce"].astype(str).str.strip()
    df_gm["fecha_gmoney_dt"] = pd.to_datetime(
        df_gm["fecha_gmoney"] + " " + df_gm["hora_completa"], errors="coerce"
    )

    merged = df_met.merge(df_gm, on="join_key", how="outer", suffixes=("_metabase", "_gmoney"))

    def _resultado(row):
        m = row.get("amount")
        g = row.get("monto_gmoney")
        m_na, g_na = pd.isna(m), pd.isna(g)
        if m_na and not g_na:
            return "Solo en GMoney"
        if g_na and not m_na:
            return "Solo en Metabase"
        if row.get("estado_ar") == "R":
            return "Rechazada (R)"
        if abs(m - g) <= tolerancia:
            return "OK"
        return "Diferencia de monto"

    merged["diferencia"] = (merged["amount"].fillna(0) - merged["monto_gmoney"].fillna(0)).round(2)
    merged["resultado"] = merged.apply(_resultado, axis=1)

    # --- Detalle por operación ---
    df_detalle = merged.rename(columns={
        "join_key":     "id_operacion",
        "amount":       "amount_metabase",
        "monto_gmoney": "amount_gmoney",
    })
    columnas_detalle = [
        "id_operacion", "fecha_operacion", "fecha_gmoney_dt",
        "amount_metabase", "amount_gmoney", "comision_gmoney",
        "diferencia", "resultado", "estado_ar",
        "comercio_nombre", "deudor_documento",
    ]
    columnas_detalle = [c for c in columnas_detalle if c in df_detalle.columns]
    df_detalle = df_detalle[columnas_detalle].rename(columns={"fecha_gmoney_dt": "fecha_gmoney"})

    # --- Importes agregados por día (aprobadas vs rechazadas por separado) ---
    fecha_dia_met = pd.to_datetime(merged["fecha_operacion"], errors="coerce").dt.date
    fecha_dia_gm  = merged["fecha_gmoney_dt"].dt.date
    merged["fecha_dia"] = fecha_dia_met.fillna(fecha_dia_gm)

    # Excluye rechazadas del cálculo de diferencia de importes
    aprob = merged[merged["estado_ar"] != "R"]
    df_importes = (
        aprob.groupby("fecha_dia")
        .agg(total_metabase=("amount", "sum"), total_gmoney=("monto_gmoney", "sum"))
        .reset_index()
    )
    df_importes["diferencia"] = (df_importes["total_metabase"] - df_importes["total_gmoney"]).round(2)
    df_importes["estado"] = df_importes["diferencia"].abs().apply(
        lambda x: "Conciliado" if x <= tolerancia else "Diferencias"
    )

    return df_importes, df_detalle


# -----------------------
# TOPBAR
# -----------------------
st.title("📄 Conciliación Diaria — GMoney (Local)")
st.caption("Conciliación 100% local — no depende de Supabase ni de n8n.")
st.divider()

tipo_conciliacion = st.selectbox(
    "Selecciona el tipo de conciliación",
    ["Conciliacion PayOuts - Diaria", "Conciliacion PayIns - Diaria (QR)"]
)

st.header("Subir archivos")
col1, col2 = st.columns(2)
with col1:
    st.subheader("Metabase")
    archivo_metabase = st.file_uploader(
        'Archivo operaciones día anterior',
        type=['xlsx', 'csv'],
        accept_multiple_files=True,
        key='uploader_metabase'
    )
with col2:
    st.subheader("GMoney")
    archivo_gmoney = st.file_uploader(
        "Archivo txt GMoney",
        type=["txt"],
        key='uploader_gmoney'
    )
st.divider()

df_metabase = None
if archivo_metabase:
    dfs = [pd.read_csv(a) for a in archivo_metabase]
    df_metabase = pd.concat(dfs, ignore_index=True)

archivos_listos = df_metabase is not None and archivo_gmoney is not None

if st.button("Conciliar", disabled=not archivos_listos, type="primary", use_container_width=True):
    codigo_conciliacion = generate_session_id()
    try:
        if tipo_conciliacion == "Conciliacion PayIns - Diaria (QR)":
            archivo_gmoney.seek(0)
            df_gmoney, fecha_min, fecha_max = parsear_gmoney_qr(archivo_gmoney)

            if df_gmoney.empty:
                st.error("El archivo GMoney no contiene registros válidos.")
                st.stop()

            df_mapeado_mb = mapear_metabase_payin(df_metabase, codigo_conciliacion)

            with st.spinner("Conciliando localmente..."):
                df_importes, df_detalle = conciliar_qr(df_mapeado_mb, df_gmoney)

            st.session_state.resultado_importes  = df_importes
            st.session_state.resultado_detalle   = df_detalle
            st.session_state.codigo_conciliacion = codigo_conciliacion
            st.success(f"✅ Conciliación completada — código `{codigo_conciliacion}`")
        else:
            st.warning("El flujo PayOuts - Diaria usa el parser anterior. Selecciona PayIns para el flujo QR.")

    except KeyError as e:
        st.error(f"Falta una columna esperada en el archivo: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Error inesperado ({type(e).__name__}): {e}")
        st.stop()

# -----------------------
# RESULTADOS
# -----------------------
if st.session_state.resultado_detalle is not None:
    df_importes = st.session_state.resultado_importes
    df_detalle  = st.session_state.resultado_detalle

    st.divider()
    st.subheader("Conciliación por importes por día")
    st.write("Montos totales agregados por día (excluye operaciones rechazadas), Metabase vs GMoney.")
    if not df_importes.empty and (df_importes["estado"] == "Diferencias").any():
        st.dataframe(df_importes, use_container_width=True)
        st.warning("Se identificaron diferencias por importes en al menos un día.")
    else:
        st.dataframe(df_importes, use_container_width=True)
        st.success("No se encontraron diferencias por importes.")

    st.divider()
    st.subheader("Conciliación por detalle de operaciones")
    st.write("Resultado a nivel de operación individual, Metabase vs GMoney.")

    # Resumen por categoría
    resumen = df_detalle["resultado"].value_counts()
    st.write("Resumen:", {k: int(v) for k, v in resumen.items()})

    diferencias_reales = df_detalle[~df_detalle["resultado"].isin(["OK", "Rechazada (R)"])]
    st.dataframe(df_detalle, use_container_width=True)
    if not diferencias_reales.empty:
        st.warning(f"Se identificaron {len(diferencias_reales)} diferencias (sin contar rechazadas).")
    else:
        st.success("No se encontraron diferencias (fuera de las rechazadas, que son categoría aparte).")
