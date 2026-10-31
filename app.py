"""
SupplySentinel - Streamlit UI.
Run locally:  streamlit run app.py
"""
import math
import os

import folium
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from folium.plugins import Draw
from streamlit_folium import st_folium

APP_NAME = "SupplySentinel"
st.set_page_config(page_title=APP_NAME, page_icon="🛡️", layout="wide",
                   initial_sidebar_state="collapsed")

load_dotenv()
try:  # On Streamlit Cloud, keys come from the app's Secrets settings
    for key, value in st.secrets.items():
        os.environ.setdefault(key, str(value))
except Exception:
    pass

from agent.graph import run_agent  # noqa: E402
from db import queries as q        # noqa: E402

# A user-defined test scenario (one click instead of drawing during the demo)
SCENARIO_ZONE = {"type": "Polygon", "coordinates": [[
    [72.80, 22.60], [72.98, 22.60], [72.98, 22.76], [72.80, 22.76], [72.80, 22.60]]]}
INDIA = {"lat": (6.0, 37.5), "lon": (68.0, 98.0)}
STEP_ICON = {"detect": "🛰️", "assess": "🔍", "reroute": "🧭", "resource": "🏭",
             "decide": "🧠", "draft": "📝", "finish": "✅"}
SEV_COLOR = {"low": "#e9c46a", "moderate": "#f4a261", "high": "#e63946", "severe": "#9d0208"}
STATUS_COLOR = {"in_transit": "#3a86ff", "at_risk": "#e63946", "rerouted": "#2a9d8f",
                "resourced": "#8d99ae", "delayed": "#fb8500"}

ss = st.session_state
for k, v in {"zones": [], "hazards": [], "trace": [], "source_status": {},
             "summary": "", "run_error": ""}.items():
    ss.setdefault(k, v)


# ---------------- helpers ----------------

def fmt(e: dict) -> str:
    return f"{STEP_ICON.get(e['step'], '•')} **{e['step']}** · {e['msg']}"


def validate(sku, qty, dest, lat, lon) -> str | None:
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
    if not (INDIA["lat"][0] <= lat <= INDIA["lat"][1] and INDIA["lon"][0] <= lon <= INDIA["lon"][1]):
        return f"location ({lat}, {lon}) is outside India"
    return None


def import_csv(file, warehouses) -> tuple[int, list[str]]:
    try:
        df = pd.read_csv(file)
    except pd.errors.EmptyDataError:
        return 0, ["The file is empty"]
    except Exception as e:
        return 0, [f"Could not read CSV: {e}"]
    needed = {"origin", "sku", "qty", "dest_name", "dest_lat", "dest_lon"}
    missing = needed - set(df.columns.str.strip())
    if missing:
        return 0, [f"Missing columns: {', '.join(sorted(missing))}"]
    df.columns = df.columns.str.strip()
    by_name = {w["name"].lower(): w["id"] for w in warehouses}
    added, errors = 0, []
    for i, row in df.iterrows():
        line = i + 2  # header is line 1
        origin_id = by_name.get(str(row["origin"]).strip().lower())
        if origin_id is None:
            errors.append(f"Line {line}: unknown warehouse '{row['origin']}'")
            continue
        err = validate(row["sku"], row["qty"], row["dest_name"], row["dest_lat"], row["dest_lon"])
        if err:
            errors.append(f"Line {line}: {err}")
            continue
        q.add_shipment(origin_id, str(row["sku"]).strip(), int(row["qty"]),
                       str(row["dest_name"]).strip(), float(row["dest_lat"]), float(row["dest_lon"]))
        added += 1
    return added, errors


def view_for(points, width_px=600, height_px=560) -> tuple[list[float], int]:
    """Centre and zoom that fit all points. Computed here because Leaflet's
    fitBounds runs before the map has its final size and zooms out to the world."""
    if not points:
        return [22.0, 72.8], 7
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    lat_span = max(max(lats) - min(lats), 0.5) * 1.2
    lon_span = max(max(lons) - min(lons), 0.5) * 1.2
    zoom_lon = math.log2(width_px * 360 / (256 * lon_span))
    zoom_lat = math.log2(height_px * 180 / (256 * lat_span))
    zoom = int(max(4, min(10, math.floor(min(zoom_lon, zoom_lat)))))
    return [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2], zoom


