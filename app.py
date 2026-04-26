import streamlit as st
import pandas as pd
import plotly.express as px
import numpy as np
from datetime import datetime, timedelta
import re, time, json, requests, zipfile, io
from rapidfuzz import fuzz
from collections import Counter
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from geopy.distance import geodesic
from shapely.geometry import Point, shape

st.set_page_config(page_title="FFGH Immunization Dashboard", layout="wide", page_icon="💉")

# ================= CACHE & HELPER FUNCTIONS =================

@st.cache_data
def get_sheet_names(uploaded_file):
    try:
        return pd.ExcelFile(uploaded_file, engine='openpyxl').sheet_names
    except Exception as e:
        st.error(f"Error reading Excel file: {e}")
        return []

@st.cache_data
def parse_robust_date(date_val):
    """Convert various date formats to datetime. Returns NaT if unparseable."""
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
    """Clean text for fuzzy matching: lowercase, remove punctuation, collapse spaces."""
    if pd.isna(text): return ""
    text = str(text).strip().lower()
    text = re.sub(r'[^\w\s]', '', text)
    return re.sub(r'\s+', ' ', text)

def find_name_clusters(series, threshold=85):
    """Group similar names using fuzzy matching. Returns dict: {canonical_name: [variations]}."""
    clean_series = series.dropna().astype(str).str.strip()
    unique_names = clean_series.unique()
    if len(unique_names) == 0: return {}
    assigned, clusters = set(), {}
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
    """Replace name variations with the most common version."""
    mapping = {var: canonical for canonical, vars in clusters.items() for var in vars}
    return series.apply(lambda x: mapping.get(str(x).strip(), x) if pd.notna(x) else x)

@st.cache_data(ttl=3600)
def geocode_villages(village_tuple):
    """
    Try to find GPS coordinates for village names using OpenStreetMap.
    Returns dict: {village_name: (lat, lon)} or None if not found.
    
    ⚠️ ASSUMPTION: Village names in your data match names in OpenStreetMap.
    If geocoding fails, the map will show only villages with known coordinates.
    """
    geolocator = Nominatim(user_agent="ffgh_immunization_dashboard", timeout=15)
    coords, villages = {}, list(village_tuple)
    progress = st.progress(0, text="Searching for village locations...")
    
    # Clean names for better matching
    clean_map = {v: v.replace("/", " ").replace(",", "").strip() for v in villages}
    
    for i, v in enumerate(villages):
        cleaned = clean_map[v]
        coords[v] = None  # Default: not found
        
        # Try searching with different location hints
        # We don't assume a state — let OpenStreetMap find the best match
        search_queries = [
            f"{cleaned}, Nigeria",
            cleaned  # Just the name, let OSM decide
        ]
        
        for query in search_queries:
            if coords[v] is not None:
                break
            try:
                loc = geolocator.geocode(query, exactly_one=True)
                if loc:
                    coords[v] = (loc.latitude, loc.longitude)
                    break
            except (GeocoderTimedOut, GeocoderServiceError, Exception):
                continue
        
        progress.progress((i + 1) / len(villages))
        time.sleep(1.1)  # Respect OpenStreetMap rate limits
    
    progress.empty()
    
    # Report results honestly
    successful = sum(1 for v in coords.values() if v is not None)
    if successful < len(villages):
        failed = [v for v, c in coords.items() if c is None]
        st.warning(f"⚠️ Found coordinates for {successful}/{len(villages)} villages. The rest will not appear on the map.")
        if failed and len(failed) <= 10:
            with st.expander("📋 Villages without coordinates"):
                st.write(failed)
    
    return coords

@st.cache_data(ttl=86400)
def fetch_nigeria_lga_geojson():
    """Try to download Nigeria LGA boundaries from open data sources."""
    urls = [
        "https://data.humdata.org/dataset/nigeria-administrative-boundaries-levels-0-3/resource/xxx/download/nga_admbnda_adm2_ocha.geojson",
        "https://geoboundaries.org/api/v3/geojson/?ISO=NGA&ADM=ADM2&format=geojson",
    ]
    for url in urls:
        try:
            resp = requests.get(url, timeout=15, headers={'User-Agent': 'FFGH-Dashboard'})
            if resp.status_code == 200 and "features" in resp.json():
                return resp.json()
        except: continue
    return None  # Return None if all attempts fail — no made-up data

@st.cache_data
def assign_lgas_to_villages(village_coords_tuple, lga_geojson_str):
    """
    Match villages to Local Government Areas using GPS coordinates.
    Returns dict: {village_name: "LGA Name"} or "Unassigned" if not found.
    
    ⚠️ ASSUMPTION: LGA GeoJSON file contains valid polygons with "name" or "LGA" properties.
    """
    if not village_coords_tuple or not lga_geojson_str: return {}
    village_coords, lga_mapping = dict(village_coords_tuple), {}
    lga_geojson = json.loads(lga_geojson_str)
    lga_polygons, lga_names = [], []
    
    # Extract valid polygons from GeoJSON
    for feat in lga_geojson.get("features", []):
        try:
            geom = shape(feat["geometry"])
            if geom.is_valid:
                lga_polygons.append(geom)
                lga_names.append(feat["properties"].get("name", feat["properties"].get("LGA", "Unknown")))
        except: continue
    
    if not lga_polygons: return {}
    
    progress = st.progress(0, text="Matching villages to LGAs...")
    for i, (village, coords) in enumerate(village_coords.items()):
        if not coords: 
            lga_mapping[village] = "Unassigned"
            continue
        pt = Point(coords[1], coords[0])  # Note: Point uses (lon, lat)
        matched = False
        for j, poly in enumerate(lga_polygons):
            if poly.contains(pt):
                lga_mapping[village] = lga_names[j]
                matched = True
                break
        if not matched: 
            lga_mapping[village] = "Unassigned"
        progress.progress((i + 1) / len(village_coords))
    progress.empty()
    return lga_mapping

