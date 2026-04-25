import streamlit as st
import pandas as pd
import plotly.express as px
import numpy as np
from datetime import datetime, timedelta
import re

st.set_page_config(page_title="FFGH Immunization Dashboard", layout="wide", page_icon="💉")

@st.cache_data
def get_sheet_names(uploaded_file):
    """Get all sheet names from Excel file"""
    try:
        xl = pd.ExcelFile(uploaded_file, engine='openpyxl')
        return xl.sheet_names
    except Exception as e:
        st.error(f"Error reading Excel file: {e}")
        return []

@st.cache_data
def parse_messy_date(date_val):
    """Handle multiple date formats found in CHEW logs"""
    if pd.isna(date_val):
        return pd.NaT
    
    date_str = str(date_val).strip()
    
    # Try common formats
    formats = [
        "%Y-%m-%d",           # 2026-04-06
        "%d/%m/%Y %H:%M",     # 6/3/26 11:48
        "%d/%m/%Y %H:%M:%S",  # 22/03/2026 10:59:04
        "%Y%m%d",             # 20260406
        "%d-%m-%Y",           # 06-04-2026
    ]
    
    for fmt in formats:
        try:
            return pd.to_datetime(date_str, format=fmt)
        except:
            continue
    
    # Fallback: try pandas auto-parse
    try:
        return pd.to_datetime(date_str, errors='coerce')
    except:
        return pd.NaT

@st.cache_data
def process_data(uploaded_file, sheet_name=None):
    """Load and clean CHEW log data from selected sheet"""
    # Read file
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='openpyxl')
    
    # Clean column names
    df.columns = df.columns.str.strip()
    df.columns = df.columns.str.replace(r":$", "", regex=True)
    df.columns = df.columns.str.replace("Has the child received any of the following immunizations? /", "Vax_")
    df.columns = df.columns.str.replace("Which of the following injections did you provide? /", "Provided_")
    df.columns = df.columns.str.replace("For which illness is treatment necessary?/", "Illness_")
    
    # Parse dates - try multiple columns
    date_candidates = ['start', 'Start', 'date', 'Date', 'submission_time', '_submission_time']
    date_col = next((c for c in date_candidates if c in df.columns), None)
    
    if date_col:
        df['date'] = df[date_col].apply(parse_messy_date)
        df = df[df['date'].notna()]
    else:
        df['date'] = pd.NaT
        st.warning("⚠️ No valid date column detected. Date filters will be disabled.")
    
    # Ensure vaccine columns are numeric (0/1)
    vax_cols = [c for c in df.columns if c.startswith("Vax_")]
    for col in vax_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    
    # Standardize village names
    if "Village / Settlement" in df.columns:
        df["Village / Settlement"] = df["Village / Settlement"].str.strip().str.title()
    
    # Deduplicate by UUID
    uuid_col = "uuid" if "uuid" in df.columns else "_uuid" if "_uuid" in df.columns else None
    if uuid_col:
        df = df.drop_duplicates(subset=[uuid_col], keep="last")
    
    return df, vax_cols