def build_map(warehouses, suppliers, shipments, pos):
    center, zoom = view_for([(w["lat"], w["lon"]) for w in warehouses]
                            + [(x["lat"], x["lon"]) for x in suppliers]
                            + [(x["dest_lat"], x["dest_lon"]) for x in shipments])
    m = folium.Map(location=center, zoom_start=zoom, tiles="OpenStreetMap")

    for h in ss.hazards:
        p = h["properties"]
        color = SEV_COLOR.get(p["severity"], "#e63946") if p["source"] != "User" else "#7b2cbf"
        folium.GeoJson(h, style_function=lambda f, c=color: {
            "color": c, "fillColor": c, "fillOpacity": 0.25, "weight": 2},
            tooltip=f"{p['title']} ({p['source']}, {p['severity']})").add_to(m)

    for z in ss.zones:
        folium.GeoJson({"type": "Feature", "geometry": z, "properties": {}},
                       style_function=lambda f: {"color": "#7b2cbf", "fillColor": "#7b2cbf",
                                                 "fillOpacity": 0.12, "dashArray": "6,6"},
                       tooltip="Your disruption zone").add_to(m)

    for s in shipments:
        color = STATUS_COLOR.get(s["status"], "gray")
        tip = f"#{s['id']} {s['origin']} → {s['destination']} · {s['qty']} × {s['sku']} · {s['status']}"
        if s["route_geojson"]:
            # the blocked original path, then the safe road route the agent found
            folium.PolyLine([(s["origin_lat"], s["origin_lon"]), (s["dest_lat"], s["dest_lon"])],
                            color="#e63946", weight=1.5, dash_array="4,8", opacity=0.6).add_to(m)
            folium.GeoJson(s["route_geojson"], style_function=lambda f: {
                "color": "#2a9d8f", "weight": 5, "opacity": 0.9},
                tooltip=tip + " · safe route").add_to(m)
        else:
            folium.PolyLine([(s["origin_lat"], s["origin_lon"]), (s["dest_lat"], s["dest_lon"])],
                            color=color, weight=2, dash_array="6,6", tooltip=tip).add_to(m)
        folium.CircleMarker((s["dest_lat"], s["dest_lon"]), radius=4, color=color, fill=True,
                            tooltip=s["destination"]).add_to(m)

    for p in pos:
        folium.PolyLine([(p["sup_lat"], p["sup_lon"]), (p["dest_lat"], p["dest_lon"])],
                        color="#2a9d8f", weight=3, dash_array="2,8",
                        tooltip=f"PO-{p['id']}: {p['supplier']} → {p['dest_name']}").add_to(m)

    for w in warehouses:
        folium.Marker((w["lat"], w["lon"]), tooltip=f"Warehouse: {w['name']}",
                      icon=folium.Icon(color="blue", icon="home")).add_to(m)
    for s in suppliers:
        folium.CircleMarker((s["lat"], s["lon"]), radius=6, color="#2a9d8f", fill=True,
                            fill_opacity=0.8, tooltip=f"Supplier: {s['name']} | {s['stock']}").add_to(m)

    Draw(export=False,
         draw_options={"polyline": False, "circle": False, "marker": False,
                       "circlemarker": False, "polygon": True, "rectangle": True},
         edit_options={"edit": False}).add_to(m)
    return m, center, zoom


# ---------------- data ----------------

try:
    warehouses = q.get_warehouses()
    suppliers = q.get_suppliers()
    shipments = q.get_all_shipments()
    reroutes = q.get_pending_reroutes()
    pos = q.get_pending_pos()
except Exception as e:
    st.error(f"Database unavailable: {e}")
    st.stop()


# ---------------- sidebar ----------------

with st.sidebar:
    st.header("Add shipment")
    with st.form("add_shipment", clear_on_submit=True):
        origin = st.selectbox("From warehouse", warehouses, format_func=lambda w: w["name"])
        sku = st.text_input("SKU", "RICE-25KG")
        qty = st.number_input("Quantity", min_value=1, value=50, step=5)
        dest = st.text_input("Destination name", placeholder="e.g. Store, Vadodara")
        c1, c2 = st.columns(2)
        lat = c1.number_input("Latitude", value=22.3072, format="%.4f")
        lon = c2.number_input("Longitude", value=73.1812, format="%.4f")
        if st.form_submit_button("Add shipment", width="stretch"):
            err = validate(sku, qty, dest, lat, lon)
            if err or origin is None:
                st.error(err or "No warehouse selected")
            else:
                new_id = q.add_shipment(origin["id"], sku.strip(), int(qty), dest.strip(), lat, lon)
                st.success(f"Shipment #{new_id} added")
                st.rerun()

    st.header("Upload shipments (CSV)")
    st.caption("Columns: origin, sku, qty, dest_name, dest_lat, dest_lon")
    upload = st.file_uploader("CSV file", type="csv", label_visibility="collapsed")
    if upload is not None and st.button("Import CSV", width="stretch"):
        added, errors = import_csv(upload, warehouses)
        if added:
            st.success(f"Imported {added} shipments")
        for e in errors[:10]:
            st.warning(e)
        if len(errors) > 10:
            st.warning(f"...and {len(errors) - 10} more problems")

    st.divider()
    if st.button("Reset demo data", width="stretch"):
        q.reset_demo()
        for k in ("hazards", "trace", "source_status", "summary", "run_error"):
            ss[k] = [] if k in ("hazards", "trace") else ({} if k == "source_status" else "")
        st.rerun()


# ---------------- main ----------------

st.title(f"🛡️ {APP_NAME}")
st.caption("An autonomous agent that watches live climate hazards, reroutes trucks around danger "
           "zones, finds backup suppliers, and drafts orders for your approval.")