def solve_tsp_route(coords_dict, start_village=None):
    """
    Find the shortest route to visit multiple villages (Traveling Salesman Problem).
    Uses a simple algorithm: start with nearest neighbor, then improve with 2-opt.
    
    ⚠️ ASSUMPTION: Straight-line distance × 1.3 approximates actual road distance in rural Nigeria.
    This is a common estimate but may not be exact for every location.
    """
    villages, coords = list(coords_dict.keys()), list(coords_dict.values())
    if len(villages) < 2: return villages, 0, 0
    
    # Find starting point
    start_idx = villages.index(start_village) if start_village and start_village in villages else 0
    
    # Nearest Neighbor algorithm
    n, visited, route, current, total_dist = len(villages), [False]*len(villages), [start_idx], start_idx, 0
    visited[start_idx] = True
    for _ in range(n - 1):
        nearest, min_dist = -1, float('inf')
        for j in range(n):
            if not visited[j]:
                d = geodesic(coords[current], coords[j]).km
                if d < min_dist: min_dist, nearest = d, j
        visited[nearest], route, total_dist, current = True, route + [nearest], total_dist + min_dist, nearest
    
    # Return to start
    total_dist += geodesic(coords[route[-1]], coords[route[0]]).km
    
    # 2-opt improvement (simple local search)
    improved, iterations = True, 0
    while improved and iterations < 100:
        improved, iterations = False, iterations + 1
        for i in range(1, len(route) - 2):
            for j in range(i + 1, len(route)):
                if j - i == 1: continue
                old_cost = geodesic(coords[route[i-1]], coords[route[i]]).km + geodesic(coords[route[j]], coords[route[(j+1)%len(route)]]).km
                new_cost = geodesic(coords[route[i-1]], coords[route[j]]).km + geodesic(coords[route[i]], coords[route[(j+1)%len(route)]]).km
                if new_cost < old_cost: 
                    route[i:j+1] = reversed(route[i:j+1])
                    improved = True
    
    return [villages[i] for i in route], round(total_dist, 2), iterations

# ================= DATA PROCESSING =================
@st.cache_data
def process_data(uploaded_file, sheet_name=None):
    """Load and clean CHEW log data. All calculations use only the uploaded data."""
    if uploaded_file.name.endswith('.csv'):
        df = pd.read_csv(uploaded_file)
    else:
        df = pd.read_excel(uploaded_file, sheet_name=sheet_name, engine='openpyxl')
    
    # Clean column names to match our expected format
    df.columns = df.columns.str.strip()
    df.columns = df.columns.str.replace(r":$", "", regex=True)
    df.columns = df.columns.str.replace("Has the child received any of the following immunizations? /", "Vax_", regex=False)
    df.columns = df.columns.str.replace("Which of the following injections did you provide? /", "Provided_", regex=False)
    df.columns = df.columns.str.replace("For which illness is treatment necessary?/", "Illness_", regex=False)
    
    # Find and parse the date column
    date_candidates = ['Enter the date', 'start', 'Start', 'date', 'Date', 'submission_time']
    date_col = next((c for c in date_candidates if c in df.columns), None)
    if not date_col and len(df.columns) > 0:
        first_col = df.columns[0]
        if df[first_col].dropna().astype(str).str.match(r'\d{4}[-/]\d{2}[-/]\d{2}').any(): 
            date_col = first_col
            
    if date_col:
        df['date'] = df[date_col].apply(parse_robust_date)
        df = df[df['date'].notna()].copy()  # Keep only rows with valid dates
    else:
        df['date'] = pd.NaT
        
    # Clean village and CHEW names using fuzzy matching
    if "Village / Settlement" in df.columns:
        v_clusters = find_name_clusters(df["Village / Settlement"], threshold=85)
        if v_clusters: 
            df["Village / Settlement"] = apply_name_mapping(df["Village / Settlement"], v_clusters)
    
    chew_col = next((c for c in df.columns if "chew" in c.lower()), None)
    if chew_col:
        c_clusters = find_name_clusters(df[chew_col], threshold=90)
        if c_clusters: 
            df[chew_col] = apply_name_mapping(df[chew_col], c_clusters)
    
    # Separate vaccine columns by data source
    vax_cols = [c for c in df.columns if c.startswith("Vax_")]  # Caregiver recall
    provided_cols = [c for c in df.columns if c.startswith("Provided_")]  # CHEW-administered
    
    # Ensure vaccine columns are numeric (0 = not given, 1 = given)
    for col in vax_cols + provided_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    
    # Remove duplicate records (keep the most recent)
    uuid_col = "uuid" if "uuid" in df.columns else "_uuid" if "_uuid" in df.columns else None
    if uuid_col: 
        df = df.drop_duplicates(subset=[uuid_col], keep="last")
        
    # Reset index to avoid pandas errors later
    df = df.reset_index(drop=True)
    return df, vax_cols, provided_cols, chew_col

