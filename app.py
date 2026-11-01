"""
SupplySentinel: the Streamlit front end.

Run locally:  streamlit run app.py
"""
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium

st.set_page_config(page_title="SupplySentinel", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="collapsed")

load_dotenv()
try:  # on Streamlit Cloud the keys live in the app's Secrets
    for key, value in st.secrets.items():
        os.environ.setdefault(key, str(value))
except Exception:
    pass

from agent.graph import run_agent  # noqa: E402
from db import queries as q        # noqa: E402
from ui import components as ui    # noqa: E402
from ui import sidebar             # noqa: E402
from ui.map import build_map       # noqa: E402

# A ready-made zone on NH-48 between Nadiad and Anand, so the demo doesn't
# depend on drawing a rectangle in exactly the right place.
FLOOD_SCENARIO = {"type": "Polygon", "coordinates": [[
    [72.80, 22.60], [72.98, 22.60], [72.98, 22.76], [72.80, 22.76], [72.80, 22.60]]]}

ss = st.session_state
FRESH_STATE = {"zones": [], "hazards": [], "trace": [], "source_status": {},
               "summary": "", "run_error": "", "decided_by": ""}
for key, value in FRESH_STATE.items():
    ss.setdefault(key, value)


def forget_last_run():
    for key, value in FRESH_STATE.items():
        if key != "zones":
            ss[key] = type(value)()


ui.inject_css()

try:
    warehouses = q.get_warehouses()
    suppliers = q.get_suppliers()
    shipments = q.get_all_shipments()
    reroutes = q.get_pending_reroutes()
    orders = q.get_pending_pos()
except Exception as e:
    st.error(f"Can't reach the database right now: {e}")
    st.stop()

sidebar.render(warehouses, on_reset=forget_last_run)

held = [s for s in shipments if s["status"] == "delayed"]
waiting = len(reroutes) + len(orders)
status_counts = pd.Series([s["status"] for s in shipments]).value_counts() if shipments else {}


def current_step():
    if waiting or held:
        return 3
    return 2 if ss.zones else 1


# ---------- top of the page ----------

ui.html(ui.header(ss.source_status, ss.decided_by))
stepper_slot = st.empty()
stepper_slot.markdown(ui.stepper(current_step()), unsafe_allow_html=True)
ui.html(ui.kpis(
    in_transit=int(status_counts.get("in_transit", 0)),
    at_risk=waiting,
    rerouted=int(status_counts.get("rerouted", 0)),
    resourced=int(status_counts.get("resourced", 0)),
    hazards=len(ss.hazards),
))

map_col, agent_col = st.columns([3, 2], gap="large")

# ---------- map ----------

with map_col:
    if ss.zones:
        label = "Scenario: flood on NH-48 near Nadiad" if ss.zones == [FLOOD_SCENARIO] else \
            f"{len(ss.zones)} disruption zone{'s' if len(ss.zones) > 1 else ''} drawn"
        ui.html(f'<span class="ss-scenario">{label}</span>')
    else:
        ui.html('<span class="ss-scenario none">No disruption chosen · draw a zone with the '
                'square tool, or load the flood scenario</span>')

    fmap, center, zoom = build_map(warehouses, suppliers, shipments, orders, ss.hazards, ss.zones)
    drawn = st_folium(fmap, key="map", center=center, zoom=zoom, height=560,
                      use_container_width=True, returned_objects=["all_drawings"])

    polygons = [f["geometry"] for f in (drawn or {}).get("all_drawings") or []
                if (f.get("geometry") or {}).get("type") == "Polygon"]
    if polygons and polygons != ss.zones:
        ss.zones = polygons
        st.rerun()

    ui.html('<div class="ss-legend">'
            '<span><em class="zone"></em>Danger zone</span>'
            '<span><em class="risk"></em>At risk</span>'
            '<span><em class="safe"></em>Safe reroute</span>'
            '<span><em class="backup"></em>Backup order</span></div>')

# ---------- agent panel ----------

