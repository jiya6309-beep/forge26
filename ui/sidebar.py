"""
Sidebar tools: add a shipment, import a CSV, reset the demo.
"""
import pandas as pd
import streamlit as st

from db import queries as q

INDIA_LAT = (6.0, 37.5)
INDIA_LON = (68.0, 98.0)
CSV_COLUMNS = {"origin", "sku", "qty", "dest_name", "dest_lat", "dest_lon"}


def validate(sku, qty, dest, lat, lon):
    """Returns a readable problem, or None if the row is fine."""
    if not str(sku).strip():
        return "SKU is empty"
    try:
        if int(qty) <= 0:
            return "quantity must be positive"
    except (TypeError, ValueError):
        return f"quantity '{qty}' is not a number"
    if not str(dest).strip():
        return "destination name is empty"
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return "latitude/longitude must be numbers"
    if not (INDIA_LAT[0] <= lat <= INDIA_LAT[1] and INDIA_LON[0] <= lon <= INDIA_LON[1]):
        return f"location ({lat}, {lon}) is outside India"
    return None


def import_csv(file, warehouses):
    try:
        df = pd.read_csv(file)
    except pd.errors.EmptyDataError:
        return 0, ["The file is empty"]
    except Exception as e:
        return 0, [f"Could not read the CSV: {e}"]

    df.columns = df.columns.str.strip()
    missing = CSV_COLUMNS - set(df.columns)
    if missing:
        return 0, [f"Missing columns: {', '.join(sorted(missing))}"]

    warehouse_ids = {w["name"].lower(): w["id"] for w in warehouses}
    added, problems = 0, []
    for i, row in df.iterrows():
        line = i + 2  # the header is line 1
        origin_id = warehouse_ids.get(str(row["origin"]).strip().lower())
        if origin_id is None:
            problems.append(f"Line {line}: unknown warehouse '{row['origin']}'")
            continue
        problem = validate(row["sku"], row["qty"], row["dest_name"], row["dest_lat"], row["dest_lon"])
        if problem:
            problems.append(f"Line {line}: {problem}")
            continue
        q.add_shipment(origin_id, str(row["sku"]).strip(), int(row["qty"]),
                       str(row["dest_name"]).strip(), float(row["dest_lat"]), float(row["dest_lon"]))
        added += 1
    return added, problems


def render(warehouses, on_reset):
    with st.sidebar:
        st.markdown("### Add a shipment")
        with st.form("add_shipment", clear_on_submit=True):
            origin = st.selectbox("From warehouse", warehouses, format_func=lambda w: w["name"])
            sku = st.text_input("SKU", "RICE-25KG")
            qty = st.number_input("Quantity", min_value=1, value=50, step=5)
            dest = st.text_input("Destination", placeholder="Store name, city")
            left, right = st.columns(2)
            lat = left.number_input("Latitude", value=22.3072, format="%.4f")
            lon = right.number_input("Longitude", value=73.1812, format="%.4f")
            if st.form_submit_button("Add shipment", width="stretch"):
                problem = validate(sku, qty, dest, lat, lon) or (None if origin else "pick a warehouse")
                if problem:
                    st.error(problem)
                else:
                    new_id = q.add_shipment(origin["id"], sku.strip(), int(qty), dest.strip(), lat, lon)
                    st.success(f"Shipment #{new_id} added")
                    st.rerun()

        st.markdown("### Import a CSV")
        st.caption("Columns: origin, sku, qty, dest_name, dest_lat, dest_lon")
        upload = st.file_uploader("CSV file", type="csv", label_visibility="collapsed")
        if upload is not None and st.button("Import", width="stretch"):
            added, problems = import_csv(upload, warehouses)
            if added:
                st.success(f"Imported {added} shipment{'s' if added != 1 else ''}")
            for problem in problems[:10]:
                st.warning(problem)
            if len(problems) > 10:
                st.warning(f"…and {len(problems) - 10} more")

        st.divider()
        if st.button("Reset demo data", width="stretch"):
            q.reset_demo()
            on_reset()
            st.rerun()