def main():
    st.title("💉 FFGH Ruwan Bore Immunization Dashboard")
    st.markdown("*Track coverage, identify zero-dose children, and prioritize outreach in Nigeria*")

    uploaded_file = st.sidebar.file_uploader("📤 Upload CHEW Log (Excel/CSV)", type=["xlsx", "xls", "csv"])
    if not uploaded_file:
        st.info("👆 Please upload your CHEW log file to activate the dashboard.")
        st.markdown("""
        ### 📋 Expected columns:
        - `Village / Settlement`
        - `CHEW Name`  
        - Date column (`start`, `date`, etc.)
        - Vaccine columns like `Vax_BCG`, `Vax_Oral Polio Vaccine (OPV) 0`, etc.
        """)
        return

    # Sheet selector for Excel files
    sheet_name = None
    if uploaded_file.name.endswith(('.xlsx', '.xls')):
        sheet_names = get_sheet_names(uploaded_file)
        if sheet_names:
            sheet_name = st.sidebar.selectbox(
                "📑 Select Sheet",
                sheet_names,
                help="Choose which sheet to analyze"
            )
        else:
            st.error("❌ No sheets found in Excel file")
            return

    with st.spinner("Processing your data..."):
        df, vax_cols = process_data(uploaded_file, sheet_name)
    
    if df.empty:
        st.error("❌ No valid data found in selected sheet.")
        return
        
    if not vax_cols:
        st.warning("⚠️ No vaccination columns detected. Check that columns start with 'Vax_'")
        with st.expander("🔍 Debug: Available columns"):
            st.write(df.columns.tolist())
        return

    # ===== SIDEBAR FILTERS =====
    st.sidebar.header("🔍 Filters")
    
    # Village filter
    village_col = "Village / Settlement"
    if village_col in df.columns:
        villages = sorted(df[village_col].dropna().unique().tolist())
        selected_village = st.sidebar.selectbox("🏘️ Village / Settlement", ["All"] + villages)
    else:
        selected_village = "All"
    
    # CHEW filter
    chew_col = next((c for c in df.columns if "chew" in c.lower() or "CHEW" in c), None)
    if chew_col:
        chews = sorted(df[chew_col].dropna().unique().tolist())
        selected_chew = st.sidebar.selectbox("👩‍⚕️ CHEW", ["All"] + chews)
    else:
        selected_chew = "All"
    
    # Date range picker
    st.sidebar.subheader("📅 Date Range")
    if "date" in df.columns and df["date"].notna().any():
        min_date = df["date"].min().date()
        max_date = df["date"].max().date()
        default_end = max_date
        default_start = max(min_date, (datetime.now().date() - timedelta(days=30)))
        
        date_range = st.sidebar.date_input(
            "Select dates",
            value=[default_start, default_end],
            min_value=min_date,
            max_value=max_date,
            help=f"Available: {min_date} to {max_date}"
        )
        
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start_date, end_date = date_range
            if start_date > end_date:
                st.sidebar.error("⚠️ Start date must be before end date")
                st.stop()
        else:
            st.sidebar.warning("Please select both start and end dates")
            st.stop()
    else:
        start_date = end_date = None

    # Vaccine selector
    if vax_cols:
        vaccine_options = {v.replace("Vax_", "").replace("_", " "): v for v in vax_cols}
        selected_label = st.sidebar.selectbox(
            "💉 Select Vaccine for Coverage", 
            list(vaccine_options.keys()),
            index=0 if "Measles (MCV) 1" in vaccine_options else 0
        )
        selected_vax = vaccine_options[selected_label]
    else:
        selected_vax = None

    # ===== APPLY FILTERS =====
    mask = pd.Series([True] * len(df))
    if selected_village != "All" and village_col in df.columns:
        mask &= (df[village_col] == selected_village)
    if selected_chew != "All" and chew_col:
        mask &= (df[chew_col] == selected_chew)
    if start_date and end_date and "date" in df.columns:
        mask &= (df["date"].dt.date >= start_date) & (df["date"].dt.date <= end_date)
    
    df_f = df[mask].copy()
    total_records = len(df_f)

    # ===== KEY METRICS =====
    st.subheader("📊 Key Metrics")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 Total Records", f"{total_records:,}")
    if chew_col:
        c4.metric("👩‍⚕️ Active CHEWs", df_f[chew_col].nunique())
    
    # Zero-dose calculation
    bcg_col = "Vax_BCG" if "Vax_BCG" in df_f.columns else None
    opv0_col = next((c for c in df_f.columns if "OPV 0" in c or "Oral Polio Vaccine (OPV) 0" in c), None)
    if bcg_col and opv0_col and total_records > 0:
        zero_dose = df_f[(df_f[bcg_col] == 0) & (df_f[opv0_col] == 0)].shape[0]
        zero_pct = (zero_dose / total_records) * 100
        c2.metric("🚫 Zero-Dose Children", f"{zero_dose:,}", delta=f"{zero_pct:.1f}% of total")
    else:
        c2.metric("🚫 Zero-Dose", "Data unavailable")
    
    if selected_vax and selected_vax in df_f.columns and total_records > 0:
        coverage = (df_f[selected_vax].sum() / total_records) * 100
        c3.metric(f"💉 {selected_label} Coverage", f"{coverage:.1f}%")
    elif selected_vax:
        c3.metric(f"💉 {selected_label}", "N/A")

    st.markdown("---")

    # ===== VISUALIZATIONS =====
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🗺️ Coverage by Village")
        if selected_vax and village_col in df_f.columns and selected_vax in df_f.columns:
            vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
            vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
            vax_cov = vax_cov[vax_cov['count'] >= 3]
            
            if not vax_cov.empty:
                fig_bar = px.bar(
                    vax_cov, 
                    x=village_col, 
                    y='coverage',
                    color='coverage',
                    color_continuous_scale=["#e74c3c", "#f39c12", "#27ae60"],
                    title=f"{selected_label} Coverage by Village"
                )
                fig_bar.update_layout(xaxis_tickangle=-45, height=400)
                st.plotly_chart(fig_bar, use_container_width=True)
            else:
                st.info("ℹ️ Insufficient data for village breakdown")
        else:
            st.info("ℹ️ Select a vaccine to view coverage map")
    
    with col2:
        st.subheader("📈 Monthly Trend")
        if selected_vax and "date" in df_f.columns and selected_vax in df_f.columns:
            df_temp = df_f.set_index("date").resample("ME").agg({selected_vax: ["sum", "count"]}).dropna(how="all")
            if not df_temp.empty:
                df_temp.columns = ["vaccinated", "total"]
                df_temp["coverage"] = (df_temp["vaccinated"] / df_temp["total"] * 100).round(1)
                fig_line = px.line(
                    df_temp.reset_index(), 
                    x="date", 
                    y="coverage",
                    markers=True,
                    title=f"{selected_label} Monthly Coverage Trend"
                )
                fig_line.update_layout(height=400)
                st.plotly_chart(fig_line, use_container_width=True)
            else:
                st.info("ℹ️ No trend data available")
        else:
            st.info("ℹ️ Date column required for trend view")

    # ===== COLD SPOT ALERTS =====
    st.markdown("---")
    st.subheader("🚨 Priority Areas for Outreach")
    if selected_vax and village_col in df_f.columns and selected_vax in df_f.columns:
        vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
        vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
        vax_cov = vax_cov[vax_cov['count'] >= 3]
        cold_spots = vax_cov[vax_cov['coverage'] < 50].sort_values('coverage')
        
        if not cold_spots.empty:
            st.warning(f"⚠️ {len(cold_spots)} villages with <50% coverage need urgent attention:")
            for _, row in cold_spots.head(5).iterrows():
                st.markdown(f"- **{row[village_col]}**: {row['coverage']:.1f}% coverage ({int(row['count']-row['sum'])} unvaccinated)")
        else:
            st.success("✅ All villages with sufficient data have >50% coverage!")
    else:
        st.info("ℹ️ Select a vaccine to identify cold spots")

    # ===== DATA TABLE & EXPORT =====
    st.markdown("---")
    st.subheader("📋 Filtered Records")
    display_cols = [c for c in [
        "date", village_col, chew_col, "Child's Name", "Caregiver Phone", "Age (in Years)"
    ] if c in df_f.columns]
    vax_display = [v for v in vax_cols[:6] if v in df_f.columns]
    
    if display_cols or vax_display:
        st.dataframe(df_f[display_cols + vax_display].head(100), use_container_width=True, height=300)
        csv = df_f.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Filtered Data (CSV)",
            csv,
            f"ffgh_filtered_{datetime.now().strftime('%Y%m%d')}.csv",
            "text/csv"
        )
    else:
        st.info("ℹ️ No recognizable columns to display")

    st.markdown("---")
    st.caption("💡 *Tip: Use filters to focus on specific villages, CHEWs, or time periods. Export data to share with LGA officials.*")

if __name__ == "__main__":
    main()
