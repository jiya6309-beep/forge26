"""
The supply-resilience agent (LangGraph state machine).

  detect -> assess -> reroute -> [resource] -> decide -> draft -> finish
       \\-> finish   \\-> finish      (early exits when nothing is at risk)

Python computes every real option (safe reroutes, backup suppliers, hold).
The LLM only CHOOSES among them and explains why; its answer is validated,
so it cannot invent suppliers, routes or numbers. If no LLM is reachable,
a rule-based policy decides and the trace says so.

Use from the UI:   final = run_agent(trigger, user_zones, on_step=callback)
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from db import queries as q
from tools.hazards import get_active_hazards
from tools.llm import LLMUnavailable, generate_json
from tools.routing import RoutingError, find_safe_route, get_route, haversine_km, route_hits

MAX_ACCEPTABLE_DELAY_MIN = 180   # a reroute adding more than 3 h triggers re-sourcing
NEAR_DEG = 0.5                   # ~55 km: shipments farther than this from every hazard skip routing
MAX_SUPPLIERS_TO_ROUTE = 3


class AgentState(TypedDict, total=False):
    trigger: str
    user_zones: list
    hazards: list
    source_status: dict
    affected: list
    plans: list
    decisions: list
    summary: str
    po_ids: list
    run_id: int
    trace: Annotated[list, operator.add]   # each node appends; never overwritten


def _t(step: str, msg: str, **detail) -> dict:
    return {"step": step, "msg": msg, **({"detail": detail} if detail else {})}


def _describe(ev: dict, sid: int) -> str:
    """Routing log event -> one readable line for the trace panel."""
    e = ev.get("event")
    if e == "base_route":
        return f"#{sid}: normal route {ev['km']} km via {ev['provider']}"
    if e == "endpoint_blocked":
        return f"#{sid}: {ev['which']} is inside '{ev['zone']}', no detour can help"
    if e == "plan":
        return (f"#{sid}: round {ev['round']}, {ev['margin_km']} km safety margin, "
                f"{ev['pass_precheck']}/{ev['options']} detour options pass the geometry pre-check")
    if e == "detour_attempt":
        head = f"#{sid}: try {ev['side']} flank with {ev['n_waypoints']} waypoint(s)"
        if ev.get("result") == "clear":
            return f"{head}: {ev['km']} km, clear of all hazards"
        if ev.get("result") == "rejected":
            return f"{head}: {ev['km']} km, still crosses {', '.join(ev['crosses'])}, rejected"
        return f"{head}: routing failed ({ev.get('error', '')[:60]})"
    if e == "self_correct":
        return f"#{sid}: {ev['reason']}, widening margin to {ev['next_margin_km']} km"
    if e == "give_up":
        return f"#{sid}: {ev['reason']} after {ev['rounds']} rounds, will look for backup suppliers"
    if e == "chosen":
        return f"#{sid}: safe route found, {ev['km']} km (+{ev['extra_km']} km)"
    if e == "provider_failed":
        return f"#{sid}: router {ev['provider']} failed, switching provider"
    return f"#{sid}: {ev}"


def _needs_resource(plan: dict) -> bool:
    r = plan["route"]
    if r is None or r.status != "rerouted":
        return True
    return (r.extra_minutes or 0) > MAX_ACCEPTABLE_DELAY_MIN


# ---------------- nodes ----------------

def detect(state: AgentState) -> dict:
    sites = q.get_sites()
    report = get_active_hazards(sites, user_zones=state.get("user_zones"))
    trace = [_t("detect", f"{src}: {status}") for src, status in report.source_status.items()]
    trace.append(_t("detect", f"{len(report.hazards)} active hazards near {len(sites)} sites",
                    hazards=[f"[{h.severity}] {h.title}" for h in report.hazards]))
    return {"hazards": report.hazards, "source_status": report.source_status, "trace": trace}


def assess(state: AgentState) -> dict:
    hazards = state["hazards"]
    zone = unary_union([h.geometry for h in hazards])
    shipments = q.get_active_shipments()
    affected, trace = [], []

    for s in shipments:
        o, d = (s["origin_lat"], s["origin_lon"]), (s["dest_lat"], s["dest_lon"])
        if LineString([(o[1], o[0]), (d[1], d[0])]).distance(zone) > NEAR_DEG:
            continue   # nowhere near any hazard: no API call needed
        try:
            base = get_route([o, d])
        except RoutingError as e:
            affected.append({"shipment": s, "base": None, "hits": []})
            trace.append(_t("assess", f"#{s['id']}: could not route ({str(e)[:80]}), flagged"))
            continue
        hits = route_hits(base, hazards)
        if hits:
            affected.append({"shipment": s, "base": base, "hits": hits})
            trace.append(_t("assess",
                            f"#{s['id']} {s['origin_name']} -> {s['dest_name']} "
                            f"({s['qty']} x {s['sku']}) crosses: {', '.join(h.title for h in hits)}"))

    trace.insert(0, _t("assess", f"Checked {len(shipments)} active shipments, {len(affected)} at risk"))
    return {"affected": affected, "trace": trace}


def reroute(state: AgentState) -> dict:
    plans, trace = [], []
    for a in state["affected"]:
        s = a["shipment"]
        if a["base"] is None:
            plans.append({"shipment": s, "route": None, "alternatives": []})
            continue
        o, d = (s["origin_lat"], s["origin_lon"]), (s["dest_lat"], s["dest_lon"])
        try:
            r = find_safe_route(o, d, state["hazards"], original=a["base"])
        except RoutingError as e:
            trace.append(_t("reroute", f"#{s['id']}: routing failed ({str(e)[:80]})"))
            plans.append({"shipment": s, "route": None, "alternatives": []})
            continue
        trace += [_t("reroute", _describe(ev, s["id"])) for ev in r.attempts]
        plans.append({"shipment": s, "route": r, "alternatives": []})
    return {"plans": plans, "trace": trace}


def resource(state: AgentState) -> dict:
    hazards, trace = state["hazards"], []
    for p in state["plans"]:
        if not _needs_resource(p):
            continue
        s = p["shipment"]
        d = (s["dest_lat"], s["dest_lon"])
        suppliers = [c for c in q.get_suppliers_with_stock(s["sku"], s["qty"])
                     if not any(h.geometry.contains(Point(c["lon"], c["lat"])) for h in hazards)]
        suppliers.sort(key=lambda c: haversine_km((c["lat"], c["lon"]), d))
        trace.append(_t("resource",
                        f"#{s['id']}: {len(suppliers)} suppliers outside danger zones hold "
                        f">= {s['qty']} x {s['sku']}; routing the nearest {min(len(suppliers), MAX_SUPPLIERS_TO_ROUTE)}"))
        for c in suppliers[:MAX_SUPPLIERS_TO_ROUTE]:
            try:
                r = find_safe_route((c["lat"], c["lon"]), d, hazards)
            except RoutingError as e:
                trace.append(_t("resource", f"#{s['id']}: {c['name']} unroutable ({str(e)[:60]})"))
                continue
            if r.chosen:
                p["alternatives"].append({"supplier": c, "route": r})
                trace.append(_t("resource",
                                f"#{s['id']}: {c['name']} (stock {c['qty']}) can deliver safely, "
                                f"{r.chosen.km:.0f} km / {r.chosen.minutes / 60:.1f} h"))
            else:
                trace.append(_t("resource", f"#{s['id']}: {c['name']} has no safe route ({r.status})"))
        if not p["alternatives"]:
            trace.append(_t("resource", f"#{s['id']}: no viable backup supplier found"))
    return {"plans": state["plans"], "trace": trace}


# ---------------- decide (LLM with guardrails) ----------------

class Decision(BaseModel):
    shipment_id: int
    option_id: str
    rationale: str = Field(description="1-3 sentences citing the numbers given")
    evidence: list[str] = Field(default_factory=list, description="hazard ids relied on")


class DecisionSet(BaseModel):
    decisions: list[Decision]
    summary: str


SYSTEM = """You are a logistics risk analyst for an Indian distributor.
For each at-risk shipment, choose exactly ONE option from the options listed for it.
Rules:
- Use only the option ids, numbers and hazard ids provided. Never invent suppliers, routes or figures.
- Prefer a safe reroute of the original shipment when the extra time is acceptable (<= 3 h).
- When comparing a reroute with a supplier, compare total_hours (reroute) with hours (supplier).
- Prefer a backup supplier when the reroute is unsafe, impossible or much slower.
- Choose HOLD only when no safe option exists.
- In "evidence", list the hazard ids your decision relies on.
Return JSON: {"decisions": [{"shipment_id": int, "option_id": str, "rationale": str,
"evidence": [str]}], "summary": str}"""


def _options(plan: dict) -> dict[str, dict]:
    opts = {}
    r = plan["route"]
    if r is not None and r.status in ("rerouted", "clear") and r.chosen:
        opts["R"] = {"type": "reroute", "extra_km": round(r.extra_km, 1),
                     "extra_hours": round(r.extra_minutes / 60, 1),
                     "total_km": round(r.chosen.km, 1),
                     "total_hours": round(r.chosen.minutes / 60, 1)}
    for i, alt in enumerate(plan["alternatives"], start=1):
        c, ar = alt["supplier"], alt["route"]
        opts[f"S{i}"] = {"type": "supplier", "supplier": c["name"], "stock": c["qty"],
                         "km": round(ar.chosen.km, 1),
                         "hours": round(ar.chosen.minutes / 60, 1)}
    opts["HOLD"] = {"type": "hold", "note": "pause shipment until the hazard clears"}
    return opts


def _rule_based(plan: dict, opts: dict) -> tuple[str, str]:
    if "R" in opts and opts["R"]["extra_hours"] * 60 <= MAX_ACCEPTABLE_DELAY_MIN:
        return "R", f"Safe reroute adds {opts['R']['extra_hours']} h, within the 3 h limit."
    suppliers = [k for k in opts if k.startswith("S")]
    if suppliers:
        best = min(suppliers, key=lambda k: opts[k]["hours"])
        return best, f"Fastest safe backup supplier: {opts[best]['supplier']} ({opts[best]['hours']} h)."
    if "R" in opts:
        return "R", f"Only safe option is a reroute adding {opts['R']['extra_hours']} h."
    return "HOLD", "No safe route or backup supplier available."


def decide(state: AgentState) -> dict:
    plans, hazards = state["plans"], state["hazards"]
    options = {p["shipment"]["id"]: _options(p) for p in plans}
    relevant = {h.id: h for p in plans for h in (p["route"].blocked_by if p["route"] else [])}
    hazard_ids = set(relevant) | {h.id for h in hazards}

    facts = {
        "hazards": [{"id": h.id, "title": h.title, "kind": h.kind, "severity": h.severity,
                     "source": h.source, "valid_until": h.valid_until, "evidence": h.evidence}
                    for h in (relevant.values() or hazards)],
        "shipments": [{"shipment_id": p["shipment"]["id"],
                       "route": f"{p['shipment']['origin_name']} -> {p['shipment']['dest_name']}",
                       "cargo": f"{p['shipment']['qty']} x {p['shipment']['sku']}",
                       "options": options[p["shipment"]["id"]]} for p in plans],
    }

    def validate(ds: DecisionSet):
        got = [d.shipment_id for d in ds.decisions]
        if sorted(got) != sorted(options):
            raise ValueError(f"Need exactly one decision for each shipment id {sorted(options)}, got {got}")
        for d in ds.decisions:
            if d.option_id not in options[d.shipment_id]:
                raise ValueError(f"Shipment {d.shipment_id}: option '{d.option_id}' does not exist; "
                                 f"choose from {list(options[d.shipment_id])}")
            unknown = set(d.evidence) - hazard_ids
            if unknown:
                raise ValueError(f"Shipment {d.shipment_id}: unknown hazard ids {sorted(unknown)}")

    llm_log: list = []
    try:
        ds = generate_json("Situation:\n" + json.dumps(facts, indent=1, default=str),
                           DecisionSet, system=SYSTEM, validate=validate, log=llm_log)
        chosen = {d.shipment_id: (d.option_id, d.rationale, d.evidence) for d in ds.decisions}
        summary, decided_by = ds.summary, next(
            (e["provider"] for e in llm_log if e["event"] == "llm_ok"), "llm")
    except LLMUnavailable as e:
        llm_log.append({"event": "fallback", "reason": str(e)[:150]})
        chosen = {}
        for p in plans:
            sid = p["shipment"]["id"]
            oid, why = _rule_based(p, options[sid])
            chosen[sid] = (oid, why, [h.id for h in (p["route"].blocked_by if p["route"] else [])])
        summary, decided_by = f"Rule-based decisions for {len(plans)} shipments (LLM unavailable).", "rules"

    trace = []
    for ev in llm_log:
        if ev["event"] == "llm_provider_failed":
            trace.append(_t("decide", f"LLM {ev['provider']} unavailable, switching to backup"))
        elif ev["event"] == "llm_output_rejected":
            trace.append(_t("decide", f"Guardrail rejected {ev['provider']} answer: {ev['reason'][:120]}; retrying"))
        elif ev["event"] == "fallback":
            trace.append(_t("decide", "No LLM reachable, using rule-based policy"))

    trace.append(_t("decide", f"Decisions made by {decided_by}"))
    decisions = []
    for p in plans:
        sid = p["shipment"]["id"]
        oid, why, evidence = chosen[sid]
        opt = options[sid][oid]
        decisions.append({"plan": p, "option_id": oid, "option": opt, "rationale": why,
                          "evidence": evidence, "decided_by": decided_by})
        trace.append(_t("decide", f"#{sid}: {oid} ({opt['type']}) - {why}", decided_by=decided_by))
    return {"decisions": decisions, "summary": summary, "trace": trace}


def draft(state: AgentState) -> dict:
    """Write proposals to the database. Nothing is sent until a human approves."""
    po_ids, trace = [], []
    for d in state["decisions"]:
        p, s, opt = d["plan"], d["plan"]["shipment"], d["option"]
        if opt["type"] == "reroute":
            q.set_shipment_status(s["id"], "at_risk",
                                  p["route"].chosen.to_feature(proposed=True, shipment_id=s["id"]))
            trace.append(_t("draft", f"#{s['id']}: reroute proposed, awaiting approval"))
        elif opt["type"] == "supplier":
            alt = p["alternatives"][int(d["option_id"][1:]) - 1]
            po = q.create_purchase_order(alt["supplier"]["id"], s["id"], s["sku"], s["qty"],
                                         d["rationale"])
            po_ids.append(po)
            q.set_shipment_status(s["id"], "at_risk")
            trace.append(_t("draft", f"#{s['id']}: purchase order PO-{po} drafted with "
                                     f"{alt['supplier']['name']}, awaiting approval"))
        else:
            q.set_shipment_status(s["id"], "delayed")
            trace.append(_t("draft", f"#{s['id']}: marked as delayed (hold)"))
    return {"po_ids": po_ids, "trace": trace}


def finish(state: AgentState) -> dict:
    if not state.get("hazards"):
        msg = "No active hazards. All shipments clear."
    elif not state.get("affected"):
        msg = "Hazards detected, but no active shipment crosses them."
    else:
        msg = state.get("summary", "Run complete.")
    trace = [_t("finish", msg)]
    status = "awaiting_approval" if state.get("decisions") else "completed"
    try:
        run_id = q.save_agent_run(state.get("trigger", "manual"), state.get("trace", []) + trace, status)
    except Exception as e:   # logging must never break the run
        run_id = None
        trace.append(_t("finish", f"Could not save run log ({str(e)[:80]})"))
    return {"run_id": run_id, "summary": msg, "trace": trace}


# ---------------- graph ----------------

def build_graph():
    g = StateGraph(AgentState)
    for name, fn in [("detect", detect), ("assess", assess), ("reroute", reroute),
                     ("resource", resource), ("decide", decide), ("draft", draft),
                     ("finish", finish)]:
        g.add_node(name, fn)

    g.add_edge(START, "detect")
    g.add_conditional_edges("detect", lambda s: "assess" if s.get("hazards") else "finish",
                            {"assess": "assess", "finish": "finish"})
    g.add_conditional_edges("assess", lambda s: "reroute" if s.get("affected") else "finish",
                            {"reroute": "reroute", "finish": "finish"})
    g.add_conditional_edges("reroute",
                            lambda s: "resource" if any(_needs_resource(p) for p in s["plans"]) else "decide",
                            {"resource": "resource", "decide": "decide"})
    g.add_edge("resource", "decide")
    g.add_edge("decide", "draft")
    g.add_edge("draft", "finish")
    g.add_edge("finish", END)
    return g.compile()


GRAPH = build_graph()


def run_agent(trigger: str = "manual", user_zones: list | None = None,
              on_step: Callable[[list], None] | None = None) -> dict:
    """Runs the graph, calling on_step(new_trace_entries) as each node finishes."""
    seen, final = 0, {}
    for state in GRAPH.stream({"trigger": trigger, "user_zones": user_zones or [], "trace": []},
                              stream_mode="values"):
        new = state.get("trace", [])[seen:]
        seen += len(new)
        if new and on_step:
            on_step(new)
        final = state
    return final