with agent_col:
    left, right = st.columns([3, 2])
    if left.button("Load flood scenario", width="stretch"):
        ss.zones = [FLOOD_SCENARIO]
        st.rerun()
    if right.button("Clear zones", width="stretch", disabled=not ss.zones):
        ss.zones = []
        st.rerun()

    run_clicked = st.button("Run agent", type="primary", width="stretch")
    now_slot, log_slot = st.empty(), st.empty()

    if run_clicked:
        ss.trace, ss.run_error, ss.decided_by = [], "", ""
        stepper_slot.markdown(ui.stepper(2, running=True), unsafe_allow_html=True)
        now_slot.markdown(ui.right_now("Starting up", "Reading shipments and live sources…",
                                       0.03, running=True), unsafe_allow_html=True)

        def show_progress(entries):
            ss.trace.extend(entries)
            for entry in entries:
                if entry["msg"].startswith("Decisions made by "):
                    ss.decided_by = entry["msg"].removeprefix("Decisions made by ")
            now_slot.markdown(ui.right_now_for(entries[-1]), unsafe_allow_html=True)
            log_slot.markdown(ui.agent_log(ss.trace[-40:], fresh=len(entries), running=True),
                              unsafe_allow_html=True)

        try:
            final = run_agent("user_zone" if ss.zones else "live_scan", ss.zones, on_step=show_progress)
            ss.hazards = [h.to_feature() for h in final.get("hazards", [])]
            ss.source_status = final.get("source_status", {})
            ss.summary = final.get("summary", "")
        except Exception as e:
            ss.run_error = f"The run stopped: {e}"
        st.rerun()

    # Not running: show where things stand
    if ss.run_error:
        now_slot.markdown(ui.right_now("Something went wrong", ss.run_error, 1.0,
                                       eyebrow="RUN STOPPED"), unsafe_allow_html=True)
    elif ss.summary:
        now_slot.markdown(ui.right_now("Run complete", ss.summary, 1.0, eyebrow="DONE", done=True),
                          unsafe_allow_html=True)
    elif ss.zones:
        now_slot.markdown(ui.right_now("Ready to run", "Hit Run agent. You’ll see every check, "
                                       "detour and decision as it happens.", 0.0, eyebrow="NEXT"),
                          unsafe_allow_html=True)
    else:
        now_slot.markdown(ui.right_now("Ready when you are", "Load the flood scenario or draw a zone "
                                       "on the map, then run the agent.", 0.0, eyebrow="START HERE"),
                          unsafe_allow_html=True)
    log_slot.markdown(ui.agent_log(ss.trace[-40:]), unsafe_allow_html=True)

# ---------- review & approve ----------

ui.html(f'<div class="ss-section-title">Review &amp; approve · {waiting} waiting</div>')

if not (reroutes or orders or held):
    st.caption("Nothing waiting for you. Run the agent to get proposals.")
elif waiting and st.button(f"Approve all {waiting}"):
    for r in reroutes:
        q.approve_reroute(r["id"])
    for o in orders:
        q.approve_po(o["id"])
    st.toast("All proposals approved")
    st.rerun()

cards = ([("reroute", r) for r in reroutes] + [("order", o) for o in orders]
         + [("hold", h) for h in held])

for row_start in range(0, len(cards), 3):
    for column, (kind, item) in zip(st.columns(3), cards[row_start:row_start + 3]):
        with column, st.container(border=True):
            if kind == "reroute":
                props = (item["route_geojson"] or {}).get("properties", {})
                hours = round((props.get("minutes") or 0) / 60, 1)
                ui.html(ui.card_text(
                    f"REROUTE · #{item['id']}",
                    f"{item['origin']} → {item['destination']}",
                    f"{item['qty']} × {item['sku']} · {props.get('km')} km (+{props.get('extra_km')}) · {hours} h",
                    props.get("rationale", ""),
                ))
                approve, reject = st.columns([3, 2])
                if approve.button("Approve", key=f"approve-route-{item['id']}", type="primary", width="stretch"):
                    q.approve_reroute(item["id"])
                    st.rerun()
                if reject.button("Reject", key=f"reject-route-{item['id']}", width="stretch"):
                    q.reject_reroute(item["id"])
                    st.rerun()

            elif kind == "order":
                ui.html(ui.card_text(
                    f"BACKUP ORDER · PO-{item['id']}",
                    f"{item['qty']} × {item['sku']} from {item['supplier']}",
                    f"For {item['dest_name']} · replaces shipment #{item['shipment_id']}",
                    item["reasoning"] or "",
                ))
                approve, reject = st.columns([3, 2])
                if approve.button("Approve", key=f"approve-po-{item['id']}", type="primary", width="stretch"):
                    if q.approve_po(item["id"]):
                        st.toast(f"PO-{item['id']} approved, supplier stock updated")
                    st.rerun()
                if reject.button("Reject", key=f"reject-po-{item['id']}", width="stretch"):
                    q.reject_po(item["id"])
                    st.rerun()

            else:
                ui.html(ui.card_text(
                    f"ON HOLD · #{item['id']}",
                    f"{item['origin']} → {item['destination']}",
                    "No safe route and no backup supplier. Marked as delayed; "
                    "run the agent again once the hazard clears.",
                    hold=True,
                ))

with st.expander(f"All shipments ({len(shipments)})"):
    st.dataframe(
        pd.DataFrame([{k: s[k] for k in ("id", "origin", "destination", "sku", "qty", "status")}
                      for s in shipments]),
        hide_index=True, width="stretch",
    )
