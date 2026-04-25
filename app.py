import streamlit as st
import pandas as pd
import plotly.express as px
import numpy as np
from datetime import datetime, timedelta
import re
import time
import json
from rapidfuzz import fuzz
from collections import Counter
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from geopy.distance import geodesic

st.set_page_config(page_title="FFGH Immunization Dashboard", layout="wide", page_icon="💉")

# ================= HELPER FUNCTIONS =================

@st.cache_data
def get_sheet_names(uploaded_file):
    try:
        return pd.ExcelFile(uploaded_file, engine='openpyxl').sheet_names
    except Exception as e:
        st.error(f"Error reading Excel file: {e}")
        return []

@st.cache_data
def parse_robust_date(date_val):
    if pd.isna(date_val) or str(date_val).strip() == '':
        return pd.NaT
    date_str = str(date_val).strip()
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"]
    for fmt in formats:
        try:
            return pd.to_datetime(date_str, format=fmt, errors='raise')
        except:
            continue
    try:
        return pd.to_datetime(date_str, errors='coerce')
    except:
        return pd.NaT

def normalize_text(text):
    if pd.isna(text): return ""
    text = str(text).strip().lower()
    text = re.sub(r'[^\w\s]', '', text)
    return re.sub(r'\s+', ' ', text)

def find_name_clusters(series, threshold=85):
    clean_series = series.dropna().astype(str).str.strip()
    unique_names = clean_series.unique()
    if len(unique_names) == 0: return {}
    
    assigned = set()
    clusters = {}
    name_counts = Counter(clean_series)
    sorted_names = sorted(unique_names, key=lambda x: name_counts[x], reverse=True)
    
    for name in sorted_names:
        if name in assigned: continue
        cluster = [name]
        assigned.add(name)
        for candidate in sorted_names:
            if candidate in assigned: continue
            if fuzz.ratio(normalize_text(name), normalize_text(candidate)) >= threshold:
                cluster.append(candidate)
                assigned.add(candidate)
        if len(cluster) > 1:
            canonical = max(cluster, key=lambda x: name_counts[x])
            clusters[canonical] = cluster
    return clusters

def apply_name_mapping(series, clusters):
    mapping = {var: canonical for canonical, vars in clusters.items() for var in vars}
    return series.apply(lambda x: mapping.get(str(x).strip(), x) if pd.notna(x) else x)

@st.cache_data(ttl=3600)
def geocode_villages(village_tuple):
    geolocator = Nominatim(user_agent="ffgh_immunization_dashboard", timeout=10)
    coords = {}
    villages = list(village_tuple)
    progress = st.progress(0)
    
    for i, v in enumerate(villages):
        try:
            loc = geolocator.geocode(f"{v}, Kano State, Nigeria", exactly_one=True)
            if not loc:
                loc = geolocator.geocode(f"{v}, Nigeria", exactly_one=True)
            coords[v] = (loc.latitude, loc.longitude) if loc else None
        except (GeocoderTimedOut, GeocoderServiceError):
            coords[v] = None
        progress.progress((i + 1) / len(villages))
        time.sleep(1.1)
        
    progress.empty()
    return coords

# ================= ROUTE OPTIMIZATION =================
def solve_tsp_route(coords_dict, start_village=None):
    """Nearest Neighbor + 2-Opt TSP solver for outreach routing"""
    villages = list(coords_dict.keys())
    coords = list(coords_dict.values())
    if len(villages) < 2:
        return villages, 0, 0
    
    # Find start index
    start_idx = 0
    if start_village and start_village in villages:
        start_idx = villages.index(start_village)
        
    # Nearest Neighbor
    n = len(villages)
    visited = [False] * n
    visited[start_idx] = True
    route = [start_idx]
    current = start_idx
    total_dist = 0
    
    for _ in range(n - 1):
        nearest = -1
        min_dist = float('inf')
        for j in range(n):
            if not visited[j]:
                d = geodesic(coords[current], coords[j]).km
                if d < min_dist:
                    min_dist = d
                    nearest = j
        visited[nearest] = True
        route.append(nearest)
        total_dist += min_dist
        current = nearest
    
    # Return to start
    total_dist += geodesic(coords[route[-1]], coords[route[0]]).km
    
    # 2-Opt Improvement
    improved = True
    iterations = 0
    while improved and iterations < 100:
        improved = False
        iterations += 1
        for i in range(1, len(route) - 2):
            for j in range(i + 1, len(route)):
                if j - i == 1: continue
                old_cost = (geodesic(coords[route[i-1]], coords[route[i]]).km + 
                            geodesic(coords[route[j]], coords[route[(j+1)%len(route)]]).km)
                new_cost = (geodesic(coords[route[i-1]], coords[route[j]]).km + 
                            geodesic(coords[route[i]], coords[route[(j+1)%len(route)]]).km)
                if new_cost < old_cost:
                    route[i:j+1] = reversed(route[i:j+1])
                    improved = True
                    
    return [villages[i] for i in route], round(total_dist, 2), iterations

