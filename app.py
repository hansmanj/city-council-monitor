#!/usr/bin/env python3
"""NYC City Council Mental Health Dashboard"""

import json
import os
import sqlite3
from dotenv import load_dotenv
load_dotenv()
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template, redirect, url_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
DB_PATH = Path("cache.db")

# ── Legistar Config ────────────────────────────────────────────────────────────

TOKEN = os.environ.get("LEGISTAR_TOKEN", "").strip().lstrip("=")
BASE_URL = "https://webapi.legistar.com/v1/nyc"
CACHE_TTL = 30  # minutes

# Committee body IDs (verified from Legistar API)
TARGET_BODIES = {
    "Committee on Mental Health and Substance Use": 5315,
    "Committee on Health": 14,
    "Committee on Hospitals": 5241,
    "Committee on Public Safety": 19,
    "Committee on Housing and Buildings": 16,
    "Committee on General Welfare": 12,
    "City Council (Stated Meeting)": 1,
}

# These committees are always included — no keyword filtering needed
ALWAYS_INCLUDE = {5315, 5241, 1}

# ── Topics & Keywords ──────────────────────────────────────────────────────────

TOPICS = {
    "mental_health": {
        "label": "Mental Health",
        "tag": "bg-blue-100 text-blue-800",
        "btn_active": "bg-blue-100 text-blue-800 ring-1 ring-blue-400 font-semibold",
        "btn_inactive": "bg-blue-50 text-blue-500",
        "terms": [
            "mental health", "behavioral health", "psychiatric", "psychiatry",
            "psychosis", "omh", "dohmh", "mental hygiene", "suicide", "self-harm",
            "inpatient psychiatric", "outpatient mental",
        ],
    },
    "substance_use": {
        "label": "Substance Use",
        "tag": "bg-purple-100 text-purple-800",
        "btn_active": "bg-purple-100 text-purple-800 ring-1 ring-purple-400 font-semibold",
        "btn_inactive": "bg-purple-50 text-purple-500",
        "terms": [
            "substance use", "opioid", "addiction", "harm reduction", "naloxone",
            "overdose", "recovery", "detox", "methadone", "buprenorphine",
            "drug treatment", "alcohol use disorder", "syringe",
        ],
    },
    "crisis": {
        "label": "Crisis Response",
        "tag": "bg-red-100 text-red-800",
        "btn_active": "bg-red-100 text-red-800 ring-1 ring-red-400 font-semibold",
        "btn_inactive": "bg-red-50 text-red-500",
        "terms": [
            "crisis response", "crisis intervention", "mobile crisis", "988",
            "de-escalation", "spoa", "b-heard", "involuntary", "kendra",
            "mental health emergency", "co-response", "emergency psychiatric",
            "psychiatric emergency",
        ],
    },
    "housing": {
        "label": "Housing",
        "tag": "bg-emerald-100 text-emerald-800",
        "btn_active": "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-400 font-semibold",
        "btn_inactive": "bg-emerald-50 text-emerald-500",
        "terms": [
            "supportive housing", "homelessness", "transitional housing", "shelter",
            "encampment", "street outreach", "safe haven", "housing first",
            "permanent housing", "unsheltered",
        ],
    },
    "workforce": {
        "label": "Workforce",
        "tag": "bg-amber-100 text-amber-800",
        "btn_active": "bg-amber-100 text-amber-800 ring-1 ring-amber-400 font-semibold",
        "btn_inactive": "bg-amber-50 text-amber-600",
        "terms": [
            "peer support", "peer counselor", "peer specialist", "telepsychiatry",
            "social worker", "clinician", "psychiatric nurse", "workforce",
            "staffing", "training academy",
        ],
    },
    "youth": {
        "label": "Youth",
        "tag": "bg-orange-100 text-orange-800",
        "btn_active": "bg-orange-100 text-orange-800 ring-1 ring-orange-400 font-semibold",
        "btn_inactive": "bg-orange-50 text-orange-500",
        "terms": [
            "school-based mental health", "youth mental health", "youth",
            "adolescent", "child welfare", "children's mental health",
            "school counselor", "acs", "foster care",
        ],
    },
}

ALL_TERMS = [t for td in TOPICS.values() for t in td["terms"]]

SKIP_MATTER_TYPES = {"Land Use Application", "Land Use Call-Up", "Commissioner's Report"}

