import streamlit as st
import pandas as pd
import plotly.express as px
import numpy as np
from datetime import datetime

st.set_page_config(page_title="FFGH Immunization Dashboard", layout="wide", page_icon="💉")

@st.cache_data
def process_data(uploaded_file):
    df = pd.read_excel(uploaded_file)
    
    # Clean column names
    df.columns = df.columns.str.strip().str.replace(r":$", "", regex=True)
    df.columns = df.columns.str.replace("Has the child received any of the following immunizations? /", "Vax_")
    df.columns = df.columns.str.replace("Which of the following injections did you provide? /", "Provided_")
    df.columns = df.columns.str.replace("For which illness is treatment necessary?/", "Illness_")
    
    # Parse dates
    date_col = next((c for c in df.columns if c.startswith("start") or "date" in c.lower()), None)
    if date_col:
        df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    
    # Ensure vaccine columns are numeric
    vax_cols = [c for c in df.columns if c.startswith("Vax_")]
    for col in vax_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        
    return df, vax_cols

def main():
    st.title("💉 FFGH Ruwan Bore Immunization Dashboard")
    st.markdown("Track coverage, identify zero-dose children, and monitor CHEW performance.")

    uploaded_file = st.sidebar.file_uploader("📤 Upload CHEW Log (Excel)", type=["xlsx", "csv"])
    if not uploaded_file:
        st.warning("Please upload your CHEW log file to activate the dashboard.")
        return

    df, vax_cols = process_data(uploaded_file)
    if not vax_cols:
        st.error("❌ No vaccination columns detected. Check column naming in your Excel file.")
        return

    # Filters
    st.sidebar.header("🔍 Filters")
    villages = sorted(df["Village / Settlement"].dropna().unique().tolist()) if "Village / Settlement" in df.columns else []
    selected_village = st.sidebar.selectbox("Village / Settlement", ["All"] + villages)
    
    chew_col = next((c for c in df.columns if "chew" in c.lower()), None)
    chews = sorted(df[chew_col].dropna().unique().tolist()) if chew_col else []
    selected_chew = st.sidebar.selectbox("CHEW", ["All"] + chews)
    
    date_range = st.sidebar.date_input(
        "Date Range",
        [df["date"].min().date(), df["date"].max().date()],
        min_value=df["date"].min().date(),
        max_value=df["date"].max().date()
    )

    # Apply filters
    mask = pd.Series([True]*len(df))
    if selected_village != "All":
        mask &= (df["Village / Settlement"] == selected_village)
    if selected_chew != "All" and chew_col:
        mask &= (df[chew_col] == selected_chew)
    if len(date_range) == 2 and "date" in df.columns:
        mask &= (df["date"].dt.date >= date_range[0]) & (df["date"].dt.date <= date_range[1])
        
    df_f = df[mask]
    total = len(df_f)

    # KPIs
    st.subheader("📈 Key Metrics")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Records", f"{total:,}")
    c4.metric("Active CHEWs", df_f[chew_col].nunique() if chew_col else "N/A")

    # Zero-dose calculator
    bcg_col = "Vax_BCG" if "Vax_BCG" in vax_cols else None
    opv0_col = next((c for c in vax_cols if "OPV 0" in c or "Oral Polio Vaccine (OPV) 0" in c), None)
    if bcg_col and opv0_col:
        zero_dose = df_f[(df_f[bcg_col] == 0) & (df_f[opv0_col] == 0)].shape[0]
        c2.metric("Zero-Dose Children", f"{zero_dose:,}", delta=f"{(zero_dose/max(total,1)*100):.1f}%" if total > 0 else "0%")
    else:
        c2.metric("Zero-Dose", "Columns missing")

    # Vaccine selector
    selected_vax = st.sidebar.selectbox("Select Vaccine for Coverage", vax_cols)
    if selected_vax:
        coverage = (df_f[selected_vax].sum() / max(total, 1)) * 100
        c3.metric(f"{selected_vax.replace('Vax_', '')} Coverage", f"{coverage:.1f}%")

    st.markdown("---")
    
    # Charts
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📊 Coverage by Village")
        if selected_vax and "Village / Settlement" in df_f.columns:
            vax_cov = df_f.groupby("Village / Settlement")[selected_vax].agg(["sum", "count"])
            vax_cov["coverage"] = (vax_cov["sum"] / vax_cov["count"] * 100).round(1)
            fig_bar = px.bar(vax_cov.reset_index(), x="Village / Settlement", y="coverage",
                             color="coverage", color_continuous_scale="RdYlGn",
                             title=f"{selected_vax.replace('Vax_', '')} Coverage %")
            st.plotly_chart(fig_bar, use_container_width=True)

    with col2:
        st.subheader("📅 Monthly Coverage Trend")
        if selected_vax and "date" in df_f.columns:
            df_m = df_f.set_index("date").resample("ME").agg({selected_vax: ["sum", "count"]}).dropna(how="all")
            if not df_m.empty:
                df_m.columns = ["vaccinated", "total"]
                df_m["coverage"] = (df_m["vaccinated"] / df_m["total"] * 100).round(1)
                fig_line = px.line(df_m.reset_index(), x="date", y="coverage", markers=True, title="Monthly Trend")
                st.plotly_chart(fig_line, use_container_width=True)

    # Data Table & Export
    st.markdown("---")
    st.subheader("📋 Filtered Records")
    display_cols = [c for c in ["start", "Village / Settlement", chew_col, "Child's Name", "Caregiver Phone", "Age (in Years)"] if c in df_f.columns]
    st.dataframe(df_f[display_cols + vax_cols[:6]], use_container_width=True, height=350)

    csv = df_f.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download Filtered CSV", csv, "ffgh_filtered_data.csv", "text/csv")

if __name__ == "__main__":
    main()