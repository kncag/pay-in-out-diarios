import streamlit as st
import pandas as pd
from datetime import datetime
import re

st.set_page_config(page_title="Conciliación Diaria — Local", page_icon="📄", layout="wide")

TOLERANCIA_MONTO = 0.01

# IDs y códigos largos: se leen como texto. No son cantidades y, si pandas los
# interpreta como número, desbordan int64 (rompen Arrow al mostrar la tabla) y
# pueden perder precisión en el match contra el TXT.
COLS_TEXTO = {
    "PPY_external_id": str,
    "Deuda_PspTin": str,
    "Deuda_public_id": str,
    "Deudor_Documento": str,
}

for k in ["resultado_detalle", "resultado_solo_metabase", "resultado_resumen", "codigo_conciliacion"]:
    if k not in st.session_state:
        st.session_state[k] = None


def generate_session_id():
    """Identificador único de la corrida (trazabilidad en pantalla)."""
    import random
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rnd = "".join(str(random.randint(0, 9)) for _ in range(6))
    return f"{ts}_{rnd}"


def parsear_gmoney_qr(archivo):
    """
    Parsea el TXT QR de PayIns (RTPTXN...) de ancho fijo (200 chars/línea).

    Offsets confirmados contra archivo real:
      [42:57]   IMPORTE principal en céntimos, alineado a la derecha → /100
      [57:73]   COMISIÓN en céntimos con signo '+' (vacío si no aplica) → /100
      [117:145] ID transacción de 28 dígitos → KEY (== PPY_external_id del CSV)
      [152:163] estado (ej. '490000A0922'); letra A/R
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

        fecha_gmoney = hora_completa = ""
        if len(id_transaccion_cce) >= 14:
            i = id_transaccion_cce
            fecha_gmoney  = f"{i[0:4]}-{i[4:6]}-{i[6:8]}"
            hora_completa = f"{i[8:10]}:{i[10:12]}:{i[12:14]}"

        filas.append({
            "id_transaccion_cce": id_transaccion_cce,
            "estado_ar":          estado_ar,
            "monto_gmoney":       monto_gmoney,
            "comision_gmoney":    comision_gmoney,
            "fecha_gmoney":       fecha_gmoney,
            "hora_completa":      hora_completa,
        })

    return pd.DataFrame(filas, columns=[
        "id_transaccion_cce", "estado_ar", "monto_gmoney",
        "comision_gmoney", "fecha_gmoney", "hora_completa"
    ])


def mapear_metabase_payin(df_metabase):
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

    columnas = [
        "comercio_nombre", "deudor_documento", "deudor_nombre", "deuda_id_interno",
        "currency_code", "amount", "estado", "fecha_operacion", "id_operacion",
    ]
    return df[[c for c in columnas if c in df.columns]]


def conciliar_qr(df_metabase_mapeado, df_gmoney, tolerancia=TOLERANCIA_MONTO):
    """
    Concilia desde el TXT GMoney como fuente de verdad (left join).
    El CSV de Metabase trae de más (todos los canales); solo interesa
    verificar que cada operación del TXT esté en el CSV.

    Categorías:
      OK                       - TXT aprobada + en CSV, monto principal cuadra
      Diferencia de monto      - TXT aprobada + en CSV, monto principal distinto
      A investigar (falta CSV) - TXT APROBADA pero NO en CSV  ← hallazgo clave
      Rechazada (R)            - TXT estado R (no en CSV, esperado)

    Retorna (df_detalle, df_solo_metabase, resumen).
    """
    df_met = df_metabase_mapeado.copy()
    df_met["join_key"] = df_met["id_operacion"].astype(str).str.strip()

    df_gm = df_gmoney.copy()
    df_gm["join_key"] = df_gm["id_transaccion_cce"].astype(str).str.strip()
    df_gm["fecha_gmoney_dt"] = pd.to_datetime(
        df_gm["fecha_gmoney"] + " " + df_gm["hora_completa"], errors="coerce"
    )

    merged = df_gm.merge(
        df_met.drop(columns=["id_operacion"]),
        on="join_key", how="left", suffixes=("_gmoney", "_metabase")
    )

    def _resultado(row):
        if row["estado_ar"] == "R":
            return "Rechazada (R)"
        if pd.isna(row["amount"]):
            return "A investigar (falta en CSV)"
        if abs(row["amount"] - row["monto_gmoney"]) <= tolerancia:
            return "OK"
        return "Diferencia de monto"

    merged["dif_monto"] = (merged["amount"].fillna(0) - merged["monto_gmoney"].fillna(0)).round(2)
    merged["resultado"] = merged.apply(_resultado, axis=1)

    df_detalle = merged.rename(columns={
        "join_key": "id_operacion",
        "amount":   "monto_metabase",
    })
    columnas = [
        "id_operacion", "estado_ar", "resultado",
        "monto_gmoney", "monto_metabase", "dif_monto", "comision_gmoney",
        "fecha_gmoney_dt", "fecha_operacion", "comercio_nombre", "deudor_documento",
    ]
    df_detalle = df_detalle[[c for c in columnas if c in df_detalle.columns]].rename(columns={
        "fecha_gmoney_dt": "fecha_gmoney",
        "fecha_operacion": "fecha_metabase",
    })

    ids_txt = set(df_gm["join_key"])
    df_solo_metabase = df_met[~df_met["join_key"].isin(ids_txt)].copy()

    resumen = df_detalle["resultado"].value_counts().to_dict()
    resumen["Solo en Metabase (informativo)"] = len(df_solo_metabase)

    return df_detalle, df_solo_metabase, resumen


def generar_csv(df):
    """DataFrame a bytes CSV (sin dependencias externas; utf-8-sig para tildes en Excel)."""
    return df.to_csv(index=False).encode("utf-8-sig")


# -----------------------
# UI
# -----------------------
st.title("📄 Conciliación Diaria — GMoney (Local)")
st.caption("Conciliación 100% local — no depende de Supabase ni de n8n.")
st.divider()

st.header("Subir archivos")
col1, col2 = st.columns(2)
with col1:
    st.subheader("Metabase")
    archivo_metabase = st.file_uploader(
        "Archivo operaciones día anterior", type=["xlsx", "csv"],
        accept_multiple_files=True, key="uploader_metabase"
    )
with col2:
    st.subheader("GMoney")
    archivo_gmoney = st.file_uploader("Archivo txt GMoney", type=["txt"], key="uploader_gmoney")
st.divider()

df_metabase = None
if archivo_metabase:  # lista no vacía
    dfs = [pd.read_csv(a, dtype=COLS_TEXTO, encoding="latin-1") for a in archivo_metabase]
    if dfs:
        df_metabase = pd.concat(dfs, ignore_index=True)

archivos_listos = df_metabase is not None and archivo_gmoney is not None

if st.button("Conciliar", disabled=not archivos_listos, type="primary", width="stretch"):
    codigo = generate_session_id()
    try:
        archivo_gmoney.seek(0)
        df_gmoney = parsear_gmoney_qr(archivo_gmoney)
        if df_gmoney.empty:
            st.error("El archivo GMoney no contiene registros válidos.")
            st.stop()

        with st.spinner("Conciliando localmente..."):
            df_detalle, df_solo_metabase, resumen = conciliar_qr(
                mapear_metabase_payin(df_metabase), df_gmoney
            )

        st.session_state.resultado_detalle       = df_detalle
        st.session_state.resultado_solo_metabase  = df_solo_metabase
        st.session_state.resultado_resumen        = resumen
        st.session_state.codigo_conciliacion      = codigo
        st.success(f"✅ Conciliación completada — código `{codigo}`")

    except KeyError as e:
        st.error(f"Falta una columna esperada en el archivo: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Error inesperado ({type(e).__name__}): {e}")
        st.stop()

if st.session_state.resultado_detalle is not None:
    df_detalle       = st.session_state.resultado_detalle
    df_solo_metabase = st.session_state.resultado_solo_metabase
    resumen          = st.session_state.resultado_resumen
    codigo           = st.session_state.codigo_conciliacion

    st.divider()
    st.subheader("Resumen de conciliación")
    st.write({k: int(v) for k, v in resumen.items()})

    a_investigar = df_detalle[df_detalle["resultado"].isin(
        ["A investigar (falta en CSV)", "Diferencia de monto"]
    )]
    st.divider()
    st.subheader("⚠️ Operaciones a investigar")
    st.write("Operaciones aprobadas del TXT que faltan en el CSV o tienen monto principal distinto.")
    if a_investigar.empty:
        st.success("No hay operaciones a investigar. Todo lo aprobado del TXT está en el CSV y cuadra.")
    else:
        st.warning(f"{len(a_investigar)} operaciones requieren revisión.")
        st.dataframe(a_investigar, width="stretch")
        st.download_button(
            "📥 Descargar 'A investigar' (.csv)", generar_csv(a_investigar),
            file_name=f"a_investigar_{codigo}.csv", mime="text/csv",
        )

    st.divider()
    st.subheader("Detalle completo (operaciones del TXT)")
    st.dataframe(df_detalle, width="stretch")
    st.download_button(
        "📥 Descargar detalle completo (.csv)", generar_csv(df_detalle),
        file_name=f"conciliacion_detalle_{codigo}.csv", mime="text/csv",
    )

    st.divider()
    st.subheader("Informativo — Solo en Metabase (no investigar)")
    st.write(f"{len(df_solo_metabase)} operaciones están en el CSV pero no en el TXT QR. "
             "Es esperado: el CSV incluye otros canales. No requieren acción.")
    with st.expander("Ver operaciones solo en Metabase"):
        st.dataframe(df_solo_metabase, width="stretch")