# ================= DATA PROCESSING =================
@st.cache_data
def process_data(uploaded_file, sheet_name=None):
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='openpyxl')
    
    df.columns = df.columns.str.strip()
    df.columns = df.columns.str.replace(r":$", "", regex=True)
    df.columns = df.columns.str.replace("Has the child received any of the following immunizations? /", "Vax_", regex=False)
    df.columns = df.columns.str.replace("Which of the following injections did you provide? /", "Provided_", regex=False)
    df.columns = df.columns.str.replace("For which illness is treatment necessary?/", "Illness_", regex=False)
    
    date_candidates = ['start', 'Start', 'date', 'Date', 'Enter the date', 'submission_time']
    date_col = next((c for c in date_candidates if c in df.columns), None)
    if not date_col and len(df.columns) > 0:
        first_col = df.columns[0]
        if df[first_col].dropna().astype(str).str.match(r'\d{4}[-/]\d{2}[-/]\d{2}').any():
            date_col = first_col
            
    if date_col:
        df['date'] = df[date_col].apply(parse_robust_date)
        df = df[df['date'].notna()].copy()
    else:
        df['date'] = pd.NaT
        
    if "Village / Settlement" in df.columns:
        v_clusters = find_name_clusters(df["Village / Settlement"], threshold=85)
        if v_clusters:
            df["Village / Settlement"] = apply_name_mapping(df["Village / Settlement"], v_clusters)
            
    chew_col = next((c for c in df.columns if "chew" in c.lower()), None)
    if chew_col:
        c_clusters = find_name_clusters(df[chew_col], threshold=90)
        if c_clusters:
            df[chew_col] = apply_name_mapping(df[chew_col], c_clusters)
    
    vax_cols = [c for c in df.columns if c.startswith("Vax_")]
    for col in vax_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    
    uuid_col = "uuid" if "uuid" in df.columns else "_uuid" if "_uuid" in df.columns else None
    if uuid_col:
        df = df.drop_duplicates(subset=[uuid_col], keep="last")
        
    return df, vax_cols, chew_col

