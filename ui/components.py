"""
Small HTML building blocks for the Night Shift UI.

Streamlit's own widgets handle anything interactive (buttons, forms, the map).
Everything here is display-only markup styled by ui/style.css.
"""
from html import escape
from pathlib import Path

import streamlit as st

CSS_FILE = Path(__file__).with_name("style.css")

SHIELD = (
    '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2DD4BF" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"></path>'
    '<path d="M9 12l2 2 4-4"></path></svg>'
)
CHECK = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#062420" '
    'stroke-width="3.5" aria-hidden="true"><path d="M5 12l5 5L20 7"></path></svg>'
)

# The agent's stages, in order, with the plain-English headline shown for each.
STAGES = {
    "detect": "Checking live hazards",
    "assess": "Finding shipments at risk",
    "reroute": "Rerouting around the danger zone",
    "resource": "Looking for backup suppliers",
    "decide": "Choosing the best option for each shipment",
    "draft": "Preparing proposals for you",
    "finish": "Run complete",
}
STAGE_ORDER = list(STAGES)


def html(markup: str):
    st.markdown(markup, unsafe_allow_html=True)


def inject_css():
    html(f"<style>{CSS_FILE.read_text(encoding='utf-8')}</style>")


# ---------- header ----------

def _chip(label: str, state: str = "", live: bool = False) -> str:
    classes = " ".join(c for c in ("ss-chip", state, "live" if live else "") if c)
    return f'<span class="{classes}"><i></i>{escape(label)}</span>'


def header(source_status: dict, decided_by: str = "") -> str:
    chips = []
    if source_status:
        for source, status in source_status.items():
            chips.append(_chip(source, "bad" if status.startswith("FAILED") else "ok", live=True))
    else:
        chips.append(_chip("Sources not checked yet"))

    if decided_by.startswith("groq"):
        chips.append(_chip("Groq (Gemini busy)", "warn"))
    elif decided_by.startswith("gemini"):
        chips.append(_chip("Gemini", "ok"))
    elif decided_by == "rules":
        chips.append(_chip("Rule-based fallback", "warn"))

    return (
        '<div class="ss-header">'
        f'<div class="ss-brand">{SHIELD}<b>SupplySentinel</b><small>OPS · GUJARAT CORRIDOR</small></div>'
        f'<div class="ss-chips">{"".join(chips)}</div>'
        '</div>'
    )


# ---------- stepper ----------

def stepper(active: int, running: bool = False) -> str:
    labels = ["Choose a disruption", "Agent at work", "Review &amp; approve"]
    items = []
    for number, label in enumerate(labels, start=1):
        if number < active:
            state, dot = "done", CHECK
        elif number == active:
            state, dot = "active", str(number)
        else:
            state, dot = "todo", str(number)
        if running and number == active:
            state += " running"
        current = ' aria-current="step"' if number == active else ""
        items.append(f'<li class="ss-step {state}"{current}><span class="dot">{dot}</span>{label}</li>')
    return f'<ol class="ss-steps" aria-label="Progress">{"".join(items)}</ol>'


# ---------- KPIs ----------

def kpis(in_transit: int, at_risk: int, rerouted: int, resourced: int, hazards: int) -> str:
    tiles = [
        ("IN TRANSIT", in_transit, ""),
        ("AWAITING YOU", at_risk, "risk" if at_risk else ""),
        ("REROUTED", rerouted, "good"),
        ("RE-SOURCED", resourced, "good"),
        ("HAZARDS", hazards, "risk" if hazards else ""),
    ]
    cells = "".join(f'<div class="ss-kpi {cls}"><span>{label}</span><b>{value}</b></div>'
                    for label, value, cls in tiles)
    return f'<div class="ss-kpis">{cells}</div>'


# ---------- "right now" card ----------

def right_now(headline: str, detail: str, progress: float, eyebrow: str = "RIGHT NOW",
              running: bool = False, done: bool = False) -> str:
    pct = max(0, min(100, round(progress * 100)))
    eyebrow_cls = "ss-eyebrow good" if done else "ss-eyebrow"
    bar_cls = "ss-bar running" if running else "ss-bar"
    return (
        '<div class="ss-now">'
        f'<span class="{eyebrow_cls}">{escape(eyebrow)}</span>'
        f'<h3>{escape(headline)}</h3>'
        f'<p>{escape(detail)}</p>'
        f'<div class="{bar_cls}"><div style="width:{pct}%"></div></div>'
        '</div>'
    )


def right_now_for(entry: dict) -> str:
    """Turn the latest trace entry into the plain-English card."""
    step = entry.get("step", "")
    position = STAGE_ORDER.index(step) if step in STAGE_ORDER else 0
    return right_now(STAGES.get(step, "Working"), entry.get("msg", ""),
                     progress=(position + 0.5) / len(STAGE_ORDER), running=True)


# ---------- agent log ----------

def _tone(message: str) -> str:
    text = message.lower()
    if "rejected" in text or "failed" in text or "no safe" in text:
        return "bad"
    if "widening" in text or "switching" in text or "unavailable" in text or "fallback" in text:
        return "warn"
    if "clear of all hazards" in text or "safe route found" in text or "can deliver safely" in text:
        return "good"
    return ""


def agent_log(entries: list, fresh: int = 0, running: bool = False) -> str:
    if not entries:
        return ('<div class="ss-log"><span class="empty">The agent’s reasoning streams here, '
                'step by step: every check, every rejected detour, every fallback.</span></div>')

    lines = []
    first_fresh = len(entries) - fresh
    for i, entry in enumerate(entries):
        classes = ["ss-line", _tone(entry["msg"])]
        if i >= first_fresh:
            classes.append("new")
        lines.append(f'<div class="{" ".join(c for c in classes if c)}">'
                     f'<span class="tag">{escape(entry["step"])}</span>'
                     f'<span class="msg">{escape(entry["msg"])}</span></div>')
    if running:
        lines.append('<div class="ss-line"><span class="tag"></span><span class="ss-cursor">▍</span></div>')
    return f'<div class="ss-log">{"".join(lines)}</div>'


# ---------- review cards ----------

def card_text(eyebrow: str, title: str, meta: str, why: str = "", hold: bool = False) -> str:
    eyebrow_cls = "ss-card-eyebrow hold" if hold else "ss-card-eyebrow"
    why_html = f'<div class="ss-card-why">{escape(why)}</div>' if why else ""
    return (f'<div class="{eyebrow_cls}">{escape(eyebrow)}</div>'
            f'<div class="ss-card-title">{escape(title)}</div>'
            f'<div class="ss-card-meta">{escape(meta)}</div>{why_html}')
