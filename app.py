# Para correr la aplicacion, escribir en el terminal: streamlit run (nombre del archivo).py
# nombre: app

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

st.set_page_config(layout="wide", page_title="Analizador de Cuadro de Carga - LDC")

# ---------------- FUNCIONES ---------------- #

def read_file(uploaded_file):
    """
    Lee el archivo sin asumir header (para poder detectar filas de metadata).
    Devuelve DataFrame bruto (sin header automático).
    """
    try:
        if uploaded_file.name.lower().endswith(('.xls', '.xlsx')):
            df = pd.read_excel(uploaded_file, header=None, dtype=object)
        else:
            df = pd.read_csv(uploaded_file, header=None, dtype=object)
    except Exception as e:
        st.error(f"Error leyendo el archivo: {e}")
        return None
    return df

def parse_load_schedule(df_raw):
    """
    Detecta la fila de encabezado (contiene 'Potencia' o 'Item' o 'Horas'),
    reasigna encabezados y devuelve:
      - df_hourly: DataFrame con columnas ['timestamp','consumption_kW']
      - df_equipment_clean: tabla de equipos ya limpia con columnas horas 0..23 como 0/1
    """
    df_raw = df_raw.dropna(how='all').reset_index(drop=True)
    df0 = df_raw.copy()
    # Buscar la fila que contenga palabra clave (potencia, item, carga, horas)
    header_row_idx = None
    keywords = ('potencia', 'item', 'carga', 'horas', 'horas de uso', 'hora')
    for i, row in df0.iterrows():
        row_text = ' '.join([str(x).lower() for x in row.values if pd.notna(x)])
        if any(k in row_text for k in keywords):
            header_row_idx = i
            break

    # fallback: buscar fila que contenga varios tokens 0..23 (fila horas)
    if header_row_idx is None:
        for i, row in df0.iterrows():
            tokens = [str(x).strip() for x in row.values if pd.notna(x)]
            digit_count = sum(1 for t in tokens if t.isdigit() and 0 <= int(t) <= 23)
            if digit_count >= 3:
                header_row_idx = i
                break

    if header_row_idx is None:
        st.error("No se pudo determinar la fila de encabezado. Asegúrate que la tabla contiene 'Potencia' y las horas 0..23 en alguna fila.")
        return None, None

    # Construir encabezado desde esa fila
    raw_header = df0.iloc[header_row_idx].fillna('').astype(str).str.strip().tolist()
    df_eq = df0.iloc[header_row_idx+1:].copy().reset_index(drop=True)
    df_eq.columns = raw_header

    # Si hay columnas vacías como '', renombrarlas para evitar colisiones
    df_eq.columns = [c if c != '' else f"unnamed_{i}" for i, c in enumerate(df_eq.columns)]

    # Encontrar columna potencia (insensible a mayúsculas/minúsculas/espacios)
    potencia_col = None
    for c in df_eq.columns:
        s = str(c).lower().replace(" ", "")
        if "potencia" in s or "(w)" in s or "potencia(w)" in s or "potenciaw" in s:
            potencia_col = c
            break
    if potencia_col is None:
        # último intento: buscar columna cuyo contenido (en la primera filas) parezca numérico grande
        for c in df_eq.columns:
            try:
                sample_vals = pd.to_numeric(df_eq[c].dropna().astype(str).str.replace(',', '.'), errors='coerce')
                if not sample_vals.empty and sample_vals.mean() > 1:  # potencia en W típica
                    potencia_col = c
                    break
            except:
                pass

    if potencia_col is None:
        st.error("No se encontró columna de potencia (probé 'Potencia', 'Potencia (W)', etc.).")
        return None, None

    # Detectar columnas horarias: preferir headers que son dígitos 0..23
    hour_col_names = []
    for c in df_eq.columns:
        cs = str(c).strip()
        if cs.isdigit():
            v = int(cs)
            if 0 <= v <= 23:
                hour_col_names.append(c)

    # Si no encontró horas por header, buscar en la fila raw_header (porque las horas pueden estar ahí)
    if not hour_col_names:
        for j, cell in enumerate(raw_header):
            cell_s = str(cell).strip()
            if cell_s.isdigit() and 0 <= int(cell_s) <= 23 and j < len(df_eq.columns):
                hour_col_names.append(df_eq.columns[j])

    # último fallback: detectar columnas que tienen mayor proporción de celdas no vacías en 24 columnas contiguas
    if not hour_col_names:
        # buscar bloque de 24 columnas con mayor densidad de no-nulos
        col_list = list(df_eq.columns)
        best_block = None
        best_score = 0
        for start in range(0, max(1, len(col_list)-23)):
            block = col_list[start:start+24]
            score = df_eq[block].notna().sum(axis=0).sum()
            if score > best_score:
                best_score = score
                best_block = block
        if best_block and len(best_block) >= 24:
            hour_col_names = best_block[:24]

    if not hour_col_names:
        st.error("No se encontraron columnas horarias (0..23) tras intentar múltiples estrategias.")
        return None, None

    # Asegurarse potencia numérica (W -> kW)
    df_eq[potencia_col] = pd.to_numeric(df_eq[potencia_col].astype(str).str.replace(',', '.'), errors='coerce')
    df_eq['Potencia_kW'] = df_eq[potencia_col] / 1000.0

    # Interpretar estados: celda no vacía y no igual a '0' -> 1, else 0
    for col in hour_col_names:
        df_eq[col] = df_eq[col].apply(lambda x: 1 if (pd.notna(x) and str(x).strip() not in ['', '0', '0.0', 'nan']) else 0).astype(int)

    # Ordenar hour_col_names por el número de la hora si es posible:
    def try_hour_value(colname):
        s = str(colname).strip()
        if s.isdigit():
            return int(s)
        # else try reading raw header cell
        idx = raw_header.index(colname) if colname in raw_header else None
        try:
            if idx is not None:
                v = raw_header[idx].strip()
                if v.isdigit():
                    return int(v)
        except:
            pass
        return 999
    hour_col_names_sorted = sorted(hour_col_names, key=lambda c: try_hour_value(c))

    # Calcular totales horarios (kW)
    hourly_totals = []
    for col in hour_col_names_sorted:
        hourly_totals.append((df_eq[col].astype(int) * df_eq['Potencia_kW']).sum())

    timestamps = pd.date_range("2025-01-01", periods=len(hourly_totals), freq="H")
    df_hourly = pd.DataFrame({'timestamp': timestamps, 'consumption_kW': hourly_totals})

    # Preparar tabla de equipos limpia para mostrar
    display_cols = [c for c in df_eq.columns if c not in hour_col_names_sorted] + hour_col_names_sorted
    df_equipment_clean = df_eq[display_cols].copy()

    return df_hourly, df_equipment_clean