STATUS_STYLES = {
    "committee":        "bg-sky-100 text-sky-700",
    "enacted":          "bg-green-100 text-green-700",
    "signed":           "bg-green-100 text-green-700",
    "passed":           "bg-emerald-100 text-emerald-700",
    "laid over":        "bg-amber-100 text-amber-700",
    "filed":            "bg-gray-100 text-gray-500",
    "vetoed":           "bg-red-100 text-red-700",
    "withdrawn":        "bg-gray-100 text-gray-500",
    "mayor":            "bg-green-100 text-green-700",
}


def get_topics(text: str) -> list[str]:
    t = text.lower()
    return [key for key, td in TOPICS.items() if any(term in t for term in td["terms"])]


def is_relevant(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in ALL_TERMS)


# ── Database Cache ─────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

init_db()


def cache_get(key: str):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row and datetime.now() < datetime.fromisoformat(row[1]):
        return json.loads(row[0])
    return None


def cache_set(key: str, value, ttl: int = CACHE_TTL):
    exp = (datetime.now() + timedelta(minutes=ttl)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), exp),
        )


# ── Legistar API ───────────────────────────────────────────────────────────────

def legistar(endpoint: str, params: dict = None) -> list:
    """GET request to Legistar API. params keys use OData $ prefix."""
    parts = [f"token={TOKEN}"]
    if params:
        for k, v in params.items():
            parts.append(f"{k}={str(v).replace(' ', '%20')}")
    url = f"{BASE_URL}/{endpoint}?" + "&".join(parts)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def matter_url(mid: int, guid: str) -> str:
    return f"https://nyc.legistar.com/LegislationDetail.aspx?ID={mid}&GUID={guid}"


# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch_hearings() -> list[dict]:
    today = datetime.now()
    past_30  = (today - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    future_90 = (today + timedelta(days=90)).strftime("%Y-%m-%dT00:00:00")
    today_str = today.strftime("%Y-%m-%dT00:00:00")

    raw_events = []
    for name, body_id in TARGET_BODIES.items():
        try:
            events = legistar("events", {
                "$filter": f"EventBodyId eq {body_id} and EventDate ge datetime'{past_30}' and EventDate le datetime'{future_90}'",
                "$orderby": "EventDate asc",
            })
        except Exception as e:
            log.warning(f"Events fetch failed for {name}: {e}")
            events = []
        for ev in events:
            ev["_name"] = name
            ev["_body_id"] = body_id
            ev["_is_past"] = ev.get("EventDate", "") < today_str
        raw_events.extend(events)

    enriched = []
    for ev in raw_events:
        eid = ev["EventId"]
        try:
            items = legistar(f"events/{eid}/eventitems")
        except Exception:
            items = []

        # Separate T-type items (hearing titles) from bill items
        hearing_titles = []
        agenda_items = []
        for i in items:
            if not i.get("EventItemMatterId"):
                continue
            file = i.get("EventItemMatterFile", "")
            t = (i.get("EventItemTitle") or "") + " " + (i.get("EventItemMatterName") or "")
            if file.startswith("T"):
                hearing_titles.append(i.get("EventItemTitle") or i.get("EventItemMatterName", ""))
            else:
                agenda_items.append({
                    "title": i.get("EventItemTitle") or i.get("EventItemMatterName", ""),
                    "file":  file,
                    "type":  i.get("EventItemMatterType", ""),
                    "status": i.get("EventItemMatterStatus", ""),
                    "topics": get_topics(t),
                    "url": matter_url(
                        i["EventItemMatterId"],
                        i.get("EventItemMatterGuid", ""),
                    ),
                })

        always = ev["_body_id"] in ALWAYS_INCLUDE
        all_item_text = " ".join(
            (i.get("EventItemTitle") or "") + " " + (i.get("EventItemMatterName") or "")
            for i in items
        )
        event_topics = sorted({t for item in agenda_items for t in item["topics"]})

        if always or is_relevant(all_item_text):
            enriched.append({
                "id":            eid,
                "guid":          ev.get("EventGuid", ""),
                "committee":     ev["_name"],
                "date":          ev.get("EventDate", ""),
                "time":          ev.get("EventTime", ""),
                "location":      (ev.get("EventLocation") or "").strip('"'),
                "agenda_status": ev.get("EventAgendaStatusName", ""),
                "is_past":       ev["_is_past"],
                "hearing_titles": hearing_titles,
                "agenda":        agenda_items,
                "topics":        event_topics,
                "url":           ev.get("EventInSiteURL") or f"https://nyc.legistar.com/MeetingDetail.aspx?ID={eid}&GUID={ev.get('EventGuid', '')}",
                "is_stated":     ev["_body_id"] == 1,
                "video_url":     ev.get("EventVideoPath") or "",
            })

    return sorted(enriched, key=lambda e: e["date"])


def fetch_bills() -> list[dict]:
    # NYC Council operates on 4-year terms (elections in 2021, 2025, 2029...).
    # New terms begin in January the following year. Calculate automatically.
    current_year = datetime.now().year
    term_start_year = current_year - ((current_year - 2022) % 4)
    session_start = f"{term_start_year}-01-01T00:00:00"
    new_cutoff    = (datetime.now() - timedelta(days=14)).isoformat()

    all_matters = []
    seen: set[int] = set()

    for name, body_id in TARGET_BODIES.items():
        try:
            results = legistar("matters", {
                "$filter": f"MatterBodyId eq {body_id} and MatterIntroDate ge datetime'{session_start}'",
                "$top": "200",
            })
        except Exception as e:
            log.warning(f"Matters fetch failed for {name}: {e}")
            continue

        for m in results:
            mid = m.get("MatterId")
            if mid in seen:
                continue
            seen.add(mid)

            matter_type = m.get("MatterTypeName") or ""
            if matter_type in SKIP_MATTER_TYPES:
                continue
            # Skip hearing transcripts / testimony records (file prefix "T")
            if (m.get("MatterFile") or "").startswith("T"):
                continue

            text = (
                (m.get("MatterTitle") or "") + " "
                + (m.get("MatterName") or "") + " "
                + (m.get("MatterEXText5") or "")
            )
            topics = get_topics(text)
            always = body_id in ALWAYS_INCLUDE

            if not (always or topics or is_relevant(text)):
                continue

            last_action_date = m.get("MatterEXDate10") or m.get("MatterLastModifiedUtc") or ""
            intro_date = m.get("MatterIntroDate") or ""

            all_matters.append({
                "id":               mid,
                "file":             m.get("MatterFile", ""),
                "name":             m.get("MatterName", ""),
                "title":            m.get("MatterTitle", ""),
                "type":             m.get("MatterTypeName", ""),
                "status":           m.get("MatterStatusName", ""),
                "committee":        m.get("MatterBodyName", name),
                "intro_date":       intro_date,
                "last_action_date": last_action_date,
                "last_action_text": m.get("MatterEXText10", ""),
                "summary":          (m.get("MatterEXText5") or "").strip(),
                "sponsor":          m.get("MatterEXText9", ""),
                "topics":           topics,
                "url":              matter_url(mid, m.get("MatterGuid", "")),
                "is_new":           intro_date > new_cutoff,
            })

    return sorted(
        all_matters,
        key=lambda m: m["last_action_date"] or m["intro_date"],
        reverse=True,
    )


def get_data(force: bool = False) -> dict:
    if not force:
        cached = cache_get("dashboard")
        if cached:
            return cached

    log.info("Refreshing data from Legistar...")
    try:
        hearings = fetch_hearings()
        bills    = fetch_bills()
    except Exception as e:
        log.error(f"Data fetch failed: {e}")
        stale = cache_get("dashboard_stale")
        if stale:
            stale["stale"] = True
            return stale
        raise

    data = {
        "hearings":       hearings,
        "bills":          bills,
        "fetched_at":     datetime.now().strftime("%-m/%-d/%Y at %-I:%M %p"),
        "upcoming_count": sum(1 for h in hearings if not h["is_past"]),
        "bill_count":     len(bills),
        "stale":          False,
    }

    cache_set("dashboard", data)
    # Keep a stale copy as fallback indefinitely
    cache_set("dashboard_stale", data, ttl=60 * 24 * 7)
    log.info(f"Done: {len(hearings)} hearings, {len(bills)} bills")
    return data


# ── Template Filters ───────────────────────────────────────────────────────────

@app.template_filter("fmt_date")
def fmt_date(v: str) -> str:
    if not v:
        return ""
    try:
        return datetime.fromisoformat(v).strftime("%-m/%-d/%y")
    except (ValueError, TypeError):
        return v


@app.template_filter("fmt_date_full")
def fmt_date_full(v: str) -> str:
    if not v:
        return ""
    try:
        return datetime.fromisoformat(v).strftime("%A, %B %-d, %Y")
    except (ValueError, TypeError):
        return v


@app.template_filter("status_style")
def status_style(s: str) -> str:
    s_lower = (s or "").lower()
    for key, cls in STATUS_STYLES.items():
        if key in s_lower:
            return cls
    return "bg-gray-100 text-gray-500"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    data = get_data()
    return render_template("index.html", data=data, topics=TOPICS)


@app.route("/refresh")
def do_refresh():
    get_data(force=True)
    return redirect(url_for("index"))



if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=os.environ.get("FLASK_ENV") != "production", port=port, use_reloader=False)
