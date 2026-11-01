# 🛡️ SupplySentinel

**An autonomous agent that keeps Indian supply chains moving through climate disruptions.**
It watches live disaster alerts and weather forecasts, finds which shipments are at risk,
reroutes trucks around danger zones, finds backup suppliers with stock, and drafts
orders for a human to approve, showing every step of its reasoning.

🔗 **Live app:** https://supplysenetial.streamlit.app/ 
🎬 **Demo video:** https://drive.google.com/file/d/106mT5rqgK1cNUaj2uGZzV3eymmtrOezs/view?usp=drive_link  
📊 **Pitch deck:** https://docs.google.com/presentation/d/1Kvzacs7wsT_1hNlCoOW8qTc2nHjTzFo1/edit?usp=sharing&ouid=101812570380967967487&rtpof=true&sd=true  
🏷️ **Track:** Climate, Resource Infrastructure & Global Supply Resilience (Forge'26)

---

## The problem

Every monsoon and cyclone season, floods and storms cut highways across India. When a
corridor like NH-48 (Ahmedabad to Mumbai) floods, distributors find out late, trucks sit
in water or queues, stores run out of stock, and logistics teams scramble across phone
calls and spreadsheets to work out which deliveries are affected and what to do.

Existing tools show weather *or* track trucks. None of them connect a live hazard to
specific shipments, work out a safe alternative, check supplier stock, and prepare the
decision, all in one loop.

**Who it serves:** small and mid-sized distributors and logistics teams (FMCG, food,
pharma) that run trucks between warehouses, suppliers and retail stores, and lack the
enterprise supply-chain software large companies use.

## What it does

1. **Detects** hazards from live GDACS disaster alerts and Open-Meteo forecasts scored
   against official **IMD thresholds** (heavy rain ≥ 64.5 mm/day, heat wave ≥ 45 °C),
   plus disruption zones a user draws on the map.
2. **Assesses** which in-transit shipments' real road routes cross a danger zone.
3. **Reroutes** around the zone with a plan, execute, verify, retry loop that works for
   zones of any size.
4. **Re-sources** when rerouting fails or adds more than 3 hours: finds suppliers outside
   the danger zone that hold enough stock, and checks they have a safe route.
5. **Decides** with an LLM that may only choose among options the system computed, and
   must cite the hazard evidence. Invalid answers are rejected and retried.
6. **Drafts** reroutes and purchase orders for **human approval**. Nothing is executed
   automatically; approving a purchase order updates supplier stock in the database.

## Architecture

```mermaid
flowchart LR
    subgraph Sources["Live data sources"]
        GDACS["GDACS disaster alerts<br/>(API, RSS fallback)"]
        OM["Open-Meteo forecasts<br/>+ IMD thresholds"]
        USER["User-drawn zones"]
        OSM["OpenStreetMap<br/>(Overpass, 3 mirrors)"]
    end

    subgraph Agent["LangGraph agent"]
        HZ["tools/hazards.py<br/>danger-zone polygons"]
        RT["tools/routing.py<br/>detour engine"]
        LLM["tools/llm.py<br/>guardrailed decisions"]
        G["agent/graph.py<br/>state machine"]
    end

    subgraph Routers["Road routing"]
        VAL["Valhalla (truck)"]
        OSRM["OSRM (fallback)"]
    end

    subgraph Models["LLMs"]
        GEM["Gemini"]
        GROQ["Groq gpt-oss-120b<br/>(fallback)"]
    end

    DB[("Supabase Postgres<br/>warehouses, suppliers,<br/>inventory, shipments,<br/>purchase orders, run logs")]
    UI["Streamlit app<br/>map, live trace,<br/>approval queue"]

    GDACS --> HZ
    OM --> HZ
    USER --> HZ
    OSM -->|seed real locations| DB
    HZ --> G
    G <--> RT
    RT --> VAL
    RT -.->|if down| OSRM
    G <--> LLM
    LLM --> GEM
    LLM -.->|if down| GROQ
    G <--> DB
    UI -->|run agent| G
    G -->|streams trace| UI
    UI <-->|approve / reject| DB
```

### The agent's state graph

```mermaid
flowchart TD
    START((start)) --> detect["detect<br/>hazards from all sources"]
    detect -->|no hazards| finish
    detect -->|hazards found| assess["assess<br/>which shipments cross a zone?"]
    assess -->|none at risk| finish
    assess -->|at risk| reroute["reroute<br/>detour engine per shipment"]
    reroute -->|all rerouted within 3 h| decide
    reroute -->|no safe route or too slow| resource["resource<br/>backup suppliers with stock"]
    resource --> decide["decide<br/>LLM picks from computed options"]
    decide --> draft["draft<br/>proposals for approval"]
    draft --> finish["finish<br/>save run log"]
    finish --> END((end))
```

## Key engineering decisions

### A detour engine that works for real disasters
Public routers can only avoid small areas (Valhalla caps "avoid" polygons at a 10 km
outline), while real floods and cyclones are far bigger. So the agent reroutes on its own:

1. Get the normal road route and test it against every hazard polygon (Shapely).
2. If it is blocked and the origin or destination is *inside* the zone, report
   `endpoint_blocked`: no detour can help, so the shipment goes to re-sourcing.
3. Draw a safety ring around the blocking zones and split it into a **left and right flank**
   relative to the direction of travel.
4. Generate flank paths with 1, 2, 3 and 5 waypoints in travel order. A single waypoint
   says where to go but not how; several waypoints force the route around the zone.
5. Pre-check each path with straight-line geometry (free, no API call), then route the
   most promising ones on real roads.
6. **Verify** every candidate against *all* hazards, not just the one being avoided.
7. If nothing is clear, **self-correct** by doubling the safety margin and trying again.

Every attempt is logged and shown in the UI, e.g.
*"try left flank with 1 waypoint: still crosses flood zone, rejected → widening margin
to 10 km → 3 waypoints: clear (+30.7 km)"*.

### Guardrails against hallucination
- Python computes every real option (safe reroute, each backup supplier, hold). The LLM
  only **chooses** and explains.
- Its JSON answer must match a Pydantic schema, and a validator checks that every
  shipment gets exactly one decision, every option exists, and every cited hazard id is
  real. On failure, the model receives the exact error and retries.
- If no LLM answers validly, a transparent **rule-based policy** decides, and the trace
  says so.
- Every hazard carries its evidence (measured value, threshold, source link, and whether
  the radius is an estimate), so decisions are traceable to data.

### Resilience: every external dependency has a fallback

| Job | Primary | Fallback | On total failure |
|---|---|---|---|
| Disaster alerts | GDACS JSON API | GDACS RSS feed | Recorded in source status; weather still runs |
| Weather | Open-Meteo | (independent source) | Recorded; other sources still run |
| Road routing | Valhalla (truck) | OSRM | Shipment flagged for re-sourcing |
| Decisions | Gemini | Groq gpt-oss-120b | Rule-based policy |
| Location data | Overpass | 2 mirrors + local cache | Seed uses cached OSM data |

HTTP calls retry temporary errors (429/5xx) with backoff and fail fast on real errors.
Routes are cached in memory and on disk (`route_cache.json`, simplified to ~50 m), so shipments that share a corridor cost one API call and repeat runs take seconds. Hazards, stock and LLM decisions are always fetched live. Delete the file to start cold.

## Data sources

| Data | Source | Live? |
|---|---|---|
| Disaster events | [GDACS](https://www.gdacs.org) (UN / EU JRC) | ✅ every run |
| Weather forecasts | [Open-Meteo](https://open-meteo.com) | ✅ every run |
| Rain and heat thresholds | India Meteorological Department categories | Official values |
| Road routes | [Valhalla](https://valhalla1.openstreetmap.de) / [OSRM](https://project-osrm.org) | ✅ every run |
| Warehouse, supplier and store locations | [OpenStreetMap](https://www.openstreetmap.org) via Overpass | ✅ fetched when seeding |

**About the operational records:** warehouse, supplier and store *locations* are real
OpenStreetMap places (see `db/osm_cache.json`). Inventory levels and shipments represent
the distributor's operational records, which in production would come from the company's
ERP. They are generated once by `db/seed.py`, stored in the live Postgres database, and
editable in the app (add shipments by form or CSV upload).

## Tech stack

Python 3.12 · LangGraph · Streamlit · Folium / Leaflet · Shapely · Pydantic ·
Supabase Postgres (psycopg 3) · Gemini · Groq · Valhalla · OSRM · GDACS · Open-Meteo ·
OpenStreetMap Overpass

## Project structure

```
app.py               Streamlit page: layout, agent run, review & approve
ui/components.py     Header, stepper, KPI strip, live agent log, cards (HTML pieces)
ui/map.py            Dark map with hazard zones and animated safe routes
ui/sidebar.py        Add shipment, validated CSV import, demo reset
ui/style.css         Theme and animations (respects reduced-motion settings)
agent/graph.py       LangGraph state machine and decision guardrails
tools/hazards.py     GDACS + Open-Meteo/IMD + user zones -> hazard polygons with evidence
tools/routing.py     Routing with provider fallback, route cache, detour engine
tools/llm.py         Gemini -> Groq fallback, schema validation, retry with feedback
tools/http.py        Shared HTTP client with retries
db/schema.sql        Database schema
db/queries.py        All database access
db/seed.py           Real OSM locations + operational records
smoke_test.py        Checks every external service and its fallback
test_agent.py        Runs the full agent from the terminal
```

## Run it locally

```bash
git clone https://github.com/jiya6309-beep/forge26.git
cd forge26
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a free [Supabase](https://supabase.com) project, run `db/schema.sql` in its SQL
Editor, then create `.env` from `.env.example`:

```
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.8-flash
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-120b
DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
```

```bash
python smoke_test.py          # every service should PASS
python -m db.seed --reset     # load real locations + operational records
streamlit run app.py          # open http://localhost:8501
```

In the app: click **Load flood scenario (Nadiad)** or draw a zone, then **Run agent**,
and approve or reject the proposals. **Reset demo data** in the sidebar restores the
starting state.

## Limitations and roadmap

- **Limitations:** public routing servers are rate-limited (about 1 request/second), so
  large fleets take longer. GDACS often reports only an event's centre, so its affected
  radius is estimated by severity (marked `approx_radius_km`). Supplier coverage depends on
  OpenStreetMap completeness.
- **Next:** connect to ERP and fleet GPS feeds; self-hosted Valhalla for speed; scheduled
  background scans with WhatsApp / email alerts; IMD district warnings and flood-gauge data;
  learning from approved and rejected decisions.

## Team

- TEAM_MEMBER_1
- TEAM_MEMBER_2