def compute_metrics(df_hourly):
    total_energy = df_hourly['consumption_kW'].sum()
    peak = df_hourly['consumption_kW'].max()
    hours = len(df_hourly)
    load_factor = total_energy / (peak * hours) if peak > 0 else 0
    return {
        'total_energy_kWh': total_energy,
        'peak_kW': peak,
        'hours': hours,
        'load_factor': load_factor
    }


def compute_ldc(df):
    series = df['consumption_kW'].sort_values(ascending=False).reset_index(drop=True)
    pct = (series.index + 1) / len(series) * 100
    return series, pct


def plot_time_series(df):
    return px.line(df, x='timestamp', y='consumption_kW', title="Serie temporal (potencia total por hora)",
                   labels={'timestamp': 'Hora', 'consumption_kW': 'Potencia (kW)'})


def plot_ldc(series, pct):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pct, y=series, mode='lines'))
    fig.update_layout(title='Load Duration Curve (LDC)',
                      xaxis_title='% del tiempo',
                      yaxis_title='Potencia (kW)')
    return fig


def plot_heatmap(df):
    df2 = df.copy()
    df2['date'] = df2['timestamp'].dt.date
    df2['hour'] = df2['timestamp'].dt.hour
    pivot = df2.pivot_table(index='date', columns='hour', values='consumption_kW', aggfunc='sum').fillna(0)
    fig = px.imshow(pivot.values, x=pivot.columns, y=pivot.index, color_continuous_scale='YlGnBu',
                    labels=dict(x='Hora', y='Día'), title='Heatmap: Potencia (kW)')
    return fig


def make_pdf(figs_dict, metrics):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, height - 40, "Reporte - Análisis Cuadro de Carga")
    y = height - 70
    c.setFont("Helvetica", 10)
    c.drawString(30, y, f"Energía total (kWh): {metrics['total_energy_kWh']:.2f}")
    c.drawString(230, y, f"Pico (kW): {metrics['peak_kW']:.2f}")
    c.drawString(400, y, f"Factor de carga: {metrics['load_factor']:.3f}")
    y -= 20
    for name, fig in figs_dict.items():
        try:
            img = fig.to_image(format='png', scale=2)
            img_io = BytesIO(img)
            c.drawImage(ImageReader(img_io), 30, y - 180, width=width - 60, height=160)
            y -= 200
            if y < 100:
                c.showPage()
                y = height - 60
        except Exception:
            continue
    c.save()
    buffer.seek(0)
    return buffer.getvalue()