counts = pd.Series([s["status"] for s in shipments]).value_counts() if shipments else pd.Series(dtype=int)
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("In transit", int(counts.get("in_transit", 0)))
m2.metric("To approve", len(reroutes) + len(pos))
m3.metric("Rerouted", int(counts.get("rerouted", 0)))
m4.metric("Re-sourced", int(counts.get("resourced", 0)))
m5.metric("Hazards", len(ss.hazards))

left, right = st.columns([3, 2], gap="large")

with left:
    st.subheader("Live map")
    st.caption("Draw a disruption zone with the rectangle or polygon tool. "
               "Dashed = planned shipments (red = at risk), solid green = safe reroute, dotted green = backup order.")
    fmap, center, zoom = build_map(warehouses, suppliers, shipments, pos)
    out = st_folium(fmap, key="map", center=center, zoom=zoom,
                    height=560, use_container_width=True, returned_objects=["all_drawings"])
    drawn = [f["geometry"] for f in (out or {}).get("all_drawings") or []
             if (f.get("geometry") or {}).get("type") == "Polygon"]
    if drawn and drawn != ss.zones:
        ss.zones = drawn
        st.rerun()

with right:
    st.subheader("Agent")
    st.caption("Open the ❯ sidebar to add shipments, upload a CSV or reset the demo.")
    st.write(f"Disruption zones: **{len(ss.zones)}**"
             + ("" if ss.zones else " (the agent will scan live hazards only)"))
    if ss.source_status:
        st.caption("  ·  ".join(f"{'🔴' if v.startswith('FAILED') else '🟢'} {k}"
                                 for k, v in ss.source_status.items()),
                   help="\n\n".join(f"{k}: {v}" for k, v in ss.source_status.items()))
    if st.button("🌊 Load flood scenario (Nadiad)", width="stretch"):
        ss.zones = [SCENARIO_ZONE]
        st.rerun()
    if ss.zones and st.button("Clear zones", width="stretch"):
        ss.zones = []
        st.rerun()

    run = st.button("▶ Run agent", type="primary", width="stretch")
    box = st.container(height=430)
    if run:
        ss.trace, ss.run_error = [], ""
        with box:
            status = st.status("Agent running...", expanded=True)

            def on_step(entries):
                for e in entries:
                    ss.trace.append(e)
                    status.markdown(fmt(e))

            try:
                final = run_agent("user_zone" if ss.zones else "live_scan", ss.zones, on_step=on_step)
                ss.hazards = [h.to_feature() for h in final.get("hazards", [])]
                ss.source_status = final.get("source_status", {})
                ss.summary = final.get("summary", "")
                status.update(label="Run complete", state="complete")
            except Exception as e:
                ss.run_error = f"Agent stopped: {e}"
                status.update(label=ss.run_error, state="error")
        st.rerun()
    else:
        with box:
            if ss.run_error:
                st.error(ss.run_error)
            if ss.summary:
                st.success(ss.summary)
            for e in ss.trace:
                st.markdown(fmt(e))
            if not ss.trace:
                st.caption("The agent's reasoning will stream here, step by step.")

st.divider()
st.subheader("Awaiting your approval")
if not reroutes and not pos:
    st.caption("Nothing pending. Run the agent to generate proposals.")
else:
    if st.button(f"Approve all ({len(reroutes) + len(pos)})"):
        for r in reroutes:
            q.approve_reroute(r["id"])
        for p in pos:
            q.approve_po(p["id"])
        st.toast("All proposals approved")
        st.rerun()

for r in reroutes:
    props = (r["route_geojson"] or {}).get("properties", {})
    with st.container(border=True):
        a, b, c = st.columns([7, 1, 1])
        a.markdown(
            f"🧭 **Reroute shipment #{r['id']}** · {r['origin']} → {r['destination']} · "
            f"{r['qty']} × {r['sku']}  \n"
            f"New route: {props.get('km')} km (+{props.get('extra_km')} km), "
            f"{round((props.get('minutes') or 0) / 60, 1)} h via {props.get('provider')}  \n"
            f"_{props.get('rationale', '')}_ · decided by {props.get('decided_by', '?')}")
        if b.button("Approve", key=f"ar{r['id']}", type="primary"):
            q.approve_reroute(r["id"])
            st.rerun()
        if c.button("Reject", key=f"rr{r['id']}"):
            q.reject_reroute(r["id"])
            st.rerun()

for p in pos:
    with st.container(border=True):
        a, b, c = st.columns([7, 1, 1])
        a.markdown(
            f"🏭 **PO-{p['id']}** · order {p['qty']} × {p['sku']} from **{p['supplier']}** "
            f"for {p['dest_name']} (replaces shipment #{p['shipment_id']})  \n_{p['reasoning']}_")
        if b.button("Approve", key=f"ap{p['id']}", type="primary"):
            if q.approve_po(p["id"]):
                st.toast(f"PO-{p['id']} approved, supplier stock updated")
            st.rerun()
        if c.button("Reject", key=f"rp{p['id']}"):
            q.reject_po(p["id"])
            st.rerun()

with st.expander(f"All shipments ({len(shipments)})"):
    st.dataframe(pd.DataFrame([{k: s[k] for k in ("id", "origin", "destination", "sku", "qty", "status")}
                               for s in shipments]), hide_index=True, width="stretch")