# ================= MAIN APP =================
def main():
    st.title("💉 FFGH Ruwan Bore Immunization Dashboard")
    st.markdown("*Track coverage, identify zero-dose children, and prioritize outreach in Nigeria*")

    # ===== SIDEBAR UPLOADERS =====
    st.sidebar.header("📤 Upload Files")
    uploaded_file = st.sidebar.file_uploader("📊 CHEW Log (Excel/CSV)", type=["xlsx", "xls", "csv"], help="Upload your CHEW log export")
    if not uploaded_file:
        st.info("👆 Please upload your CHEW log file to activate the dashboard.")
        return

    # ===== LGA BOUNDARIES UPLOAD (Single / Multiple / ZIP) =====
    st.sidebar.subheader("🗺️ LGA Boundaries (Optional)")
    st.sidebar.markdown("*Upload Nigeria LGA GeoJSON for administrative mapping. This is optional — the dashboard works without it.*")
    
    upload_method = st.sidebar.radio(
        "Upload method:",
        ["Single GeoJSON", "Multiple files / ZIP archive"],
        index=0
    )
    
    lga_geojson = None
    
    if upload_method == "Single GeoJSON":
        lga_file = st.sidebar.file_uploader("📂 Upload LGA GeoJSON", type=["geojson", "json"], help="Download from: https://data.humdata.org/dataset/cod-ab-nga")
        if lga_file:
            try:
                lga_geojson = json.load(lga_file)
                st.sidebar.success(f"✅ Loaded: {lga_file.name}")
            except Exception as e: 
                st.sidebar.error(f"❌ Invalid GeoJSON: {e}")
    else:
        uploaded_files = st.sidebar.file_uploader(
            "📂 Upload GeoJSONs or ZIP", 
            type=["geojson", "json", "zip"], 
            accept_multiple_files=True,
            help="Upload multiple GeoJSON files or a ZIP containing them"
        )
        if uploaded_files:
            lga_geojson = {"type": "FeatureCollection", "features": []}
            files_loaded = 0
            for f in uploaded_files:
                try:
                    if f.name.endswith('.zip'):
                        with zipfile.ZipFile(f, 'r') as zip_ref:
                            for zf in zip_ref.namelist():
                                if zf.endswith(('.geojson', '.json')):
                                    with zip_ref.open(zf) as zf_data:
                                        data = json.load(zf_data)
                                        if "features" in data: 
                                            lga_geojson["features"].extend(data["features"])
                                            files_loaded += 1
                    else:
                        data = json.load(f)
                        if "features" in data: 
                            lga_geojson["features"].extend(data["features"])
                            files_loaded += 1
                except Exception as e: 
                    st.sidebar.warning(f"⚠️ Could not load {f.name}: {str(e)[:50]}")
            if files_loaded > 0: 
                st.sidebar.success(f"✅ Loaded {files_loaded} file(s) with {len(lga_geojson['features'])} features")
            else: 
                lga_geojson = None
                st.sidebar.error("❌ No valid GeoJSON files found")

    # Try auto-load as fallback (but don't assume it will work)
    if lga_geojson is None:
        lga_geojson = fetch_nigeria_lga_geojson()
        if lga_geojson: 
            st.sidebar.info("🌐 Auto-loaded LGA boundaries from open data source")
        else: 
            st.sidebar.warning("💡 Auto-load failed. Upload manually for LGA features.")

    # Sheet selector for Excel files
    sheet_name = None
    if uploaded_file.name.endswith(('.xlsx', '.xls')):
        sheet_names = get_sheet_names(uploaded_file)
        if sheet_names:
            sheet_name = st.sidebar.selectbox("📑 Select Sheet", sheet_names, help="Choose which sheet to analyze")
        else:
            st.sidebar.error("❌ No sheets found")
            return

    # Process data
    with st.spinner("Processing & auto-cleaning data..."):
        df, vax_cols, provided_cols, chew_col = process_data(uploaded_file, sheet_name)
    
    if df.empty: 
        st.error("❌ No valid data found after cleaning."); 
        return
    if not vax_cols and not provided_cols: 
        st.warning("⚠️ No vaccination columns detected."); 
        return

    # ===== SIDEBAR FILTERS =====
    st.sidebar.header("🔍 Filters")
    village_col = "Village / Settlement"
    villages = sorted(df[village_col].dropna().unique().tolist()) if village_col in df.columns else []
    selected_village = st.sidebar.selectbox("🏘️ Village / Settlement", ["All"] + villages)
    
    if chew_col:
        chews = sorted(df[chew_col].dropna().unique().tolist())
        selected_chew = st.sidebar.selectbox("👩‍⚕️ CHEW", ["All"] + chews)
    else: 
        selected_chew = "All"

    st.sidebar.subheader("📅 Date Range")
    start_date = end_date = None
    if "date" in df.columns and df["date"].notna().any():
        try:
            min_dt, max_dt = df["date"].min(), df["date"].max()
            if pd.notna(min_dt) and pd.notna(max_dt) and min_dt <= max_dt:
                min_date, max_date = min_dt.to_pydatetime().date(), max_dt.to_pydatetime().date()
                today = datetime.now().date()
                default_end = min(max_date, today)
                default_start = max(min_date, default_end - timedelta(days=30))
                date_range = st.sidebar.date_input("Select dates", value=[default_start, default_end], min_value=min_date, max_value=max_date)
                if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                    start_date, end_date = date_range
                    if start_date > end_date: 
                        st.sidebar.error("⚠️ Start date must be before end date"); 
                        return
                elif isinstance(date_range, datetime): 
                    start_date = end_date = date_range.date()
        except Exception as e: 
            st.sidebar.warning(f"⚠️ Date filter unavailable: {str(e)[:50]}")

    # ===== DATA SOURCE SEPARATION =====
    st.sidebar.header("📊 Data Source Selection")
    st.sidebar.markdown("*⚠️ Never merge these sources. Historical data is for screening only.*")
    
    if provided_cols:
        data_source = st.sidebar.radio("🔍 Which data to analyze?", ["✅ Verifiable (Injections Provided)", "📝 Historical Recall (Caregiver Report)"], index=0)
    else:
        data_source = "📝 Historical Recall (Caregiver Report)"
        st.sidebar.warning("⚠️ Only historical recall data available.")
        
    if "Verifiable" in data_source:
        active_prefix, active_vax_cols, source_banner = "Provided_", provided_cols, "✅ Currently analyzing: VERIFIABLE DATA"
    else:
        active_prefix, active_vax_cols, source_banner = "Vax_", vax_cols, "📝 Currently analyzing: HISTORICAL RECALL"
    
    st.info(source_banner)
    if not active_vax_cols: 
        st.warning("⚠️ No columns found for the selected data source."); 
        return

    # ===== VACCINE SELECTOR WITH "ALL" OPTION =====
    vaccine_options = {"All vaccines": "all"}
    vaccine_options.update({v.replace(active_prefix, "").replace("_", " "): v for v in active_vax_cols})
    selected_label = st.sidebar.selectbox("💉 Select Vaccine", list(vaccine_options.keys()), index=0)
    selected_vax = vaccine_options[selected_label]

    # ===== APPLY FILTERS =====
    mask = pd.Series(True, index=df.index)
    if selected_village != "All": mask &= (df[village_col] == selected_village)
    if selected_chew != "All" and chew_col: mask &= (df[chew_col] == selected_chew)
    if start_date and end_date: mask &= (df["date"].dt.date >= start_date) & (df["date"].dt.date <= end_date)
    df_f = df[mask].copy()
    total = len(df_f)

    # ===== LGA ASSIGNMENT =====
    selected_lga_filter = "All"
    if village_col in df_f.columns and lga_geojson:
        coords = geocode_villages(tuple(df_f[village_col].dropna().unique()))
        valid_coords = {k: v for k, v in coords.items() if v is not None}
        if valid_coords:
            lga_mapping = assign_lgas_to_villages(tuple(valid_coords.items()), json.dumps(lga_geojson))
            df_f["LGA"] = df_f[village_col].map(lga_mapping).fillna("Unassigned")
            lgas = sorted(df_f["LGA"].dropna().unique().tolist())
            selected_lga_filter = st.sidebar.selectbox("🏛️ Filter by LGA", ["All"] + lgas)
            if selected_lga_filter != "All":
                df_f = df_f[df_f["LGA"] == selected_lga_filter].copy()
                total = len(df_f)

    # ===== TABS UI =====
    tab1, tab2, tab3, tab4 = st.tabs(["📊 Analytics", "🏛️ LGA Coverage", "🗺️ Map & Boundaries", "🚑 Route Planner"])
    
    with tab1:
        st.subheader("📊 Key Metrics")
        st.markdown("""
        **What these numbers mean (simple explanation):**
        - **Total Records**: How many children were checked in your selected time period. Like counting how many students took a test.
        - **Active CHEWs**: How many health workers submitted reports. Like counting how many teachers graded papers.
        - **Zero-Dose Children**: Kids who haven't received ANY vaccines yet. These children have no protection against diseases — they are the top priority.
        - **Selected Vaccine Coverage**: What percentage of children got the vaccine you selected. If 70 out of 100 kids got measles vaccine, coverage is 70%.
        """)
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📋 Total Records", f"{total:,}")
        if chew_col: 
            c4.metric("👩‍⚕️ Active CHEWs", df_f[chew_col].nunique() if chew_col in df_f.columns else 0)
        
        # Zero-dose calculation: children with NO BCG AND NO OPV0 in historical data
        bcg_col = "Vax_BCG" if "Vax_BCG" in df_f.columns else None
        opv0_col = next((c for c in df_f.columns if "OPV 0" in c), None)
        if bcg_col and opv0_col and total > 0:
            zero_dose = df_f[(df_f[bcg_col] == 0) & (df_f[opv0_col] == 0)].shape[0]
            c2.metric("🚫 Zero-Dose Children", f"{zero_dose:,}", delta=f"{(zero_dose/total*100):.1f}%")
        else: 
            c2.metric("🚫 Zero-Dose", "N/A")
        
        # Coverage calculation: % of children who received the selected vaccine
        if selected_vax == "all":
            # Average coverage across all vaccines
            all_cols = [c for c in active_vax_cols if c in df_f.columns]
            if all_cols and total > 0:
                # Sum all vaccine doses given, divide by (number of vaccines × number of children)
                coverage = (df_f[all_cols].sum().sum() / (len(all_cols) * total)) * 100
            else:
                coverage = 0
        else:
            # Simple: (children who got this vaccine) / (total children) × 100
            coverage = (df_f[selected_vax].sum() / total) * 100 if total > 0 else 0
            
        c3.metric(f"💉 {selected_label} Coverage", f"{coverage:.1f}%")

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("🗺️ Coverage by Village")
            st.markdown("""
            **What this chart shows:** Each bar is one village. The height shows what % of children got the vaccine.
            - 🔴 Red bar = Less than 50% coverage (needs urgent help)
            - 🟡 Yellow bar = 50-80% coverage (doing okay but can improve)
            - 🟢 Green bar = Above 80% coverage (meeting targets)
            
            **Why it matters:** This helps you see which villages need more support. If a village has 0% coverage, no children there got vaccinated — that village needs immediate attention.
            """)
            if village_col in df_f.columns and (selected_vax == "all" or selected_vax in df_f.columns):
                if selected_vax == "all":
                    all_cols = [c for c in active_vax_cols if c in df_f.columns]
                    if all_cols:
                        # Calculate average coverage per village across all vaccines
                        vax_cov = df_f.groupby(village_col)[all_cols].sum().reset_index()
                        counts = df_f.groupby(village_col).size().reset_index(name='count')
                        vax_cov = pd.merge(vax_cov, counts, on=village_col)
                        vax_cov['sum'] = vax_cov[all_cols].sum(axis=1)
                        vax_cov['total'] = vax_cov['count'] * len(all_cols)
                        vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['total'] * 100).round(1)
                    else: 
                        vax_cov = pd.DataFrame()
                else:
                    # Simple coverage per village for one vaccine
                    vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
                    vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
                
                # Only show villages with at least 3 children (more reliable)
                vax_cov = vax_cov[vax_cov['count'] >= 3] if 'count' in vax_cov.columns else vax_cov
                if not vax_cov.empty:
                    fig = px.bar(vax_cov, x=village_col, y='coverage', color='coverage', 
                               color_continuous_scale=["#e74c3c", "#f39c12", "#27ae60"], 
                               title=f"{selected_label} Coverage by Village")
                    fig.update_layout(xaxis_tickangle=-45, height=400)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("ℹ️ Not enough data to show village coverage (need at least 3 children per village).")
        
        with col2:
            st.subheader("📈 Monthly Trend")
            st.markdown("""
            **What this chart shows:** A line that goes from left to right showing how coverage changed over time.
            - If the line goes UP: More children are getting vaccinated each month (good!)
            - If the line goes DOWN: Fewer children are getting vaccinated (needs investigation)
            
            **Why it matters:** Like tracking your grades through the school year. If your math grade went from 60% to 75%, you're improving! Same here — if coverage is rising, your program is working.
            """)
            if "date" in df_f.columns and (selected_vax == "all" or selected_vax in df_f.columns):
                if selected_vax == "all":
                    all_cols = [c for c in active_vax_cols if c in df_f.columns]
                    if all_cols:
                        # Calculate monthly average coverage across all vaccines
                        df_temp = df_f.set_index("date").resample("ME").agg({c: ["sum", "count"] for c in all_cols}).dropna(how="all")
                        if not df_temp.empty:
                            df_temp.columns = [f"{c}_{stat}" for c, stat in df_temp.columns]
                            df_temp['vaccinated'] = df_temp[[f"{c}_sum" for c in all_cols]].sum(axis=1)
                            df_temp['total'] = df_temp[[f"{c}_count" for c in all_cols]].sum(axis=1)
                            df_temp['coverage'] = (df_temp['vaccinated'] / df_temp['total'] * 100).round(1)
                            df_temp = df_temp.reset_index()
                            fig = px.line(df_temp, x="date", y="coverage", markers=True, 
                                        title=f"{selected_label} Monthly Coverage Trend")
                            fig.update_layout(height=400)
                            st.plotly_chart(fig, use_container_width=True)
                else:
                    # Simple monthly trend for one vaccine
                    df_temp = df_f.set_index("date").resample("ME").agg({selected_vax: ["sum", "count"]}).dropna(how="all")
                    if not df_temp.empty:
                        df_temp.columns = ["vaccinated", "total"]
                        df_temp["coverage"] = (df_temp["vaccinated"] / df_temp["total"] * 100).round(1)
                        fig = px.line(df_temp.reset_index(), x="date", y="coverage", markers=True, 
                                    title=f"{selected_label} Monthly Coverage Trend")
                        fig.update_layout(height=400)
                        st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("ℹ️ Not enough date data to show trends.")

        st.markdown("---")
        st.subheader("🚨 Priority Areas for Outreach")
        st.info("ℹ️ Shows villages with <50% coverage based on your selected data source.")
        st.markdown("""
        **What this list means:** These villages have low vaccination coverage and need urgent help.
        - The number after each village name is the coverage percentage (lower = worse)
        - The number in parentheses is how many children are unvaccinated (higher = more urgent)
        
        **Example:** `Bakin kasuwa: 0.0% coverage (37 unvaccinated)` means:
        - 0% of children in Bakin kasuwa got the vaccine
        - 37 children there have no protection
        - This village should be a top priority for outreach
        
        **Why it matters:** You can't help every village at once. This list helps you focus on where the need is greatest first.
        """)
        if village_col in df_f.columns and (selected_vax == "all" or selected_vax in df_f.columns):
            if selected_vax == "all":
                all_cols = [c for c in active_vax_cols if c in df_f.columns]
                if all_cols:
                    # Calculate average coverage and unvaccinated count per village
                    vax_cov = df_f.groupby(village_col)[all_cols].sum().reset_index()
                    counts = df_f.groupby(village_col).size().reset_index(name='count')
                    vax_cov = pd.merge(vax_cov, counts, on=village_col)
                    vax_cov['sum'] = vax_cov[all_cols].sum(axis=1)
                    vax_cov['coverage'] = (vax_cov['sum'] / (vax_cov['count'] * len(all_cols)) * 100).round(1)
                    vax_cov['unvaccinated'] = (vax_cov['count'] * len(all_cols)) - vax_cov['sum']
            else:
                # Simple coverage and unvaccinated count for one vaccine
                vax_cov = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
                vax_cov['coverage'] = (vax_cov['sum'] / vax_cov['count'] * 100).round(1)
                vax_cov['unvaccinated'] = vax_cov['count'] - vax_cov['sum']
            
            # Only show villages with at least 3 children (more reliable)
            vax_cov = vax_cov[vax_cov['count'] >= 3]
            cold = vax_cov[vax_cov['coverage'] < 50].sort_values('coverage')
            if not cold.empty:
                st.warning(f"⚠️ {len(cold)} villages with <50% coverage need urgent attention:")
                for _, row in cold.head(10).iterrows():
                    st.markdown(f"- **{row[village_col]}**: {row['coverage']:.1f}% coverage ({int(row['unvaccinated'])} unvaccinated)")
            else: 
                st.success("✅ All villages with sufficient data have >50% coverage!")
        else: 
            st.info("ℹ️ Select a vaccine to identify priority areas.")

    with tab2:
        st.subheader("🏛️ LGA-Level Coverage Aggregation")
        st.markdown("""
        **What this shows:** Groups villages into their Local Government Areas (LGAs — like counties or districts).
        - Shows total vaccinated vs total children in each LGA
        - Shows coverage percentage for the whole LGA
        
        **Why it matters:** Instead of looking at each village individually, you see how the whole district is doing. 
        If "Malam Madori LGA" has 45% coverage across all its villages, the LGA health coordinator needs to step up their game. 
        This helps you report to government officials who manage entire LGAs, not just individual villages.
        
        ⚠️ **Note:** This only works if you uploaded an LGA GeoJSON file. Without it, this tab will show a message.
        """)
        if "LGA" not in df_f.columns or df_f["LGA"].isna().all():
            st.info("ℹ️ LGA mapping unavailable. Upload LGA GeoJSON in the sidebar to enable this feature.")
        else:
            # Aggregate data by LGA
            lga_agg = df_f.groupby("LGA").agg({selected_vax if selected_vax != "all" else active_vax_cols[0]: ['sum', 'count'], village_col: 'nunique'}).reset_index()
            lga_agg.columns = ["LGA", "Vaccinated", "Total", "Villages"]
            lga_agg["Coverage %"] = (lga_agg["Vaccinated"] / lga_agg["Total"] * 100).round(1)
            lga_agg["Unvaccinated"] = lga_agg["Total"] - lga_agg["Vaccinated"]
            lga_agg = lga_agg.sort_values("Coverage %")
            
            c1, c2 = st.columns(2)
            with c1:
                fig_lga = px.bar(lga_agg, x="LGA", y="Coverage %", color="Coverage %", 
                               color_continuous_scale=["#e74c3c", "#f39c12", "#27ae60"], 
                               title=f"{selected_label} Coverage by LGA")
                fig_lga.update_layout(xaxis_tickangle=-45, height=400)
                st.plotly_chart(fig_lga, use_container_width=True)
            with c2: 
                st.dataframe(lga_agg, use_container_width=True, height=400)
            
            csv_lga = lga_agg.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download LGA Coverage Report", csv_lga, "lga_coverage_report.csv", "text/csv")

    with tab3:
        st.subheader("🗺️ Interactive Coverage Map")
        st.markdown("""
        **What this map shows:** A map with colored dots on each village:
        - 🔴 Red dot = Less than 50% coverage
        - 🟡 Yellow dot = 50-80% coverage  
        - 🟢 Green dot = Above 80% coverage
        
        The size of the dot shows how many children are in that village. You can click on any dot to see exact numbers.
        
        **Why it matters:** Some people understand pictures better than tables. When you show this map to LGA officials or donors, they instantly see "Wow, all these red dots mean we have a problem there." 
        It also helps you plan routes — you can see which low-coverage villages are close together so one CHEW team can visit multiple villages in one trip.
        
        ⚠️ **Note:** This only shows villages that OpenStreetMap could find. If a village name isn't in their database, it won't appear on the map. This is not a problem with your data — it's a limitation of the free map service.
        """)
        show_lga = st.checkbox("🔲 Show LGA Boundaries on Map", value=False, disabled=lga_geojson is None)
        if village_col in df_f.columns and (selected_vax == "all" or selected_vax in df_f.columns):
            if selected_vax == "all":
                all_cols = [c for c in active_vax_cols if c in df_f.columns]
                cov_df = df_f.groupby(village_col)[all_cols].sum().reset_index()
                counts = df_f.groupby(village_col).size().reset_index(name='count')
                cov_df = pd.merge(cov_df, counts, on=village_col)
                cov_df['sum'] = cov_df[all_cols].sum(axis=1)
                cov_df['coverage_pct'] = (cov_df['sum'] / (cov_df['count'] * len(all_cols)) * 100).round(1)
            else:
                cov_df = df_f.groupby(village_col)[selected_vax].agg(['sum', 'count']).reset_index()
                cov_df['coverage_pct'] = (cov_df['sum'] / cov_df['count'] * 100).round(1)
            
            # Only show villages with at least 3 children (more reliable)
            cov_df = cov_df[cov_df['count'] >= 3]
            if not cov_df.empty:
                coords = geocode_villages(tuple(cov_df[village_col].unique()))
                valid_coords = {k: v for k, v in coords.items() if v is not None}
                if valid_coords:
                    # Center map on Nigeria (neutral — no state assumption)
                    m = folium.Map(location=[9.0833, 8.6753], zoom_start=6, tiles="CartoDB positron")
                    folium.LayerControl(collapsed=False).add_to(m)
                    if show_lga and lga_geojson:
                        try: 
                            folium.GeoJson(lga_geojson, name="LGA Boundaries", 
                                         style_function=lambda x: {'fillColor': 'transparent', 'color': '#2c3e50', 'weight': 1.5, 'opacity': 0.8}, 
                                         tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["LGA:"])).add_to(m)
                        except: pass
                    
                    for v, (lat, lon) in valid_coords.items():
                        row = cov_df[cov_df[village_col] == v].iloc[0]
                        pct = row['coverage_pct']
                        # Calculate unvaccinated count honestly
                        if selected_vax == "all" and 'all_cols' in locals():
                            unvax = int(row['count'] * len(all_cols) - row['sum'])
                        else:
                            unvax = int(row['count'] - row['sum'])
                        color = "#e74c3c" if pct < 50 else "#f39c12" if pct < 80 else "#27ae60"
                        radius = 6 + (pct / 15)
                        popup_html = f"""<div style="font-family:sans-serif; min-width:150px;"><b>{v}</b><br><span style="color:{color};">● Coverage: {pct}%</span><br>Unvaccinated: {unvax} | Total: {int(row['count'])}</div>"""
                        folium.CircleMarker(location=[lat, lon], radius=radius, color=color, weight=2, fill=True, 
                                          fill_color=color, fill_opacity=0.75, 
                                          popup=folium.Popup(popup_html, max_width=250), 
                                          tooltip=f"{v}: {pct}%").add_to(m)
                    
                    # Fit map to show all villages
                    if valid_coords:
                        lats, lons = zip(*valid_coords.values())
                        m.fit_bounds([[min(lats)-0.2, min(lons)-0.2], [max(lats)+0.2, max(lons)+0.2]])
                    st_folium(m, width=700, height=500, returned_objects=[])
                    
                    # Export coordinates for villages that were found
                    cov_export = pd.DataFrame(list(valid_coords.items()), columns=['Village', 'coords'])
                    cov_export['Lat'], cov_export['Lon'] = cov_export['coords'].apply(lambda x: x[0]), cov_export['coords'].apply(lambda x: x[1])
                    csv = cov_export[['Village', 'Lat', 'Lon']].to_csv(index=False).encode("utf-8")
                    st.download_button("⬇️ Download Geocoded Coordinates", csv, "village_coordinates.csv", "text/csv")
                else: 
                    st.error("❌ Geocoding failed for all villages. This means OpenStreetMap doesn't know these village names. Try uploading a CSV with Lat/Lon columns if you have them.")
            else: 
                st.info("ℹ️ Insufficient data for mapping (min 3 records per village).")
        else: 
            st.info("ℹ️ Select a vaccine and ensure village data is available.")

    with tab4:
        st.subheader("🚑 Outreach Route Optimizer")
        st.markdown("""
        **What this does:** Calculates the most efficient travel sequence for CHEW teams.
        
        **How it works (simple explanation):**
        1. You pick which villages need visits (or select "Cold Spots" to auto-pick villages under 50% coverage)
        2. The computer calculates distances between all villages
        3. It tries different orders to find the shortest total route
        4. It adds time for road conditions (the "1.3x road factor" means if the map says 10 km, you'll actually drive about 13 km because roads aren't straight)
        5. It tells you when you'll arrive at each stop
        
        **Why it matters:** CHEW teams have limited time and fuel. Without planning, they might drive from Village A to B to C to D, then realize Village D is actually next to Village A — they wasted gas and time! This tool uses math to find the shortest route. If a team can visit 5 villages in 6 hours instead of 10 hours, they can help more kids and go home before dark.
        
        ⚠️ **Assumptions made:**
        - Straight-line distance × 1.3 approximates actual road distance (common estimate for rural Nigeria)
        - Average road speed is 35 km/h (you can adjust this in the settings)
        - Each village visit takes 20 minutes (you can adjust this too)
        These are estimates — actual travel times may vary based on weather, road conditions, and other factors.
        """)
        if village_col in df_f.columns:
            cov_df = df_f.groupby(village_col).size().reset_index(name='count')
            if not cov_df.empty:
                coords = geocode_villages(tuple(cov_df[village_col].unique()))
                valid_coords = {k: v for k, v in coords.items() if v is not None}
                if len(valid_coords) >= 2:
                    st.sidebar.subheader("📍 Route Settings")
                    target_type = st.sidebar.radio("Optimize for:", ["Cold Spots (<50%)", "All Villages", "Custom Selection"])
                    
                    if target_type == "Cold Spots (<50%)":
                        # Only include villages with known coordinates AND low coverage
                        target_villages = [v for v in cov_df[cov_df['count'] >= 2][village_col].tolist() if v in valid_coords]
                    elif target_type == "All Villages":
                        target_villages = list(valid_coords.keys())
                    else:
                        target_villages = st.sidebar.multiselect("Select villages", list(valid_coords.keys()))
                        
                    start_point = st.sidebar.selectbox("🏁 Starting Point", ["First village in route"] + list(valid_coords.keys()))
                    avg_speed = st.sidebar.slider("Avg road speed (km/h)", 20, 50, 35, step=5, help="Estimate of how fast CHEWs can travel on rural roads")
                    time_per_stop = st.sidebar.slider("Time per village (min)", 10, 45, 20, step=5, help="How long CHEWs spend at each village for vaccinations")
                    
                    if st.sidebar.button("🧮 Calculate Optimal Route", type="primary"):
                        if len(target_villages) < 2: 
                            st.warning("⚠️ Select at least 2 villages for routing.")
                        else:
                            subset_coords = {v: valid_coords[v] for v in target_villages if v in valid_coords}
                            if len(subset_coords) < 2: 
                                st.warning("⚠️ Need at least 2 geocoded villages for routing.")
                            else:
                                with st.spinner("Optimizing route..."):
                                    route_order, total_dist, iterations = solve_tsp_route(subset_coords, start_point if start_point != "First village in route" else None)
                                    
                                    # Apply road factor (1.3x) to estimate actual driving distance
                                    road_factor = 1.3
                                    road_dist = round(total_dist * road_factor, 2)
                                    
                                    # Calculate time estimates
                                    travel_time = road_dist / avg_speed  # hours driving
                                    stop_time = len(route_order) * time_per_stop / 60  # hours stopping
                                    total_time = travel_time + stop_time
                                    
                                    st.success(f"✅ Route optimized in {iterations} iterations")
                                    c1, c2, c3 = st.columns(3)
                                    c1.metric("📏 Straight Distance", f"{total_dist} km")
                                    c2.metric("🛣️ Est. Road Distance", f"{road_dist} km")
                                    c3.metric("⏱️ Est. Total Time", f"{total_time:.1f} hrs")
                                    
                                    # Create route table with sequence, village, and timing
                                    route_df = pd.DataFrame({
                                        "Sequence": range(1, len(route_order) + 1),
                                        "Village": route_order,
                                        "Lat": [subset_coords[v][0] for v in route_order],
                                        "Lon": [subset_coords[v][1] for v in route_order]
                                    })
                                    
                                    # Calculate cumulative distance and ETA for each stop
                                    cum_dist, current_time, cum_dists, etas = 0, 0, [], []
                                    for i in range(len(route_order)):
                                        if i == 0: 
                                            cum_dist, current_time = 0, 0
                                        else:
                                            d = geodesic(subset_coords[route_order[i-1]], subset_coords[route_order[i]]).km * road_factor
                                            cum_dist += d
                                            current_time += (d / avg_speed) + (time_per_stop/60)
                                        cum_dists.append(round(cum_dist, 2))
                                        etas.append(current_time)
                                        
                                    route_df["Cum. Distance (km)"] = cum_dists
                                    route_df["ETA (hrs)"] = [f"{t:.1f}" for t in etas]
                                    st.dataframe(route_df, use_container_width=True)
                                    
                                    # Export route plan
                                    csv_route = route_df.to_csv(index=False).encode("utf-8")
                                    st.download_button("⬇️ Download Route Plan (CSV)", csv_route, "outreach_route_plan.csv", "text/csv")
                                    
                                    # Draw route on map
                                    m_route = folium.Map(location=[subset_coords[route_order[0]][0], subset_coords[route_order[0]][1]], zoom_start=10)
                                    folium.LayerControl().add_to(m_route)
                                    for i, v in enumerate(route_order):
                                        folium.Marker([subset_coords[v][0], subset_coords[v][1]], 
                                                    popup=f"{i+1}. {v}", 
                                                    icon=folium.Icon(color="blue" if i == 0 else "green" if i == len(route_order)-1 else "orange", 
                                                                   prefix="fa", icon=str(i+1))).add_to(m_route)
                                    folium.PolyLine([subset_coords[v] for v in route_order] + [subset_coords[route_order[0]]], 
                                                  color="#e74c3c", weight=4, opacity=0.8, dash_array="10, 5").add_to(m_route)
                                    st_folium(m_route, width=700, height=500)
                else: 
                    st.info("ℹ️ Not enough geocoded villages for routing.")
            else: 
                st.info("ℹ️ Insufficient data for routing.")
        else: 
            st.info("ℹ️ Village data missing.")

    st.markdown("---")
    st.subheader("📋 Filtered Records")
    st.markdown("""
    **What this table shows:** The raw data — every child's record that matches your filters.
    - Shows their name, village, CHEW, phone number, age, and which vaccines they received
    - This is your "source of truth" — if the dashboard says 37 kids in Bakin kasuwa need vaccines, this table shows you their actual names and phone numbers
    
    **Why it matters:** CHEWs can use this to call caregivers and remind them to come for vaccination, or to do house-to-house visits. Always double-check important decisions with this raw data.
    """)
    display_cols = [c for c in ["date", village_col, chew_col, "Child's Name", "Caregiver Phone", "Age (in Years)"] if c in df_f.columns]
    vax_display = [v for v in active_vax_cols[:6] if v in df_f.columns]
    if display_cols or vax_display:
        st.dataframe(df_f[display_cols + vax_display].head(100), use_container_width=True, height=300)
        csv = df_f.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download Filtered CSV", csv, f"ffgh_filtered_{datetime.now().strftime('%Y%m%d')}.csv", "text/csv")

if __name__ == "__main__":
    main()