# ---------------- INTERFAZ ---------------- #

st.title("📊 Analizador de Cuadro de Carga y LDC")

modo = st.radio("Selecciona cómo deseas ingresar los datos:",
                ("Cargar archivo Excel/CSV", "Ingresar manualmente"))

# ---------- MODO 1: CARGA DE ARCHIVO ---------- #
if modo == "Cargar archivo Excel/CSV":
    uploaded = st.file_uploader("Sube tu archivo de cuadro de carga", type=['xls', 'xlsx', 'csv'])
    if uploaded:
        df_raw = read_file(uploaded)
        if df_raw is None:
            st.stop()

        st.subheader("Vista previa (raw):")
        st.dataframe(df_raw)

        df_hourly, df_equipment = parse_load_schedule(df_raw)
        if df_hourly is None:
            st.stop()

        st.subheader("Tabla de equipos (limpia)")
        st.dataframe(df_equipment)

        # ahora usa df_hourly para todo lo demás
        df = df_hourly.copy()
        st.subheader("Perfil horario calculado (primeras 24 filas)")
        st.dataframe(df.head(24))

        # luego sigue con métricas y gráficos
        metrics = compute_metrics(df)   # ahora df es DataFrame, no tuple
        if df is not None:
            st.success("Archivo procesado correctamente ✅")

# ---------- MODO 2: INGRESO MANUAL ---------- #
else:
    st.subheader("🧾 Ingreso manual de cargas")
    n = st.number_input("Número de cargas a ingresar:", min_value=1, max_value=20, value=3)
    cargas = []
    for i in range(n):
        with st.expander(f"Carga #{i+1}"):
            nombre = st.text_input(f"Nombre de la carga #{i+1}", f"Carga {i+1}")
            potencia = st.number_input(f"Potencia (W) - Carga #{i+1}", min_value=0.0, value=1000.0)
            horas = st.multiselect(f"Horas de uso (0–23) para {nombre}", options=list(range(24)))
            data = {'Nombre': nombre, 'Potencia (w)': potencia}
            for h in range(24):
                data[h] = 1 if h in horas else 0
            cargas.append(data)
    if st.button("Generar perfil horario"):
        df_raw = pd.DataFrame(cargas)
        df_raw['Potencia_kW'] = df_raw['Potencia (w)'] / 1000
        hour_cols = list(range(24))
        hourly_values = [(df_raw[h] * df_raw['Potencia_kW']).sum() for h in hour_cols]
        timestamps = pd.date_range("2025-01-01", periods=24, freq="H")
        df = pd.DataFrame({'timestamp': timestamps, 'consumption_kW': hourly_values})
        st.success("Datos generados correctamente ✅")

# ---------- PROCESAMIENTO Y GRÁFICOS ---------- #
if 'df' in locals() and df is not None:
    metrics = compute_metrics(df)
    col1, col2, col3 = st.columns(3)
    col1.metric("Energía total (kWh)", f"{metrics['total_energy_kWh']:.2f}")
    col2.metric("Pico (kW)", f"{metrics['peak_kW']:.2f}")
    col3.metric("Factor de carga", f"{metrics['load_factor']:.3f}")

    fig_ts = plot_time_series(df)
    st.plotly_chart(fig_ts, use_container_width=True)

    series, pct = compute_ldc(df)
    fig_ldc = plot_ldc(series, pct)
    st.plotly_chart(fig_ldc, use_container_width=True)

    fig_heat = plot_heatmap(df)
    st.plotly_chart(fig_heat, use_container_width=True)

    # Exportar
    st.subheader("📤 Exportar resultados:")
    csv_bytes = df.to_csv(index=False).encode('utf-8')
    st.download_button("Descargar datos horarios (CSV)", data=csv_bytes,
                       file_name="perfil_horario.csv", mime="text/csv")

    if st.button("Generar reporte PDF"):
        pdf_bytes = make_pdf({"Serie temporal": fig_ts, "LDC": fig_ldc, "Heatmap": fig_heat}, metrics)
        st.download_button("Descargar reporte PDF", data=pdf_bytes,
                           file_name="reporte_cuadro_carga.pdf", mime="application/pdf")
        