# ================= MAIN APP =================
def main():
    st.title("💉 FFGH Ruwan Bore Immunization Dashboard")
    st.markdown("*Track coverage, identify zero-dose children, and prioritize outreach in Nigeria*")

    uploaded_file = st.sidebar.file_uploader("📤 Upload CHEW Log (Excel/CSV)", type=["xlsx", "xls", "csv"])
    if not uploaded_file:
        st.info("👆 Please upload your CHEW log file to activate the dashboard.")
        return

    sheet_name = None
    if uploaded_file.name.endswith(('.xlsx', '.xls')):
        sheet_names = get_sheet_names(uploaded_file)
        if sheet_names:
            sheet_name = st.sidebar.selectbox("📑 Select Sheet", sheet_names, help="Choose which sheet to analyze")
        else:
            st.error("❌ No sheets found in Excel file")
            return

    with st.spinner("Processing & auto-cleaning data..."):
        df, vax_cols, chew_col = process_data(uploaded_file, sheet_name)
    
    if df.empty:
        st.error("❌ No valid data found after cleaning.")
        return
    if not vax_cols:
        st.warning("⚠️ No vaccination columns detected. Check that immunization columns start with 'Vax_'")
        return

    # ===== SIDEBAR FILTERS =====
    st.sidebar.header("🔍 Filters")
    village_col = "Village / Settlement"
    if village_col in df.columns:
        villages = sorted(df[village_col].dropna().unique().tolist())
        selected_village = st.sidebar.selectbox("🏘️ Village / Settlement", ["All"] + villages)
    else:
        selected_village = "All"
        
    if chew_col:
        chews = sorted(df[chew_col].dropna().unique().tolist())
        selected_chew = st.sidebar.selectbox("👩‍⚕️ CHEW", ["All"] + chews)
    else:
        selected_chew = "All"

    st.sidebar.subheader("📅 Date Range")
    start_date = end_date = None
    if "date" in df.columns and df["date"].notna().any():
        try:
            min_dt = df["date"].min()
            max_dt = df["date"].max()
            if pd.notna(min_dt) and pd.notna(max_dt) and min_dt <= max_dt:
                min_date = min_dt.to_pydatetime().date()
                max_date = max_dt.to_pydatetime().date()
                today = datetime.now().date()
                default_end = min(max_date, today)
                default_start = max(min_date, default_end - timedelta(days=30))
                date_range = st.sidebar.date_input("Select dates", value=[default_start, default_end], min_value=min_date, max_value=max_date)
                if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                    start_date, end_date = date_range
                    if start_date > end_date:
                        st.sidebar.error("⚠️ Start date must be before end date")
                        st.stop()
                elif isinstance(date_range, datetime):
                    start_date = end_date = date_range.date()
        except Exception as e:
            st.sidebar.warning(f"⚠️ Date filter unavailable: {str(e)[:50]}")

    vaccine_options = {v.replace("Vax_", "").replace("_", " "): v for v in vax_cols}
    default_idx = next((i for i, label in enumerate(vaccine_options.keys()) if "Measles" in label or "MCV" in label), 0)
    selected_label = st.sidebar.selectbox("💉 Select Vaccine for Coverage", list(vaccine_options.keys()), index=default_idx)
    selected_vax = vaccine_options[selected_label]

    # ===== APPLY FILTERS =====
    mask = pd.Series([True] * len(df))
    if selected_village != "All": mask &= (df[village_col] == selected_village)
    if selected_chew != "All" and chew_col: mask &= (df[chew_col] == selected_chew)
    if start_date and end_date: mask &= (df["date"].dt.date >= start_date) & (df["date"].dt.date <= end_date)
    df_f = df[mask].copy()
    total = len(df_f)

    # ===== TABS UI =====
    tab1, tab2, tab3 = st.tabs(["📊 Analytics Dashboard", "🗺️ Coverage Map", "🚑 Outreach Route Planner"])
    
    with tab1:
        st.subheader("📊 Key Metrics")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📋 Total Records", f"{total:,}")
        if chew_col: c4.metric("👩‍⚕️ Active CHEWs", df_f[chew_col].nunique())
        
        bcg_col = "Vax_BCG" if "Vax_BCG" in df_f.columns else None
        opv0_col = next((c for c in df_f.columns if "OPV 0" in c or "Oral Polio Vaccine (OPV) 0" in c), None)
        if bcg_col and opv0_col and total > 0:
            zero_dose = df_f[(df_f[bcg_col] == 0) & (df_f[opv0_col] == 0)].shape[0]
            c2.metric("🚫 Zero-Dose Children", f"{zero_dose:,}", delta=f"{(zero_dose/total*100):.1f}%")
        else:
            c2.metric("🚫 Zero-Dose", "N/A")
            
        if total > 0:
            cov = (df_f[selected_vax].sum() / total) * 100
            c3.metric(f"💉 {selected_label} Coverage", f"{cov:.1f}%")
        else:
            c3.metric(f"💉 {selected_label}", "0.0%")

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("🗺️ Coverage by Village")
            if village_col in df_f.columns and selected_vax in df_f.columns:
                vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
                vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
                vax_cov = vax_cov[vax_cov['count'] >= 3]
                if not vax_cov.empty:
                    fig = px.bar(vax_cov, x=village_col, y='coverage', color='coverage',
                               color_continuous_scale=["#e74c3c", "#f39c12", "#27ae60"],
                               title=f"{selected_label} Coverage by Village")
                    fig.update_layout(xaxis_tickangle=-45, height=400)
                    st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("📈 Monthly Trend")
            if "date" in df_f.columns and selected_vax in df_f.columns:
                df_temp = df_f.set_index("date").resample("ME").agg({selected_vax: ["sum", "count"]}).dropna(how="all")
                if not df_temp.empty:
                    df_temp.columns = ["vaccinated", "total"]
                    df_temp["coverage"] = (df_temp["vaccinated"] / df_temp["total"] * 100).round(1)
                    fig = px.line(df_temp.reset_index(), x="date", y="coverage", markers=True,
                                title=f"{selected_label} Monthly Coverage Trend")
                    fig.update_layout(height=400)
                    st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.subheader("🚨 Priority Areas for Outreach")
        if village_col in df_f.columns and selected_vax in df_f.columns:
            vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
            vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
            vax_cov = vax_cov[vax_cov['count'] >= 3]
            cold = vax_cov[vax_cov['coverage'] < 50].sort_values('coverage')
            if not cold.empty:
                st.warning(f"⚠️ {len(cold)} villages with <50% coverage need urgent attention:")
                for _, row in cold.head(5).iterrows():
                    st.markdown(f"- **{row[village_col]}**: {row['coverage']:.1f}% coverage ({int(row['count']-row['sum'])} unvaccinated)")
            else:
                st.success("✅ All villages with sufficient data have >50% coverage!")

    with tab2:
        st.subheader("🗺️ Interactive Coverage Map")
        st.markdown("*Map uses OpenStreetMap geocoding. Toggle LGA boundaries for geographic context.*")
        
        if village_col in df_f.columns and selected_vax in df_f.columns:
            # Geocode
            cov_df = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
            cov_df['coverage_pct'] = (cov_df['sum'] / cov_df['count'] * 100).round(1)
            cov_df = cov_df[cov_df['count'] >= 3]
            
            if not cov_df.empty:
                coords = geocode_villages(tuple(cov_df[village_col].unique()))
                cov_df['coords'] = cov_df[village_col].map(coords)
                cov_df = cov_df.dropna(subset=['coords'])
                
                if not cov_df.empty:
                    m = folium.Map(location=[9.0, 8.5], zoom_start=7, tiles="CartoDB positron")
                    folium.LayerControl(collapsed=False).add_to(m)
                    
                    # LGA Boundaries Layer
                    st.sidebar.subheader("🗺️ Map Layers")
                    show_lga = st.sidebar.checkbox("Show LGA Boundaries", value=False)
                    lga_file = st.sidebar.file_uploader("📂 Upload LGA GeoJSON", type=["geojson", "json"], key="lga_uploader")
                    
                    if show_lga:
                        if lga_file:
                            try:
                                lga_data = json.load(lga_file)
                                folium.GeoJson(
                                    lga_data, 
                                    name="LGA Boundaries",
                                    style_function=lambda x: {'fillColor': 'transparent', 'color': '#2c3e50', 'weight': 1.5, 'opacity': 0.8}
                                ).add_to(m)
                                st.sidebar.success("✅ LGA boundaries loaded")
                            except Exception as e:
                                st.sidebar.error(f"❌ Invalid GeoJSON: {e}")
                        else:
                            st.sidebar.info("💡 Upload Nigeria LGA GeoJSON or fetch from HDX")
                            st.markdown("[📥 Download Nigeria LGA Boundaries (GeoJSON)](https://data.humdata.org/dataset/cod-ab-nga)")
                    
                    # Coverage Markers
                    for _, row in cov_df.iterrows():
                        lat, lon = row['coords']
                        pct = row['coverage_pct']
                        unvax = int(row['count'] - row['sum'])
                        color = "#e74c3c" if pct < 50 else "#f39c12" if pct < 80 else "#27ae60"
                        radius = 6 + (pct / 15)
                        popup_html = f"""<div style="font-family:sans-serif; min-width:150px;">
                            <b>{row[village_col]}</b><br>
                            <span style="color:{color};">● Coverage: {pct}%</span><br>
                            Unvaccinated: {unvax} | Total: {int(row['count'])}
                        </div>"""
                        folium.CircleMarker(
                            location=[lat, lon], radius=radius, color=color, weight=2, fill=True,
                            fill_color=color, fill_opacity=0.75, popup=folium.Popup(popup_html, max_width=250),
                            tooltip=f"{row[village_col]}: {pct}%"
                        ).add_to(m)
                        
                    sw = cov_df['coords'].apply(lambda x: x[0]).min() - 0.1
                    ne = cov_df['coords'].apply(lambda x: x[1]).max() + 0.1
                    m.fit_bounds([[sw - 0.2, ne - 0.2], [sw + 0.2, ne + 0.2]])
                    st_folium(m, width=700, height=500, returned_objects=[])
                    
                    # Export coordinates
                    cov_export = cov_df.copy()
                    cov_export['Lat'] = cov_export['coords'].apply(lambda x: x[0])
                    cov_export['Lon'] = cov_export['coords'].apply(lambda x: x[1])
                    csv = cov_export[['Village / Settlement', 'Lat', 'Lon', 'coverage_pct']].to_csv(index=False).encode("utf-8")
                    st.download_button("⬇️ Download Geocoded Coordinates", csv, "village_coordinates.csv", "text/csv")
                else:
                    st.warning("⚠️ Geocoding failed for all villages.")
            else:
                st.info("ℹ️ Insufficient data for mapping (min 3 records per village).")
        else:
            st.info("ℹ️ Select a vaccine and ensure village data is available.")

    with tab3:
        st.subheader("🚑 Outreach Route Optimizer")
        st.markdown("*Calculates the most efficient travel sequence for CHEW teams. Uses straight-line distances × 1.3 road factor.*")
        
        if village_col in df_f.columns:
            # Get villages with coordinates
            cov_df = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
            cov_df['coverage_pct'] = (cov_df['sum'] / cov_df['count'] * 100).round(1)
            cov_df = cov_df[cov_df['count'] >= 2]
            
            if not cov_df.empty:
                coords = geocode_villages(tuple(cov_df[village_col].unique()))
                valid_coords = {k: v for k, v in coords.items() if v is not None}
                
                if len(valid_coords) >= 2:
                    st.sidebar.subheader("📍 Route Settings")
                    target_type = st.sidebar.radio("Optimize for:", ["Cold Spots (<50%)", "All Villages", "Custom Selection"])
                    
                    if target_type == "Cold Spots (<50%)":
                        target_villages = cov_df[cov_df['coverage_pct'] < 50][village_col].tolist()
                    elif target_type == "All Villages":
                        target_villages = list(valid_coords.keys())
                    else:
                        target_villages = st.sidebar.multiselect("Select villages", list(valid_coords.keys()))
                        
                    start_point = st.sidebar.selectbox("🏁 Starting Point", ["First village in route"] + list(valid_coords.keys()))
                    avg_speed = st.sidebar.slider("Avg road speed (km/h)", 20, 50, 35, step=5)
                    time_per_stop = st.sidebar.slider("Time per village (min)", 10, 45, 20, step=5)
                    
                    if st.sidebar.button(" Calculate Optimal Route", type="primary"):
                        if len(target_villages) < 2:
                            st.warning("⚠️ Select at least 2 villages for routing.")
                        else:
                            subset_coords = {v: valid_coords[v] for v in target_villages if v in valid_coords}
                            if not subset_coords:
                                st.error("❌ No valid coordinates for selected villages.")
                            else:
                                with st.spinner("Optimizing route..."):
                                    route_order, total_dist, iterations = solve_tsp_route(subset_coords, start_point if start_point != "First village in route" else None)
                                    
                                    # Road factor adjustment
                                    road_factor = 1.3
                                    road_dist = round(total_dist * road_factor, 2)
                                    travel_time = road_dist / avg_speed  # hours
                                    stop_time = len(route_order) * time_per_stop / 60  # hours
                                    total_time = travel_time + stop_time
                                    
                                    st.success(f"✅ Route optimized in {iterations} iterations")
                                    c1, c2, c3 = st.columns(3)
                                    c1.metric("📏 Straight Distance", f"{total_dist} km")
                                    c2.metric("🛣️ Est. Road Distance", f"{road_dist} km")
                                    c3.metric("⏱️ Est. Total Time", f"{total_time:.1f} hrs")
                                    
                                    # Route table
                                    route_df = pd.DataFrame({
                                        "Sequence": range(1, len(route_order) + 1),
                                        "Village": route_order,
                                        "Lat": [subset_coords[v][0] for v in route_order],
                                        "Lon": [subset_coords[v][1] for v in route_order],
                                        "Coverage %": [cov_df.set_index(village_col).loc[v, 'coverage_pct'] for v in route_order]
                                    })
                                    
                                    # Calculate cumulative distance & ETA
                                    cum_dist = 0
                                    current_time = 0
                                    cum_dists = []
                                    etas = []
                                    for i in range(len(route_order)):
                                        if i == 0:
                                            cum_dist = 0
                                            current_time = 0
                                        else:
                                            d = geodesic(subset_coords[route_order[i-1]], subset_coords[route_order[i]]).km * road_factor
                                            cum_dist += d
                                            travel_h = d / avg_speed
                                            current_time += travel_h + (time_per_stop/60)
                                        cum_dists.append(round(cum_dist, 2))
                                        etas.append(current_time)
                                        
                                    route_df["Cum. Distance (km)"] = cum_dists
                                    route_df["ETA (hrs)"] = [f"{t:.1f}" for t in etas]
                                    st.dataframe(route_df, use_container_width=True)
                                    
                                    csv_route = route_df.to_csv(index=False).encode("utf-8")
                                    st.download_button("⬇️ Download Route Plan (CSV)", csv_route, "outreach_route_plan.csv", "text/csv")
                                    
                                    # Draw route on map
                                    m_route = folium.Map(location=[subset_coords[route_order[0]][0], subset_coords[route_order[0]][1]], zoom_start=10)
                                    folium.LayerControl().add_to(m_route)
                                    
                                    # Add markers
                                    for i, v in enumerate(route_order):
                                        lat, lon = subset_coords[v]
                                        folium.Marker(
                                            [lat, lon],
                                            popup=f"{i+1}. {v}",
                                            icon=folium.Icon(color="blue" if i == 0 else "green" if i == len(route_order)-1 else "orange", prefix="fa", icon=str(i+1))
                                        ).add_to(m_route)
                                    
                                    # Draw polyline
                                    route_coords = [subset_coords[v] for v in route_order] + [subset_coords[route_order[0]]]
                                    folium.PolyLine(
                                        route_coords,
                                        color="#e74c3c",
                                        weight=4,
                                        opacity=0.8,
                                        dash_array="10, 5"
                                    ).add_to(m_route)
                                    
                                    st_folium(m_route, width=700, height=500)
                                else:
                                    st.warning("⚠️ Need at least 2 geocoded villages for routing.")
                            else:
                                st.info("ℹ️ Select villages to generate route.")
                        else:
                            st.info("ℹ️ Insufficient village data for routing.")
                else:
                    st.info("ℹ️ Not enough data for route optimization.")

    st.markdown("---")
    st.subheader("📋 Filtered Records")
    display_cols = [c for c in ["date", village_col, chew_col, "Child's Name", "Caregiver Phone", "Age (in Years)"] if c in df_f.columns]
    vax_display = [v for v in vax_cols[:6] if v in df_f.columns]
    if display_cols or vax_display:
        st.dataframe(df_f[display_cols + vax_display].head(100), use_container_width=True, height=300)
        csv = df_f.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download Filtered CSV", csv, f"ffgh_filtered_{datetime.now().strftime('%Y%m%d')}.csv", "text/csv")

if __name__ == "__main__":
    main()
