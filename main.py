import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
from io import BytesIO
import random
import json
import re

st.set_page_config(page_title="Conciliación Diaria — Local", layout="centered")

TOLERANCIA_MONTO = 0.01

COLS_TEXTO = {
    "PPY_external_id": str,
    "Deuda_PspTin": str,
    "Deuda_public_id": str,
    "Deudor_Documento": str,
}

MAPA_METABASE = {
    "Comercio_Nombre":         "comercio_nombre",
    "Deudor_Documento":        "deudor_documento",
    "Deudor_Nombre":           "deudor_nombre",
    "amount":                  "amount",
    "Deuda_Estado":            "estado",
    "PC_create_date_GMT_Peru": "fecha_operacion",
    "PPY_external_id":         "id_operacion",
}
COLUMNAS_SALIDA = ["id_operacion", "comercio_nombre", "deudor_documento",
                   "deudor_nombre", "amount", "estado", "fecha_operacion"]

CLAVES_ANOMALAS = {"clave", "code", "reason"}

_DEFAULTS = {
    "resultado_detalle": None,
    "resultado_solo_metabase": None,
    "resultado_resumen": None,
    "codigo_conciliacion": None,
    "json_anomalos": None,
    "ver_detalle": False,
    "ver_solo_metabase": False,
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


def generate_session_id():
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rnd = "".join(str(random.randint(0, 9)) for _ in range(6))
    return f"{ts}_{rnd}"


def _leer_archivo(archivo):
    """Lee un CSV o Excel de Metabase a DataFrame crudo (una sola lectura)."""
    archivo.seek(0)
    if archivo.name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(archivo, dtype=COLS_TEXTO)
    else:
        df = pd.read_csv(archivo, dtype=COLS_TEXTO, encoding="latin-1")
    archivo.seek(0)
    return df


def parsear_gmoney_qr(archivo):
    """Parsea un TXT QR de PayIns (RTPTXN...) de ancho fijo (200 chars/línea)."""
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


def parsear_gmoney_multiple(archivos):
    """Parsea uno o varios TXT GMoney y los combina en un solo DataFrame."""
    dfs = [parsear_gmoney_qr(a) for a in archivos]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


def _normalizar_metabase(df):
    """Renombra y tipa columnas al esquema unificado. Falla claro si faltan las críticas."""
    df = df.rename(columns=MAPA_METABASE)
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    if "fecha_operacion" in df.columns:
        df["fecha_operacion"] = pd.to_datetime(df["fecha_operacion"], errors="coerce", dayfirst=True)
    if "id_operacion" in df.columns:
        df["id_operacion"] = df["id_operacion"].astype(str).str.strip()

    faltantes = [c for c in ["id_operacion", "amount"] if c not in df.columns]
    if faltantes:
        st.error(f"Al archivo le faltan columnas críticas: {faltantes}. "
                 f"Columnas encontradas: {list(df.columns)}")
        st.stop()
    return df[[c for c in COLUMNAS_SALIDA if c in df.columns]]


def procesar_metabase(archivos):
    """
    Lee cada archivo UNA sola vez y devuelve:
      - df_metabase: normalizado para conciliar
      - df_anomalos: filas con metadata JSON anómala (clave/code/reason)
    """
    normalizados, anomalos = [], []
    for a in archivos:
        crudo = _leer_archivo(a)
        normalizados.append(_normalizar_metabase(crudo))
        anomalos.append(_detectar_anomalos(crudo))

    df_metabase = pd.concat(normalizados, ignore_index=True) if normalizados else None
    df_anomalos = (pd.concat(anomalos, ignore_index=True) if anomalos
                   else pd.DataFrame(columns=["id_operacion", "claves_encontradas", "contenido_json"]))
    return df_metabase, df_anomalos


def _detectar_anomalos(df_crudo):
    """
    Detecta filas cuyo PC_OP_metadata contiene claves anómalas.
    Vectorizado: filtra por texto antes de parsear JSON (solo parsea candidatas).
    """
    cols_vacias = ["id_operacion", "claves_encontradas", "contenido_json"]
    if "PC_OP_metadata" not in df_crudo.columns:
        return pd.DataFrame(columns=cols_vacias)

    col = df_crudo["PC_OP_metadata"].astype(str)
    patron = "|".join(CLAVES_ANOMALAS)
    candidatas = df_crudo[col.str.contains(patron, na=False, regex=True)]
    if candidatas.empty:
        return pd.DataFrame(columns=cols_vacias)

    filas = []
    for _, row in candidatas.iterrows():
        celda = str(row.get("PC_OP_metadata", "")).strip()
        if not celda.startswith("{"):
            continue
        try:
            d = json.loads(celda)
        except json.JSONDecodeError:
            continue
        presentes = CLAVES_ANOMALAS & set(d.keys())
        if presentes:
            filas.append({
                "id_operacion":       str(row.get("PPY_external_id", "")).strip(),
                "claves_encontradas": ", ".join(sorted(presentes)),
                "contenido_json":     celda,
            })
    return pd.DataFrame(filas, columns=cols_vacias)


def conciliar_qr(df_metabase, df_gmoney, tolerancia=TOLERANCIA_MONTO):
    """Concilia desde el TXT GMoney como fuente de verdad (left join)."""
    df_met = df_metabase.copy()
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

    # Clasificación vectorizada (equivalente a la función _resultado fila por fila)
    cond = [
        merged["estado_ar"] == "R",
        merged["amount"].isna(),
        (merged["amount"] - merged["monto_gmoney"]).abs() <= tolerancia,
    ]
    opciones = ["Rechazada (R)", "A investigar (falta en Metabase)", "OK"]
    merged["resultado"] = np.select(cond, opciones, default="Diferencia de monto")

    merged["dif_monto"] = (merged["amount"].fillna(0) - merged["monto_gmoney"].fillna(0)).round(2)
    merged["tiene_comision"] = merged["comision_gmoney"].fillna(0) > 0

    df_detalle = merged.rename(columns={"join_key": "id_operacion", "amount": "monto_metabase"})
    columnas = [
        "id_operacion", "estado_ar", "resultado", "tiene_comision",
        "monto_gmoney", "monto_metabase", "dif_monto", "comision_gmoney",
        "fecha_gmoney_dt", "fecha_operacion", "comercio_nombre", "deudor_documento",
    ]
    df_detalle = df_detalle[[c for c in columnas if c in df_detalle.columns]].rename(columns={
        "fecha_gmoney_dt": "fecha_gmoney",
        "fecha_operacion": "fecha_metabase",
    })

    ids_txt = set(df_gm["join_key"])
    df_solo_metabase = df_met[~df_met["join_key"].isin(ids_txt)].copy()

    por_categoria = df_detalle["resultado"].value_counts().to_dict()
    suma_cat = int(sum(por_categoria.values()))
    total_txt = len(df_gm)

    resumen = {
        "entradas": {
            "Líneas en TXT (GMoney)":          total_txt,
            "  · Aprobadas (A)":               int((df_gm["estado_ar"] == "A").sum()),
            "  · Rechazadas (R)":              int((df_gm["estado_ar"] == "R").sum()),
            "Líneas en Metabase (CSV/Excel)":  len(df_met),
        },
        "categorias": {k: int(v) for k, v in por_categoria.items()},
        "solo_metabase": len(df_solo_metabase),
        "cuadre_txt": {"suma": suma_cat, "total": total_txt, "cuadra": suma_cat == total_txt},
    }
    return df_detalle, df_solo_metabase, resumen


def generar_descarga(df):
    """Genera bytes de descarga en Excel; si openpyxl falla, cae a CSV. -> (bytes, ext, mime)."""
    try:
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Datos")
        buffer.seek(0)
        return (buffer.getvalue(), "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception:
        return (df.to_csv(index=False).encode("utf-8-sig"), "csv", "text/csv")


def boton_descarga(df, etiqueta, nombre_base, codigo):
    """Botón de descarga con formato Excel/CSV automático."""
    datos, ext, mime = generar_descarga(df)
    st.download_button(f"{etiqueta} ({ext.upper()})", datos,
                       file_name=f"{nombre_base}_{codigo}.{ext}", mime=mime)


def mostrar_seccion_tabla(df, descripcion, msg_vacio, etiqueta_descarga, nombre_archivo, codigo):
    """Renderiza una sección estándar: descripción + tabla + descarga, o mensaje si está vacía."""
    st.write(descripcion)
    if df is None or df.empty:
        st.info(msg_vacio)
        return
    st.write(f"Total: {len(df)} registros.")
    st.dataframe(df)
    boton_descarga(df, etiqueta_descarga, nombre_archivo, codigo)


# -----------------------
# INTERFAZ
# -----------------------
st.title("Conciliación Diaria — GMoney")
st.caption("Conciliación local de operaciones QR. Acepta el CSV o el Excel de deudas pagadas.")
st.divider()

st.subheader("Carga de archivos")
archivo_metabase = st.file_uploader(
    "Metabase — CSV o Excel de deudas pagadas", type=["xlsx", "xls", "csv"],
    accept_multiple_files=True, key="uploader_metabase"
)
archivo_gmoney = st.file_uploader(
    "GMoney — archivo(s) TXT", type=["txt"],
    accept_multiple_files=True, key="uploader_gmoney"
)
st.divider()

archivos_listos = bool(archivo_metabase) and bool(archivo_gmoney)

if st.button("Conciliar", disabled=not archivos_listos, type="primary"):
    codigo = generate_session_id()
    try:
        df_gmoney = parsear_gmoney_multiple(archivo_gmoney)
        if df_gmoney.empty:
            st.error("El/los archivo(s) GMoney no contienen registros válidos.")
            st.stop()

        with st.spinner("Procesando conciliación..."):
            df_metabase, json_anom = procesar_metabase(archivo_metabase)
            df_detalle, df_solo_metabase, resumen = conciliar_qr(df_metabase, df_gmoney)

        st.session_state.resultado_detalle       = df_detalle
        st.session_state.resultado_solo_metabase  = df_solo_metabase
        st.session_state.resultado_resumen        = resumen
        st.session_state.json_anomalos            = json_anom
        st.session_state.codigo_conciliacion      = codigo
        st.session_state.ver_detalle              = False
        st.session_state.ver_solo_metabase        = False

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
    json_anom        = st.session_state.json_anomalos
    codigo           = st.session_state.codigo_conciliacion

    st.divider()

    st.subheader("Totales de entrada")
    st.table(pd.DataFrame([{"Concepto": k, "Cantidad": v} for k, v in resumen["entradas"].items()]))

    st.subheader("Desglose por categoría")
    st.table(pd.DataFrame([{"Categoría": k, "Operaciones": v} for k, v in resumen["categorias"].items()]))
    st.caption(f"Solo en Metabase (informativo, no se analiza): {resumen['solo_metabase']} operaciones.")

    st.subheader("Verificación de cuadre")
    cuadre = resumen["cuadre_txt"]
    st.table(pd.DataFrame([
        {"Concepto": "Suma de categorías", "Valor": cuadre["suma"]},
        {"Concepto": "Total líneas TXT",   "Valor": cuadre["total"]},
        {"Concepto": "Estado",             "Valor": "Cuadra" if cuadre["cuadra"] else "No cuadra"},
    ]))
    if not cuadre["cuadra"]:
        st.error("La suma de categorías no coincide con el total de líneas del TXT. Requiere revisión.")

    st.subheader("Metadata con estructura anómala")
    mostrar_seccion_tabla(
        json_anom,
        "Operaciones cuyo campo PC_OP_metadata contiene las claves 'clave', 'code' o 'reason', "
        "en lugar de la estructura estándar de una operación QR.",
        "No se detectaron registros con metadata anómala.",
        "Descargar metadata anómala", "metadata_anomala", codigo,
    )

    st.subheader("Operaciones a investigar")

    no_ok = df_detalle[df_detalle["resultado"].isin(
        ["A investigar (falta en Metabase)", "Diferencia de monto"])]
    st.markdown("**Operaciones con incidencia (resultado distinto de OK)**")
    mostrar_seccion_tabla(
        no_ok,
        "Operaciones aprobadas que faltan en Metabase o tienen monto principal distinto.",
        "No se identificaron operaciones con incidencia.",
        "Descargar operaciones con incidencia", "incidencias", codigo,
    )

    ok_comision = df_detalle[(df_detalle["resultado"] == "OK") & (df_detalle["tiene_comision"])]
    st.markdown("**Operaciones OK con comisión**")
    mostrar_seccion_tabla(
        ok_comision,
        "Operaciones conciliadas correctamente pero con comisión mayor a cero.",
        "No hay operaciones OK con comisión.",
        "Descargar OK con comisión", "ok_con_comision", codigo,
    )

    st.subheader("Detalle completo de operaciones del TXT")
    if st.button("Generar detalle completo"):
        st.session_state.ver_detalle = True
    if st.session_state.ver_detalle:
        st.dataframe(df_detalle)
        boton_descarga(df_detalle, "Descargar detalle completo", "conciliacion_detalle", codigo)

    st.subheader("Operaciones solo en Metabase")
    st.write(f"{len(df_solo_metabase)} operaciones registradas en Metabase que no figuran en el TXT. "
             "Se listan con fines informativos y no forman parte del análisis.")
    if st.button("Generar listado"):
        st.session_state.ver_solo_metabase = True
    if st.session_state.ver_solo_metabase:
        st.dataframe(df_solo_metabase)
        boton_descarga(df_solo_metabase, "Descargar solo en Metabase", "solo_metabase", codigo)
